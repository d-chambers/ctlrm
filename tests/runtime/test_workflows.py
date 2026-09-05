"""Reusable graph validation rejects ambiguous or unrouteable definitions."""

import copy

import pytest

from ctlrm.runtime.workflows import WorkflowTemplate

HUMAN = {
    "schema_version": 1,
    "name": "approval",
    "entry": "approve",
    "profiles": {},
    "roles": {"owner": {"kind": "human"}},
    "steps": {"approve": {"role": "owner", "transitions": {"approved": "terminal:completed"}}},
}


class TestTemplate:
    """A graph must have explicit outcomes, valid bindings, and a reachable exit."""

    def test_single_human(self) -> None:
        """A one-step workflow needs neither a synthetic agent nor a profile."""
        assert WorkflowTemplate.model_validate(HUMAN).entry == "approve"

    @pytest.mark.parametrize("change", ["entry", "destination", "role", "unreachable", "no_exit"])
    def test_invalid_graph(self, change) -> None:
        """Broken references and permanently cycling definitions fail at validation."""
        data = copy.deepcopy(HUMAN)
        if change == "entry":
            data["entry"] = "missing"
        elif change == "destination":
            data["steps"]["approve"]["transitions"]["approved"] = "missing"
        elif change == "role":
            data["steps"]["approve"]["role"] = "missing"
        elif change == "unreachable":
            data["steps"]["unused"] = copy.deepcopy(data["steps"]["approve"])
        else:
            data["steps"]["approve"]["transitions"]["approved"] = "approve"
        with pytest.raises(ValueError):
            WorkflowTemplate.model_validate(data)

    @pytest.mark.parametrize("text", ["name: a\nname: b", "roles: {true: {kind: human}}"])
    def test_ambiguous_yaml(self, text) -> None:
        """Duplicate and implicitly boolean YAML keys cannot change graph meaning."""
        with pytest.raises(ValueError):
            WorkflowTemplate.parse(text)


class TestTrapCycle:
    """Every reachable step must retain a possible route to a terminal."""

    def test_inescapable_branch(self) -> None:
        """A terminal on one branch cannot validate another branch that loops forever."""
        data = copy.deepcopy(HUMAN)
        data["steps"]["approve"]["transitions"]["retry"] = "trap"
        data["steps"]["trap"] = {"role": "owner", "transitions": {"again": "trap"}}
        with pytest.raises(ValueError, match="every step"):
            WorkflowTemplate.model_validate(data)
