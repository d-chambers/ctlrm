"""Workflow handoffs survive replay and preserve worktree/task isolation."""

import copy
import subprocess

import pytest

from ctlrm.managed.workflows import WorkflowEngine, WorkflowService
from ctlrm.runtime.workflows import WorkflowTemplate
from conftest import FakeTerminal
from test_projects import human_template as human_template


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
            "tasks": {
                "implement": {
                    "role": "implementer",
                    "verify_input": False,
                    "transitions": {"completed": "review"},
                },
                "review": {
                    "role": "reviewer",
                    "verify_input": True,
                    "transitions": {"approved": "approve", "changes_requested": "implement"},
                },
                "approve": {
                    "role": "owner",
                    "verify_input": True,
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
        "input_artifact": execution["input"].get("artifact"),
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
    """Run transitions remain distinct from process liveness and room state."""

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
        template.tasks["implement"].transitions["completed"] = "terminal:completed"
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
            assert engine.active()["task"] == "review"
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
                "tasks": {
                    "work": {
                        "role": "worker",
                        "verify_input": False,
                        "transitions": {"completed": "terminal:completed"},
                    }
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
        template.limits.max_task_executions = 2
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
                client.area.runtime
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
    """Direct room access cannot bypass the managed human role contract."""

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
                    "tasks": {
                        "approve": {
                            "role": "owner",
                            "verify_input": True,
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


class TestCounterpartBoundaries:
    """Dispatch errors, recovery, and cooperative role restrictions remain independent."""

    def test_dispatch_conflict_does_not_delay_stop(self, repository, template) -> None:
        """A poisoned mailbox must not prevent an accepted stop from taking effect."""
        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        terminal = FakeTerminal()
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            payload = prepare(engine, client)
            execution = engine.active()
            path = (
                client.area.runtime
                / "participants/implementer/inbox"
                / f"{execution['message']['id']}.md"
            )
            path.write_text("foreign content")
            client.request("stop", {"session_id": payload["session_id"]})
            engine.tick()
            assert engine.state["sessions"][payload["session_id"]]["status"] == "stopped"
            assert not terminal.terminals

    def test_recovered_work_requires_ack(self, repository, template) -> None:
        """not_started cannot inherit the previous generation's execution acknowledgment."""
        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        terminal = FakeTerminal()
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            payload = prepare(engine, client)
            client.request("workflow-ack", payload)
            engine.tick()
            current = engine.state["sessions"][payload["session_id"]]
            terminal.terminals[current["spec"]["terminal_name"]]["health"] = "exited"
            for _ in range(3):
                engine.tick()
            client.ready(current["id"], 2, current["native_id"])
            engine.tick()
            payload["generation"] = 2
            client.request(
                "disposition",
                {**payload, "work_id": payload["execution_id"], "status": "not_started"},
            )
            engine.tick()
            report = client.request(
                "workflow-report", {**payload, "outcome": "completed", "summary": "Done"}
            )
            engine.tick()
            assert engine.state["requests"][report]["status"] == "rejected"
            client.request("workflow-ack", payload)
            engine.tick()
            report = client.request(
                "workflow-report", {**payload, "outcome": "completed", "summary": "Done"}
            )
            engine.tick()
            assert engine.state["requests"][report]["status"] == "accepted"

    def test_managed_agent_cannot_join_human(self, repository, template, monkeypatch) -> None:
        """Prevent accidental role crossing by a managed agent's inherited environment."""
        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        monkeypatch.setenv("CTLRM_SESSION", "managed-agent")
        with pytest.raises(ValueError, match="cannot accept a human"):
            client.join("owner")


class TestAutomaticWorkflow:
    """Tasks drive role launches and authoritative handoffs without manual routing."""

    def test_lazy_launch_and_stale_approval(self, repository, template) -> None:
        """Only the active role launches, and approval is tied to the delivered bytes."""
        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        terminal = FakeTerminal()
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            assert terminal.launches == 1
            assert {s["participant"] for s in engine.state["sessions"].values()} == {"implementer"}
            (repository / "new-code.txt").write_text("delivered")
            complete(engine, client, "completed")
            assert terminal.launches == 2
            payload = prepare(engine, client)
            wrong = client.request("workflow-ack", {**payload, "input_artifact": "wrong"})
            engine.tick()
            assert engine.state["requests"][wrong]["status"] == "rejected"
            client.request("workflow-ack", payload)
            engine.tick()
            (repository / "new-code.txt").write_text("changed")
            report = client.request(
                "workflow-report", {**payload, "outcome": "approved", "summary": "Looks good"}
            )
            engine.tick()
            assert engine.state["requests"][report]["status"] == "rejected"
            assert "artifact changed" in engine.state["requests"][report]["error"]
            assert engine.active()["task"] == "review"

    def test_cancel_wins_over_report(self, repository, template) -> None:
        """Accepted cancellation prevents a late completion and disables recovery."""
        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        terminal = FakeTerminal()
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            payload = prepare(engine, client)
            client.request("workflow-ack", payload)
            engine.tick()
            client.request("workflow-cancel", {})
            late = client.request(
                "workflow-report", {**payload, "outcome": "completed", "summary": "Done"}
            )
            engine.tick()
            engine.tick()
            assert engine.state["run"]["status"] == "canceled"
            assert engine.state["requests"][late]["status"] == "rejected"
            assert not terminal.terminals and terminal.launches == 1
            WorkflowEngine(client.area, terminal).tick()
            assert terminal.launches == 1

    def test_retry_is_new_visit(self, repository, template) -> None:
        """Retry requires explicit stop and preserves the abandoned execution as evidence."""
        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        terminal = FakeTerminal()
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            payload = prepare(engine, client)
            retry = {"execution_id": payload["execution_id"], "reason": "Explicitly repeat work"}
            unsafe = client.request("workflow-retry", retry)
            engine.tick()
            assert engine.state["requests"][unsafe]["status"] == "rejected"
            client.request("stop", {"session_id": payload["session_id"]})
            engine.tick()
            request = client.request("workflow-retry", retry)
            engine.tick()
            assert engine.state["requests"][request]["status"] == "accepted"
            assert engine.active()["id"] != payload["execution_id"]
            assert engine.state["run"]["executions"][0]["status"] == "abandoned"
            assert terminal.launches == 1
            client.request("resume", {"session_id": payload["session_id"]})
            engine.tick()
            complete(engine, client, "completed")
            assert engine.active()["task"] == "review"

    def test_cancel_after_branch_drift(self, repository, template) -> None:
        """Cancellation remains available even when further dispatch has been paused."""
        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        terminal = FakeTerminal()
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            subprocess.run(
                ["git", "-C", str(repository), "switch", "-c", "drift"],
                check=True,
                capture_output=True,
            )
            engine.tick()
            client.request("workflow-cancel", {})
            engine.tick()
            engine.tick()
            assert engine.state["run"]["status"] == "canceled"
            assert not terminal.terminals


class TestHandoffCrashes:
    """Committed outcomes and assignments remain unique across supervisor failure boundaries."""

    def test_crash_after_outcome_commit(self, repository, template, monkeypatch) -> None:
        """Replay drives the already accepted next visit without accepting the outcome twice."""
        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        terminal = FakeTerminal()
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            payload = prepare(engine, client)
            client.request("workflow-ack", payload)
            engine.tick()
            request = client.request(
                "workflow-report", {**payload, "outcome": "completed", "summary": "Done"}, "handoff"
            )
            original = engine.commit

            def crash(kind, detail=None):
                """Kill the supervisor after accepting the outcome and before next-role effects."""
                original(kind, detail)
                if kind == "request" and detail["id"] == request:
                    raise KeyboardInterrupt("after outcome commit")

            monkeypatch.setattr(engine, "commit", crash)
            with pytest.raises(KeyboardInterrupt):
                engine.tick()
            recorded = client.area.journal.replay()[0]["run"]["active"]
            restarted = WorkflowEngine(client.area, terminal)
            restarted.tick()
            assert restarted.active()["id"] == recorded
            assert len(restarted.state["run"]["executions"]) == 2
            assert terminal.launches == 2
            client.request(
                "workflow-report", {**payload, "outcome": "completed", "summary": "Done"}, "handoff"
            )
            restarted.tick()
            assert len(restarted.state["run"]["executions"]) == 2

    def test_crash_after_wake(self, repository, template, monkeypatch) -> None:
        """A repeated hint after restart still refers to the same logical assignment."""
        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        terminal = FakeTerminal()
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            current = next(iter(engine.state["sessions"].values()))
            client.ready(current["id"], 1, current["native_id"])
            original = terminal.wake

            def crash(*args):
                """Interrupt after terminal delivery but before the volatile wake cache update."""
                original(*args)
                raise KeyboardInterrupt("after wake")

            with monkeypatch.context() as patch:
                patch.setattr(terminal, "wake", crash)
                with pytest.raises(KeyboardInterrupt):
                    engine.tick()
            restarted = WorkflowEngine(client.area, terminal)
            restarted.tick()
            assert len(terminal.wakes) == 2 and terminal.wakes[0] == terminal.wakes[1]
            assert len(restarted.state["run"]["executions"]) == 1
            assert restarted.active()["status"] == "pending"


class TestApprovalVersionRace:
    """The artifact attributed to an approval must be the exact version that was checked."""

    def test_captured_version_is_verified(self, repository, template, monkeypatch) -> None:
        """A change between separate observations cannot be forwarded as already approved."""
        import ctlrm.managed.artifacts as artifacts
        from ctlrm.managed.storage import read_record

        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            engine.tick()
            complete(engine, client, "completed")
            payload = prepare(engine, client)
            client.request("workflow-ack", payload)
            engine.tick()
            incoming = read_record(
                client.area.runtime / "artifacts" / f"{payload['input_artifact']}.json"
            )
            original = artifacts.fingerprint
            observed = []

            def fingerprint(root):
                """Change files if approval independently observes them a second time."""
                if observed:
                    (root / "source.txt").write_text("changed between verification and capture")
                observed.append(1)
                return original(root)

            monkeypatch.setattr(artifacts, "fingerprint", fingerprint)
            request = client.request(
                "workflow-report",
                {**payload, "outcome": "approved", "summary": "Approved delivered version"},
            )
            engine.tick()
            assert engine.state["requests"][request]["status"] == "accepted"
            accepted = engine.state["run"]["executions"][1]
            outgoing = read_record(
                client.area.runtime / "artifacts" / f"{accepted['artifact']}.json"
            )
            assert incoming["version"] == outgoing["version"]


class TestReviewedWorkflowBoundaries:
    """Timeouts, retry provenance, and custom approval names obey explicit workflow policy."""

    def test_git_timeout_rejects_report(self, repository, template, monkeypatch) -> None:
        """A slow Git status rejects the request while the supervisor continues ticking."""
        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            engine.tick()
            payload = prepare(engine, client)
            client.request("workflow-ack", payload)
            engine.tick()
            original = subprocess.run

            def run(argv, *args, **kwargs):
                """Inject a timeout into artifact dirty-state inspection only."""
                if "status" in argv:
                    raise subprocess.TimeoutExpired(argv, 15)
                return original(argv, *args, **kwargs)

            monkeypatch.setattr(subprocess, "run", run)
            request = client.request(
                "workflow-report", {**payload, "outcome": "completed", "summary": "Done"}
            )
            engine.tick()
            assert engine.state["requests"][request]["status"] == "rejected"
            assert "timed out" in engine.state["requests"][request]["error"]
            assert engine.active()["id"] == payload["execution_id"]

    def test_retry_attribution(self, repository, template) -> None:
        """A recaptured tree references the retried execution, preserving prior provenance separately."""
        from ctlrm.managed.storage import read_record

        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            engine.tick()
            complete(engine, client, "completed")
            payload = prepare(engine, client)
            previous = engine.active()["input"]["execution_id"]
            (repository / "source.txt").write_text("edited during review")
            client.request("stop", {"session_id": payload["session_id"]})
            engine.tick()
            client.request(
                "workflow-retry",
                {"execution_id": payload["execution_id"], "reason": "Review the changed version"},
            )
            engine.tick()
            incoming = engine.active()["input"]
            record = read_record(client.area.runtime / "artifacts" / f"{incoming['artifact']}.json")
            assert incoming["execution_id"] == record["execution_id"] == payload["execution_id"]
            assert incoming["previous_execution_id"] == previous

    def test_custom_approval_checks_version(self, repository, template) -> None:
        """Renaming an outcome leaves the step's explicit version policy intact."""
        template.tasks["review"].transitions = {"lgtm": "approve", "changes_requested": "implement"}
        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            engine.tick()
            complete(engine, client, "completed")
            payload = prepare(engine, client)
            client.request("workflow-ack", payload)
            engine.tick()
            (repository / "source.txt").write_text("changed")
            request = client.request(
                "workflow-report", {**payload, "outcome": "lgtm", "summary": "Good"}
            )
            engine.tick()
            assert engine.state["requests"][request]["status"] == "rejected"
            assert "artifact changed" in engine.state["requests"][request]["error"]

    def test_cancel_keeps_stopped_sessions(self, repository, template, monkeypatch) -> None:
        """Repeated cancellation cannot revive retirement of an already confirmed stopped host."""
        client, _ = WorkflowService.submit(repository, template, "Task", "Build")
        terminal = FakeTerminal()
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            payload = prepare(engine, client)
            client.request("stop", {"session_id": payload["session_id"]})
            engine.tick()

            def stop(*args):
                """A retired host must not be inspected or killed again."""
                raise AssertionError("stopped host was revisited")

            monkeypatch.setattr(terminal, "stop", stop)
            client.request("workflow-cancel", {})
            engine.tick()
            assert engine.state["run"]["status"] == "canceled"


class TestTaskInstructions:
    """Task prompts deliver their own instructions to the assigned participant."""

    def test_task_prompt(self, repository, template) -> None:
        """The actual assignment includes the task lens, separately from role instructions."""
        template.tasks["implement"].instructions = "Inspect authentication callers before editing."
        client, _ = WorkflowService.submit(repository, template, "Goal", "Improve authentication")
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            engine.tick()
            prepare(engine, client)
            body = engine.active()["message"]["body"]
            assert "Task instructions: Inspect authentication callers before editing." in body
            assert body in [item.body for item in client.area.room.read_inbox("implementer")]


class TestFreshReviewSessions:
    """A repeated review task receives a new conversation after the old process stops."""

    def test_replay_between_stop_and_replace(self, repository, template) -> None:
        """Replaying a fresh-review intent replaces once and keeps the new execution identity."""
        template.tasks["review"].fresh_session = True
        client, _ = WorkflowService.submit(repository, template, "Goal", "Implement")
        terminal = FakeTerminal()
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            complete(engine, client, "completed")
            owner = prepare(engine, client)
            previous = copy.deepcopy(engine.state["sessions"][owner["session_id"]])
            complete(engine, client, "changes_requested")
            complete(engine, client, "completed")
            execution_id = engine.active()["id"]
            assert engine.state["sessions"][previous["id"]]["status"] == "stopped"
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            current = engine.state["sessions"][previous["id"]]
            assert current["generation"] == previous["generation"] + 1
            assert current["native_id"] != previous["native_id"]
            client.ready(current["id"], current["generation"], current["native_id"])
            engine.tick()
            assert engine.active()["id"] == execution_id
            assert engine.active()["session_id"] == previous["id"]
            assert terminal.launches == 3


class TestFreshRetry:
    """A fresh-review execution cannot inherit context through explicit retry/resume."""

    def test_stop_retry_resume(self, repository, template) -> None:
        """The recorded previous native ID enforces freshness even after work is cleared."""
        template.tasks["review"].fresh_session = True
        client, _ = WorkflowService.submit(repository, template, "Goal", "Implement")
        terminal = FakeTerminal()
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            complete(engine, client, "completed")
            owner = prepare(engine, client)
            session = engine.state["sessions"][owner["session_id"]]
            old_native = session["native_id"]
            client.request("stop", {"session_id": session["id"]})
            engine.tick()
            client.request(
                "workflow-retry", {"execution_id": engine.active()["id"], "reason": "Fresh review"}
            )
            engine.tick()
            client.request("resume", {"session_id": session["id"]})
            engine.tick()
            session = engine.state["sessions"][session["id"]]
            client.ready(session["id"], session["generation"], session["native_id"])
            engine.tick()
            assert engine.state["sessions"][session["id"]]["status"] == "stopped"
            engine.tick()
            session = engine.state["sessions"][session["id"]]
            assert session["native_id"] != old_native
            client.ready(session["id"], session["generation"], session["native_id"])
            engine.tick()
            assert engine.active()["session_id"] == session["id"]


class TestInitialCommitWorkflow:
    """A direct bootstrap job can create the codebase's initial commit."""

    def test_unborn_submission(self, tmp_path, human_template) -> None:
        """A template without size policies needs HEAD only when it reports its output."""
        subprocess.run(
            ["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True
        )
        client, _ = WorkflowService.submit(tmp_path, human_template, "Initialize", "Create code")
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            engine.tick()
            assert engine.active()["task"] == "work"
            (tmp_path / "source.txt").write_text("initial\n")
            subprocess.run(
                ["git", "-C", str(tmp_path), "add", "source.txt"], check=True, capture_output=True
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(tmp_path),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.test",
                    "commit",
                    "-m",
                    "Initial",
                ],
                check=True,
                capture_output=True,
            )
            complete(engine, client, "done")
            assert engine.state["run"]["status"] == "completed"


class TestOutgoingMessages:
    """Outgoing task messages reuse supervisor ownership and crash recovery."""

    def test_generation_and_replay(self, repository, template) -> None:
        """Reject spoofed/stale senders and republish an accepted message after interrupted delivery."""
        from ctlrm.managed.storage import now
        from ctlrm.runtime.messages import MailboxMessage

        client, _ = WorkflowService.submit(repository, template, "Task", "Build it")
        terminal = FakeTerminal()
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            owner = prepare(engine, client)
            message = MailboxMessage(
                id="note",
                from_=owner["participant"],
                to="coordinator",
                kind="message",
                title="Findings",
                body="The full findings",
                status="new",
                created_at=now(),
            ).model_dump(by_alias=True)
            stale = client.request(
                "workflow-message", {**owner, "generation": 0, "message": message}
            )
            engine.tick()
            assert engine.state["requests"][stale]["status"] == "rejected"
            spoofed = client.request(
                "workflow-message", {**owner, "message": {**message, "from": "reviewer"}}
            )
            engine.tick()
            assert engine.state["requests"][spoofed]["status"] == "rejected"
            assert client.area.room.read_inbox("coordinator") == []
            engine.apply("workflow-message", {**owner, "message": message})
            engine.commit("test-accepted-before-delivery")
            assert client.area.room.read_inbox("coordinator") == []
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            engine.tick()
            inbox = client.area.room.read_inbox("coordinator")
            assert len(inbox) == 1 and inbox[0].execution_id == owner["execution_id"]
            assert inbox[0].body.strip() == "The full findings"
