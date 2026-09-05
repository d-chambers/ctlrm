"""Workflow scheduling helpers."""

from ctlrm.runtime.workflows import WorkflowGraph


def next_participant(workflow: WorkflowGraph, current_node: str, event: str) -> str | None:
    """Return the participant selected by a workflow transition.

    Parameters
    ----------
    workflow
        Workflow graph to inspect.
    current_node
        Node where the event occurred.
    event
        Transition event name.

    Returns
    -------
    str | None
        Next participant id, or ``None`` when the workflow is done or no edge matches.
    """
    for edge in workflow.edges:
        if edge.from_ == current_node and edge.when == event:
            if edge.to == "done":
                return None
            return workflow.nodes[edge.to].participant
    return None
