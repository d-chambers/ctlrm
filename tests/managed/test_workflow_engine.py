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
