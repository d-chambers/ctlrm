"""Optional fixed tasks expand reviews within a durable bounded continuation."""

import copy

import pytest

from ctlrm.managed.workflows import WorkflowEngine, WorkflowService
from ctlrm.runtime.workflows import WorkflowTemplate
from conftest import FakeTerminal


@pytest.fixture
def specialist_template():
    """Use human participants to exercise the scheduler without external providers."""
    return WorkflowTemplate.model_validate(
        {
            "name": "specialists",
            "entry": "review",
            "profiles": {},
            "roles": {"owner": {"kind": "human"}},
            "limits": {"max_specialist_executions": 2},
            "tasks": {
                "review": {
                    "role": "owner",
                    "verify_input": True,
                    "specialist_tasks": ["performance", "concurrency"],
                    "transitions": {"approved": "finish", "changes_requested": "fix"},
                },
                "performance": {
                    "role": "owner",
                    "verify_input": True,
                    "optional": True,
                    "transitions": {"approved": "terminal:return", "changes_requested": "fix"},
                },
                "concurrency": {
                    "role": "owner",
                    "verify_input": True,
                    "optional": True,
                    "transitions": {"approved": "terminal:return", "changes_requested": "fix"},
                },
                "fix": {
                    "role": "owner",
                    "verify_input": False,
                    "max_executions": 1,
                    "transitions": {"completed": "review"},
                },
                "finish": {
                    "role": "owner",
                    "verify_input": True,
                    "transitions": {"done": "terminal:completed"},
                },
            },
        }
    )


def report(engine, client, outcome, **extra):
    """Acknowledge and report one active human task through the journal."""
    engine.tick()
    item = engine.active()
    owner = {
        "run_id": engine.state["run"]["id"],
        "execution_id": item["id"],
        "participant": "owner",
        "input_artifact": item["input"].get("artifact"),
    }
    client.request("workflow-ack", owner)
    engine.tick()
    request = client.request(
        "workflow-report",
        {**owner, "outcome": outcome, "summary": "Findings for " + item["task"], **extra},
    )
    engine.tick()
    return engine.state["requests"][request]


class TestSpecialistExecution:
    """Requests, replay, findings, and limits preserve task and artifact ownership."""

    def test_queue_and_replay(self, repository, specialist_template) -> None:
        """A two-review expansion resumes its normal graph only after both approve."""
        client, _ = WorkflowService.submit(repository, specialist_template, "Goal", "Inspect")
        client.join("owner")
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            result = report(
                engine,
                client,
                "approved",
                specialists=["performance", "concurrency"],
                specialist_reason="Large locking change",
            )
            assert result["status"] == "accepted"
            assert engine.active()["task"] == "performance"
            first = engine.active()["id"]
            engine = WorkflowEngine(client.area, FakeTerminal())
            engine.tick()
            assert engine.active()["id"] == first
            assert report(engine, client, "approved")["status"] == "accepted"
            assert engine.active()["task"] == "concurrency"
            assert report(engine, client, "approved")["status"] == "accepted"
            assert engine.active()["task"] == "finish"
            assert len(engine.active()["input"]["reviews"]) == 2
            assert report(engine, client, "done")["status"] == "accepted"
            assert engine.state["run"]["status"] == "completed"

    def test_findings_and_limits(self, repository, specialist_template) -> None:
        """Findings supersede pending reviews; exhausted budgets cannot silently approve."""
        client, _ = WorkflowService.submit(repository, specialist_template, "Goal", "Inspect")
        client.join("owner")
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            assert (
                report(engine, client, "approved", specialists=["missing"], specialist_reason="x")[
                    "status"
                ]
                == "rejected"
            )
            assert (
                report(
                    engine,
                    client,
                    "approved",
                    specialists=["performance", "concurrency"],
                    specialist_reason="x",
                )["status"]
                == "accepted"
            )
            assert report(engine, client, "changes_requested")["status"] == "accepted"
            assert engine.active()["task"] == "fix"
            assert engine.active()["input"]["reviews"][0]["summary"] == "Findings for performance"
            assert report(engine, client, "completed")["status"] == "accepted"
            assert (
                report(
                    engine,
                    client,
                    "approved",
                    specialists=["performance", "concurrency"],
                    specialist_reason="again",
                )["status"]
                == "rejected"
            )
            assert report(engine, client, "changes_requested")["status"] == "accepted"
            assert engine.state["run"]["status"] == "blocked"

    def test_changed_input_rejected(self, repository, specialist_template) -> None:
        """An optional specialist cannot approve a version changed during its review."""
        client, _ = WorkflowService.submit(repository, specialist_template, "Goal", "Inspect")
        client.join("owner")
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            report(engine, client, "approved", specialists=["performance"], specialist_reason="x")
            (repository / "source.txt").write_text("changed during review\n")
            assert report(engine, client, "approved")["status"] == "rejected"
            assert engine.active()["task"] == "performance"

    def test_size_trigger_and_pr(self, repository, specialist_template) -> None:
        """Tracked change-size thresholds request only named tasks and PR metadata is validated."""
        specialist_template.tasks["review"].specialist_above_lines = {"performance": 2}
        specialist_template.tasks["finish"].requires_pr = ["done"]
        client, _ = WorkflowService.submit(repository, specialist_template, "Goal", "Inspect")
        client.join("owner")
        (repository / "source.txt").write_text("a\nb\nc\nd\n")
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            assert report(engine, client, "approved")["status"] == "accepted"
            assert engine.active()["task"] == "performance"
            assert report(engine, client, "approved")["status"] == "accepted"
            assert engine.active()["task"] == "finish"
            assert report(engine, client, "done")["status"] == "rejected"
            assert (
                report(engine, client, "done", pr={"number": 42, "repository": "owner/repo"})[
                    "status"
                ]
                == "accepted"
            )
            assert engine.state["pr"]["number"] == 42


