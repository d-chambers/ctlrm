"""Workflow handoffs survive replay and preserve worktree/task isolation."""

import copy
import subprocess

import pytest

from ctlrm.managed.workflows import WorkflowEngine, WorkflowService
from ctlrm.runtime.workflows import WorkflowTemplate
from conftest import FakeTerminal


@pytest.fixture
def template(profile):
    """Use two reusable agent roles followed by explicit human approval."""
    return WorkflowTemplate.model_validate(
        {
            "name": "implement-review",
            "entry": "implement",
            "profiles": {"test": profile.model_dump()},
            "roles": {
                "implementer": {"kind": "agent", "profile": "test"},
                "reviewer": {"kind": "agent", "profile": "test"},
                "owner": {"kind": "human"},
            },
            "steps": {
                "implement": {"role": "implementer", "transitions": {"completed": "review"}},
                "review": {
                    "role": "reviewer",
                    "transitions": {"approved": "approve", "changes_requested": "implement"},
                },
                "approve": {
                    "role": "owner",
                    "transitions": {
                        "approved": "terminal:completed",
                        "rejected": "terminal:rejected",
                    },
                },
            },
        }
    )


def prepare(engine, client):
    """Simulate manual provider startup/readiness and return assignment ownership."""
    execution = engine.active()
    participant = execution["participant"]
    if client.area.data["roles"][participant]["kind"] == "human":
        client.join(participant)
    else:
        session = next(
            (s for s in engine.state["sessions"].values() if s["participant"] == participant), None
        )
        if not session:
            client.request("launch", {"participant": participant})
            engine.tick()
            session = next(
                s for s in engine.state["sessions"].values() if s["participant"] == participant
            )
        if not session["ready"]:
            client.ready(session["id"], session["generation"], session["native_id"])
    engine.tick()
    execution = engine.active()
    return {
        "run_id": engine.state["run"]["id"],
        "execution_id": execution["id"],
        "participant": participant,
        "session_id": execution["session_id"],
        "generation": execution["generation"],
    }


def complete(engine, client, outcome, summary="Checked"):
    """Use the same acknowledgment and report path for agent and human visits."""
    payload = prepare(engine, client)
    client.request("workflow-ack", payload)
    engine.tick()
    request = client.request("workflow-report", {**payload, "outcome": outcome, "summary": summary})
    engine.tick()
    assert engine.state["requests"][request]["status"] == "accepted"
    return payload


class TestWorkflow:
    """Run transitions remain distinct from process liveness and legacy room state."""

    def test_review_loop_and_replay(self, repository, template) -> None:
        """Each visit gets one durable assignment; sessions are reused across review loops."""
        client, request = WorkflowService.submit(
            repository, template, "Task", "Build it", request_id="submit"
        )
        terminal = FakeTerminal()
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            complete(engine, client, "completed")
            complete(engine, client, "changes_requested", "Fix the edge case")
            assert engine.active()["input"]["summary"] == "Fix the edge case"
            payload = prepare(engine, client)
            original = copy.deepcopy(engine.active())
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            assert engine.active() == original
            client.request("workflow-ack", payload, "duplicate-ack")
            client.request("workflow-ack", payload, "duplicate-ack")
            engine.tick()
            complete(engine, client, "completed")
            complete(engine, client, "approved")
            complete(engine, client, "approved")
            run = engine.state["run"]
            assert run["status"] == "completed"
            assert len({e["id"] for e in run["executions"]}) == 5
            assert terminal.launches == 2
            assert len(client.area.room.read_inbox("implementer")) == 3

    def test_rejections_and_snapshot(self, repository, template) -> None:
        """Unknown and stale outcomes cannot complete work; caller mutations do not alter snapshots."""
        client, request = WorkflowService.submit(
            repository, template, "Task", "Build", request_id="submit"
        )
        retried, same = WorkflowService.submit(
            repository, template, "Task", "Build", request_id="submit"
        )
        assert retried.area.data == client.area.data and same == request
        with pytest.raises(ValueError, match="another instance"):
            WorkflowService.submit(repository, template, "Other", "Build", request_id="other")
        template.steps["implement"].transitions["completed"] = "terminal:completed"
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            engine.tick()
            payload = prepare(engine, client)
            no_ack = client.request(
                "workflow-report", {**payload, "outcome": "completed", "summary": "x"}
            )
            engine.tick()
            assert engine.state["requests"][no_ack]["status"] == "rejected"
            client.request("workflow-ack", payload)
            engine.tick()
            bad = client.request(
                "workflow-report", {**payload, "outcome": "unknown", "summary": "x"}
            )
            engine.tick()
            assert engine.state["requests"][bad]["status"] == "rejected"
            complete(engine, client, "completed")
            assert engine.active()["step"] == "review"
            stale = client.request(
                "workflow-report", {**payload, "outcome": "completed", "summary": "x"}
            )
            engine.tick()
            assert engine.state["requests"][stale]["status"] == "rejected"

    def test_one_agent_and_second_worktree(self, repository, profile, tmp_path) -> None:
        """One-agent graphs and repeated templates retain independent identities and state."""
        template = WorkflowTemplate.model_validate(
            {
                "name": "single",
                "entry": "work",
                "profiles": {"test": profile.model_dump()},
                "roles": {"worker": {"kind": "agent", "profile": "test"}},
                "steps": {
                    "work": {"role": "worker", "transitions": {"completed": "terminal:completed"}}
                },
            }
        )
        other = tmp_path / "linked"
        subprocess.run(
            ["git", "-C", str(repository), "worktree", "add", "-b", "other", str(other)],
            check=True,
            capture_output=True,
        )
        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        second, _ = WorkflowService.submit(other, template, "Task", "Build")
        assert client.area.data["id"] != second.area.data["id"]
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            engine.tick()
            complete(engine, client, "completed")
            assert engine.state["run"]["status"] == "completed"
        assert second.area.journal.replay()[0].get("run") is None

    def test_execution_limit(self, repository, template) -> None:
        """Intentional cycles stop at a bounded count without reopening prior visits."""
        template.limits.max_step_executions = 2
        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            engine.tick()
            complete(engine, client, "completed")
            complete(engine, client, "changes_requested")
            assert engine.state["run"]["status"] == "blocked"
            assert "limit" in engine.state["run"]["reason"]


