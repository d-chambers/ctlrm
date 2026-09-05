"""test workflows.py."""

import pytest
from ctlrm.runtime.workflows import WorkflowGraph

WORKFLOW = "\nid: implement_review_fix\ntitle: Implement, review, and fix\nnodes:\n  implement:\n    participant: codex-impl\n  review:\n    participant: derrick\nedges:\n  - from: implement\n    to: review\n    when: result\n  - from: review\n    to: done\n    when: approved\n"


class TestWorkflows:
    """TestWorkflows."""

    def test_parses_workflow_with_human_participant_node(self) -> None:
        """test parses workflow with human participant node."""
        workflow = WorkflowGraph.parse(WORKFLOW)
        assert workflow.nodes["review"].participant == "derrick"

    def test_rejects_unknown_edge_node(self) -> None:
        """test rejects unknown edge node."""
        with pytest.raises(ValueError, match="unknown to node"):
            WorkflowGraph.parse(
                "\nid: broken\ntitle: Broken\nnodes:\n  implement:\n    participant: codex-impl\nedges:\n  - from: implement\n    to: review\n    when: result\n"
            )