class TestSpecialistValidation:
    """Workflow authors define the allowed graph; agents cannot create arbitrary tasks."""

    @pytest.mark.parametrize(
        "defect", ["recursive", "mutating", "unknown", "normal_return", "direct_entry"]
    )
    def test_invalid_policy(self, specialist_template, defect) -> None:
        """Reject policies that evade review-only tasks or bounded entry points."""
        data = copy.deepcopy(specialist_template.model_dump())
        if defect == "recursive":
            data["tasks"]["performance"]["specialist_tasks"] = ["concurrency"]
        elif defect == "mutating":
            data["tasks"]["performance"]["verify_input"] = False
        elif defect == "unknown":
            data["tasks"]["review"]["specialist_tasks"] = ["not-defined"]
        elif defect == "normal_return":
            data["tasks"]["finish"]["transitions"]["done"] = "terminal:return"
        else:
            data["tasks"]["review"]["transitions"]["approved"] = "performance"
        with pytest.raises(ValueError):
            WorkflowTemplate.model_validate(data)


class TestSpecialistRetryBudget:
    """Explicit retries cannot evade the workflow's global specialist cap."""

    def test_retry_at_cap(self, repository, specialist_template) -> None:
        """Rejecting a retry preserves the active execution so it can still report its result."""
        specialist_template.limits.max_specialist_executions = 1
        client, _ = WorkflowService.submit(repository, specialist_template, "Goal", "Inspect")
        client.join("owner")
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            report(engine, client, "approved", specialists=["performance"], specialist_reason="x")
            execution_id = engine.active()["id"]
            request = client.request(
                "workflow-retry", {"execution_id": execution_id, "reason": "Again"}
            )
            engine.tick()
            assert engine.state["requests"][request]["status"] == "rejected"
            assert engine.active()["id"] == execution_id
            assert report(engine, client, "approved")["status"] == "accepted"


class TestSpecialistVersionRetry:
    """A changed review input invalidates the whole specialty approval continuation."""

    def test_changed_retry_returns_to_fix(self, repository, specialist_template) -> None:
        """Earlier approvals cannot authorize a changed version through a later specialist retry."""
        client, _ = WorkflowService.submit(repository, specialist_template, "Goal", "Inspect")
        client.join("owner")
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            report(
                engine,
                client,
                "approved",
                specialists=["performance", "concurrency"],
                specialist_reason="x",
            )
            report(engine, client, "approved")
            assert engine.active()["task"] == "concurrency"
            (repository / "source.txt").write_text("new version\n")
            request = client.request(
                "workflow-retry",
                {"execution_id": engine.active()["id"], "reason": "Review changed code"},
            )
            engine.tick()
            assert engine.state["requests"][request]["status"] == "accepted"
            assert engine.active()["task"] == "fix"
            assert "specialist_context" not in engine.active()["input"]
            assert "invalidated_specialist_context" in engine.active()["input"]

    def test_local_review_output(self, repository, specialist_template) -> None:
        """The assignment's review directory is excluded even for a local workflow."""
        from ctlrm.managed.artifacts import fingerprint

        client, _ = WorkflowService.submit(repository, specialist_template, "Goal", "Inspect")
        client.join("owner")
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            engine.tick()
            assert "Review records directory: .ctlrm/reviews" in engine.active()["message"]["body"]
            before = fingerprint(repository)
            reviews = client.area.room.root / "reviews"
            reviews.mkdir()
            (reviews / "findings.md").write_text("No findings.\n")
            assert fingerprint(repository) == before


class TestCompletedReviewProvenance:
    """Approvals remain attributable even after control returns to the normal graph."""

    def test_changed_continuation_retry(self, repository, specialist_template) -> None:
        """An extra non-review handoff cannot let retry certify a different code version."""
        specialist_template.tasks["finish"].transitions = {"done": "publish"}
        specialist_template.tasks["publish"] = specialist_template.tasks["finish"].model_copy(
            deep=True
        )
        specialist_template.tasks["publish"].transitions = {"done": "terminal:completed"}
        client, _ = WorkflowService.submit(repository, specialist_template, "Goal", "Inspect")
        client.join("owner")
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            report(engine, client, "approved", specialists=["performance"], specialist_reason="x")
            report(engine, client, "approved")
            report(engine, client, "done")
            execution = engine.active()["id"]
            (repository / "source.txt").write_text("unreviewed changes\n")
            request = client.request(
                "workflow-retry", {"execution_id": execution, "reason": "Refresh"}
            )
            engine.tick()
            assert engine.state["requests"][request]["status"] == "rejected"
            assert "invalidate completed reviews" in engine.state["requests"][request]["error"]
            assert engine.active()["id"] == execution
            assert report(engine, client, "done")["status"] == "rejected"

    @pytest.mark.parametrize("reason", ["x" * 4097, 42, " "])
    def test_automatic_reason_bounds(self, repository, specialist_template, reason) -> None:
        """Automatic specialist selection cannot introduce an unbounded assignment reason."""
        specialist_template.tasks["review"].specialist_above_lines = {"performance": 0}
        client, _ = WorkflowService.submit(repository, specialist_template, "Goal", "Inspect")
        client.join("owner")
        (repository / "source.txt").write_text("changed\n")
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            result = report(engine, client, "approved", specialist_reason=reason)
            assert result["status"] == "rejected"
            assert engine.active()["task"] == "review"
