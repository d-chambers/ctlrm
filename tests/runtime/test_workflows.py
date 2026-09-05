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
    "tasks": {
        "approve": {
            "role": "owner",
            "verify_input": True,
            "transitions": {"approved": "terminal:completed"},
        }
    },
}


class TestTemplate:
    """A graph must have explicit outcomes, valid bindings, and a reachable exit."""

    def test_single_human(self) -> None:
        """A one-task workflow needs neither a synthetic agent nor a profile."""
        assert WorkflowTemplate.model_validate(HUMAN).entry == "approve"

    @pytest.mark.parametrize("change", ["entry", "destination", "role", "unreachable", "no_exit"])
    def test_invalid_graph(self, change) -> None:
        """Broken references and permanently cycling definitions fail at validation."""
        data = copy.deepcopy(HUMAN)
        if change == "entry":
            data["entry"] = "missing"
        elif change == "destination":
            data["tasks"]["approve"]["transitions"]["approved"] = "missing"
        elif change == "role":
            data["tasks"]["approve"]["role"] = "missing"
        elif change == "unreachable":
            data["tasks"]["unused"] = copy.deepcopy(data["tasks"]["approve"])
        else:
            data["tasks"]["approve"]["transitions"]["approved"] = "approve"
        with pytest.raises(ValueError):
            WorkflowTemplate.model_validate(data)

    @pytest.mark.parametrize("text", ["name: a\nname: b", "roles: {true: {kind: human}}"])
    def test_ambiguous_yaml(self, text) -> None:
        """Duplicate and implicitly boolean YAML keys cannot change graph meaning."""
        with pytest.raises(ValueError):
            WorkflowTemplate.parse(text)


class TestTrapCycle:
    """Every reachable task must retain a possible route to a terminal."""

    def test_inescapable_branch(self) -> None:
        """A terminal on one branch cannot validate another branch that loops forever."""
        data = copy.deepcopy(HUMAN)
        data["tasks"]["approve"]["transitions"]["retry"] = "trap"
        data["tasks"]["trap"] = {
            "role": "owner",
            "verify_input": True,
            "transitions": {"again": "trap"},
        }
        with pytest.raises(ValueError, match="every task"):
            WorkflowTemplate.model_validate(data)


class TestExplicitVerificationPolicy:
    """Every task declares its input verification policy explicitly."""

    def test_policy_required(self) -> None:
        """New source templates cannot silently infer review policy from an outcome name."""
        data = copy.deepcopy(HUMAN)
        del data["tasks"]["approve"]["verify_input"]
        with pytest.raises(ValueError, match="verify_input"):
            WorkflowTemplate.model_validate(data)


class TestExamples:
    """The documented reusable YAML files must remain valid source templates."""

    @pytest.mark.parametrize("name", ["ship", "single-agent", "implement-review"])
    def test_source_template(self, name) -> None:
        """Validate examples through the same strict parser used at submission."""
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "src/ctlrm/web/templates" / f"{name}.yaml"
        template = WorkflowTemplate.parse(path.read_text())
        if name == "ship":
            assert (
                template.limits.max_specialist_executions
                >= template.tasks["implement"].max_executions
            )
