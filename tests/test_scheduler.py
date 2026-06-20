from ctlrm.runtime.workflows import WorkflowGraph
from ctlrm.scheduler import next_participant


def test_routes_matching_edge_to_next_participant() -> None:
    workflow = WorkflowGraph.parse(
        """
id: implement_review_fix
title: Implement, review, and fix
nodes:
  implement:
    participant: codex-impl
  review:
    participant: derrick
edges:
  - from: implement
    to: review
    when: result
"""
    )

    assert next_participant(workflow, "implement", "result") == "derrick"


def test_done_edge_returns_none() -> None:
    workflow = WorkflowGraph.parse(
        """
id: review
title: Review
nodes:
  review:
    participant: derrick
edges:
  - from: review
    to: done
    when: approved
"""
    )

    assert next_participant(workflow, "review", "approved") is None
