"""Transition selection distinguishes completion from unknown outcomes."""

import pytest

from ctlrm.runtime.workflows import WorkflowTemplate
from ctlrm.scheduler import transition


class TestTransition:
    """Explicit terminal results never hide malformed participant reports."""

    def test_outcome(self) -> None:
        """Known outcomes select destinations; unknown ones raise."""
        workflow = WorkflowTemplate.model_validate(
            {
                "name": "approval",
                "entry": "review",
                "profiles": {},
                "roles": {"owner": {"kind": "human"}},
                "tasks": {
                    "review": {
                        "role": "owner",
                        "verify_input": True,
                        "transitions": {"approved": "terminal:completed", "again": "review"},
                    }
                },
            }
        )
        assert transition(workflow, "review", "approved") == "terminal:completed"
        assert transition(workflow, "review", "again") == "review"
        with pytest.raises(ValueError, match="unknown outcome"):
            transition(workflow, "review", "garbage")
