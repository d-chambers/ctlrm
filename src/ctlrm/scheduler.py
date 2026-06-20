from ctlrm.runtime.workflows import WorkflowGraph


def next_participant(workflow: WorkflowGraph, current_node: str, event: str) -> str | None:
    for edge in workflow.edges:
        if edge.from_ == current_node and edge.when == event:
            if edge.to == "done":
                return None
            return workflow.nodes[edge.to].participant
    return None