class TestDeliveryRecovery:
    """Assignment publication is retryable after its transition has committed."""

    def test_crash_before_mailbox(self, repository, template, monkeypatch) -> None:
        """Replaying after publication failure retains the allocated message and execution IDs."""
        import ctlrm.managed.workflows as workflows

        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        terminal = FakeTerminal()
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            complete(engine, client, "completed")
            complete(engine, client, "approved")
            execution = copy.deepcopy(engine.active())
            path = (
                client.area.room.root
                / "participants/owner/inbox"
                / f"{execution['message']['id']}.md"
            )
            path.unlink()

            def crash(*args):
                """Model supervisor death at the mailbox boundary."""
                raise KeyboardInterrupt("publication crash")

            with monkeypatch.context() as patch:
                patch.setattr(workflows, "mailbox", crash)
                with pytest.raises(KeyboardInterrupt):
                    engine.tick()
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            assert engine.active() == execution
            assert path.exists()
            payload = {
                "run_id": engine.state["run"]["id"],
                "execution_id": execution["id"],
                "participant": "owner",
            }
            unjoined = client.request("workflow-ack", payload)
            engine.tick()
            assert engine.state["requests"][unjoined]["status"] == "rejected"


class TestPrecommitFailures:
    """Rejected initialization and failed commits must not leak executable work."""

    def test_blank_task_does_not_claim_area(self, repository, template) -> None:
        """All task validation happens before immutable area publication."""
        with pytest.raises(ValueError, match="nonblank"):
            WorkflowService.submit(repository, template, "Task", "   ")
        assert not (repository / ".ctlrm/area.yaml").exists()

    def test_assignment_commit_failure_cannot_wake(self, repository, template, monkeypatch) -> None:
        """A failed assignment commit rolls memory back before any terminal delivery."""
        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        terminal = FakeTerminal()
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            client.request("launch", {"participant": "implementer"})
            engine.tick()
            current = next(iter(engine.state["sessions"].values()))
            client.ready(current["id"], 1, current["native_id"])
            original = engine.commit

            def fail_assignment(kind, detail=None):
                """Inject a disk failure precisely at assignment commit."""
                if kind == "assignment-planned":
                    raise OSError("disk full")
                original(kind, detail)

            monkeypatch.setattr(engine, "commit", fail_assignment)
            errors = engine.tick()
            assert errors and not terminal.wakes
            assert engine.active()["message"] is None
            assert next(iter(engine.state["sessions"].values()))["work"] is None


