import pytest

from ctlrm.runtime.workflows import WorkflowGraph


WORKFLOW = """
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
  - from: review
    to: done
    when: approved
"""


def test_parses_workflow_with_human_participant_node() -> None:
    workflow = WorkflowGraph.parse(WORKFLOW)

    assert workflow.nodes["review"].participant == "derrick"


def test_rejects_unknown_edge_node() -> None:
    with pytest.raises(ValueError, match="unknown to node"):
        WorkflowGraph.parse(
            """
id: broken
title: Broken
nodes:
  implement:
    participant: codex-impl
edges:
  - from: implement
    to: review
    when: result
"""
        )
