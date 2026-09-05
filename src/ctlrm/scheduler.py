"""Pure workflow transition selection; persistence belongs to the supervisor."""

from ctlrm.runtime.workflows import WorkflowTemplate


def transition(workflow: WorkflowTemplate, step: str, outcome: str) -> str:
    """Return a step or explicit terminal, rejecting unknown outcomes.

    Parameters
    ----------
    workflow
        Immutable template snapshot.
    step
        Current step name.
    outcome
        Participant's structured business outcome.
    """
    if step not in workflow.tasks:
        raise ValueError("unknown active step")
    destination = workflow.tasks[step].transitions.get(outcome)
    if destination is None:
        raise ValueError(f"unknown outcome {outcome!r} for step {step!r}")
    return destination
