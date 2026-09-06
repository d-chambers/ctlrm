"""Shared request guards reject work before operation-specific ownership or mutation."""

import copy

import pytest

from ctlrm.managed.workflows import WorkflowEngine, WorkflowService
from ctlrm.runtime.workflows import WorkflowTemplate


@pytest.fixture
def workflow(repository):
    """Start a minimal real workflow under its existing single-writer lock."""
    template = WorkflowTemplate.parse("""name: guarded
entry: work
profiles: {}
roles: {owner: {kind: human}}
tasks:
  work:
    role: owner
    verify_input: false
    transitions: {done: 'terminal:completed'}
""")
    client, _ = WorkflowService.submit(repository, template, "Guard checks", "Check boundaries")
    with client.area.journal.writer():
        engine = WorkflowEngine(client.area)
        engine.tick()
        yield engine


class TestSessionGuards:
    """Direct dispatch has the same pause protection as journal-request polling."""

    @pytest.mark.parametrize(
        "kind",
        [
            "launch",
            "ready",
            "prompt",
            "interact",
            "resume",
            "replace",
            "acknowledge",
            "disposition",
        ],
    )
    def test_paused_work(self, managed, kind) -> None:
        """Every work-producing entry point rejects a paused area before touching its state."""
        _, engine, _, _ = managed
        engine.state["paused"] = "worktree identity changed"
        before = copy.deepcopy(engine.state)
        with pytest.raises(ValueError, match="area is paused"):
            engine.apply(kind, {})
        assert engine.state == before

    def test_paused_stop(self, managed) -> None:
        """An explicitly owned stop remains possible when further work is forbidden."""
        _, engine, _, _ = managed
        session = next(iter(engine.state["sessions"].values()))
        engine.state["paused"] = "worktree identity changed"
        engine.apply("stop", {"session_id": session["id"]})
        assert session["status"] == "stopping"
        engine.apply("supervisor-stop", {})
        assert engine.state["shutdown"]

    def test_unknown_operation(self, managed) -> None:
        """Unknown operations fail without resolving a session or mutating accepted state."""
        _, engine, _, _ = managed
        before = copy.deepcopy(engine.state)
        with pytest.raises(ValueError, match="unknown request kind"):
            engine.apply("not-an-operation", {})
        assert engine.state == before


class TestWorkflowGuards:
    """Retirement and cancellation apply to both inherited and workflow-specific requests."""

    @pytest.mark.parametrize(
        "kind",
        [
            "workflow-start",
            "workflow-retry",
            "workflow-ack",
            "workflow-report",
            "workflow-message",
            "launch",
            "interact",
            "disposition",
        ],
    )
    @pytest.mark.parametrize("state", ["retiring", "retired", "canceled"])
    def test_closed_job(self, workflow, kind, state) -> None:
        """A closed job rejects operations before any handler can revive it."""
        if state == "canceled":
            workflow.apply("workflow-cancel", {})
            workflow.advance()
            assert workflow.state["run"]["status"] == "canceled"
        else:
            workflow.state[state] = True
        before = copy.deepcopy(workflow.state)
        with pytest.raises(ValueError, match="retiring or archived|workflow is canceled"):
            workflow.apply(kind, {})
        assert workflow.state == before

    def test_manual_launch(self, workflow) -> None:
        """The public launch name cannot sidestep workflow role scheduling."""
        with pytest.raises(ValueError, match="launched by the active step"):
            workflow.apply("launch", {"participant": "owner"})
        assert workflow.state["sessions"] == {}