class TestHumanIdentity:
    """Legacy room access cannot bypass the managed human role contract."""

    def test_conflicting_signature(self, repository, template) -> None:
        """A participant signed as an agent cannot approve a human workflow step."""
        from ctlrm.managed.storage import now

        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            engine.tick()
            complete(engine, client, "completed")
            complete(engine, client, "approved")
            client.area.room.join_participant(
                participant_id="owner",
                name="owner",
                kind="agent",
                provider="codex",
                capabilities=[],
                joined_at=now(),
                restart_command=None,
            )
            execution = engine.active()
            request = client.request(
                "workflow-ack",
                {
                    "run_id": engine.state["run"]["id"],
                    "execution_id": execution["id"],
                    "participant": "owner",
                },
            )
            engine.tick()
            assert engine.state["requests"][request]["status"] == "rejected"
            assert "signature conflicts" in engine.state["requests"][request]["error"]


class TestUncertainOutcome:
    """An uncertainty block requires explicit reconciliation before task completion."""

    def test_blocked_session_cannot_report(self, repository, template) -> None:
        """Readiness alone is not authority after a participant reports uncertain effects."""
        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            engine.tick()
            payload = prepare(engine, client)
            client.request("workflow-ack", payload)
            engine.tick()
            client.request(
                "disposition",
                {**payload, "work_id": payload["execution_id"], "status": "uncertain"},
            )
            engine.tick()
            report = client.request(
                "workflow-report", {**payload, "outcome": "completed", "summary": "Done"}
            )
            engine.tick()
            assert engine.state["requests"][report]["status"] == "rejected"
            assert engine.active()["id"] == payload["execution_id"]


class TestSubmissionIdentity:
    """Invalid request IDs cannot reserve an otherwise fresh worktree."""

    def test_invalid_request_id(self, repository, template) -> None:
        """Validate the client retry key before publishing the area snapshot."""
        with pytest.raises(ValueError, match="request id"):
            WorkflowService.submit(repository, template, "Task", "Build", request_id="bad/id")
        assert not (repository / ".ctlrm/area.yaml").exists()
        client, _ = WorkflowService.submit(
            repository, template, "Task", "Build", request_id="valid"
        )
        assert client.area.data["mode"] == "workflow"


class TestWorkflowCli:
    """User-visible commands use the same service and committed request results."""

    def test_submit_and_human_response(self, repository, monkeypatch) -> None:
        """A human-only workflow completes through the public Typer interface."""
        import json
        from typer.testing import CliRunner
        from ctlrm.__main__ import app
        from ctlrm.managed import supervisor

        template_path = repository / "workflow.json"
        template_path.write_text(
            json.dumps(
                {
                    "name": "approval",
                    "entry": "approve",
                    "profiles": {},
                    "roles": {"owner": {"kind": "human"}},
                    "steps": {
                        "approve": {
                            "role": "owner",
                            "transitions": {"approved": "terminal:completed"},
                        }
                    },
                }
            )
        )
        task_path = repository / "task.txt"
        task_path.write_text("Check the task")

        def start(area):
            """Drive the actual engine synchronously instead of starting a daemon."""
            with area.journal.writer():
                WorkflowEngine(area, FakeTerminal()).tick()

        monkeypatch.setattr(supervisor, "start", start)
        runner = CliRunner()
        prefix = ["--root", str(repository), "workflow"]
        result = runner.invoke(app, [*prefix, "validate", "--template", str(template_path)])
        assert result.exit_code == 0, result.output
        result = runner.invoke(
            app,
            [
                *prefix,
                "submit",
                "--template",
                str(template_path),
                "--title",
                "Task",
                "--file",
                str(task_path),
            ],
        )
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, [*prefix, "status"])
        state = json.loads(result.output)["state"]
        run = state["run"]
        args = ["--run-id", run["id"], "--execution-id", run["active"], "--participant", "owner"]
        assert runner.invoke(app, [*prefix, "join", "--participant", "owner"]).exit_code == 0
        result = runner.invoke(app, [*prefix, "acknowledge", *args])
        assert result.exit_code == 0, result.output
        result = runner.invoke(
            app, [*prefix, "report", *args, "--outcome", "approved", "--summary", "Accepted"]
        )
        assert result.exit_code == 0, result.output
        assert (
            json.loads(runner.invoke(app, [*prefix, "status"]).output)["state"]["run"]["status"]
            == "completed"
        )
