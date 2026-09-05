"""test scheduler.py."""

from ctlrm.runtime.workflows import WorkflowGraph
from ctlrm.scheduler import next_participant


class TestScheduler:
    """TestScheduler."""

    def test_routes_matching_edge_to_next_participant(self) -> None:
        """test routes matching edge to next participant."""
        workflow = WorkflowGraph.parse(
            "\nid: implement_review_fix\ntitle: Implement, review, and fix\nnodes:\n  implement:\n    participant: codex-impl\n  review:\n    participant: derrick\nedges:\n  - from: implement\n    to: review\n    when: result\n"
        )
        assert next_participant(workflow, "implement", "result") == "derrick"

    def test_done_edge_returns_none(self) -> None:
        """test done edge returns none."""
        workflow = WorkflowGraph.parse(
            "\nid: review\ntitle: Review\nnodes:\n  review:\n    participant: derrick\nedges:\n  - from: review\n    to: done\n    when: approved\n"
        )
        assert next_participant(workflow, "review", "approved") is None
