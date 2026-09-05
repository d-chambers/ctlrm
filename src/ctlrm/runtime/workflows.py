"""Versioned reusable workflow definitions and task/execution records."""

import copy
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from ctlrm.managed.providers import ProviderProfile
from ctlrm.runtime.documents import load_yaml
from ctlrm.runtime.paths import validate_path_component

TERMINALS = {"terminal:completed", "terminal:rejected", "terminal:failed"}


class Record(BaseModel):
    """Reject misspelled fields instead of silently dropping workflow policy."""

    model_config = ConfigDict(extra="forbid")


class WorkflowRole(Record):
    """Reusable role instructions and its provider profile, if automated."""

    kind: Literal["agent", "human"]
    profile: str | None = None
    instructions: str = Field(default="", max_length=16384)

    @model_validator(mode="after")
    def validate_profile(self) -> "WorkflowRole":
        """Require a profile exactly for agent roles."""
        if (self.kind == "agent") != bool(self.profile):
            raise ValueError("agent roles require a profile; human roles cannot have one")
        return self


class TaskDefinition(Record):
    """A reusable assignment with task-specific instructions and explicit outcomes."""

    role: str
    instructions: str = Field(default="", max_length=16384)
    verify_input: bool
    optional: bool = False
    fresh_session: bool = False
    requires_pr: list[str] = Field(default_factory=list, max_length=32)
    max_executions: int | None = Field(default=None, ge=1, le=1000)
    specialist_tasks: list[str] = Field(default_factory=list, max_length=8)
    specialist_above_lines: dict[str, int] = Field(default_factory=dict, max_length=8)
    verified_outcomes: list[str] = Field(default_factory=list, max_length=32)
    transitions: dict[str, str] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_outcome_policy(self) -> "TaskDefinition":
        """Named verification outcomes must be unique declared transitions."""
        if (
            len(set(self.verified_outcomes)) != len(self.verified_outcomes)
            or set(self.verified_outcomes) - self.transitions.keys()
        ):
            raise ValueError("verified_outcomes must name unique declared outcomes")
        return self

    def checks_input(self, outcome: str) -> bool:
        """Select all-outcome or explicitly named input verification."""
        return self.verify_input or outcome in self.verified_outcomes


class ExecutionLimits(Record):
    """Bound intentional cycles and explicit retries."""

    max_step_executions: int = Field(default=12, ge=1, le=1000)
    max_specialist_executions: int = Field(default=3, ge=0, le=32)


class WorkflowTemplate(Record):
    """A validated graph independent of mutable participants and run state."""

    schema_version: Literal[1] = 1
    name: str
    entry: str
    limits: ExecutionLimits = Field(default_factory=ExecutionLimits)
    profiles: dict[str, ProviderProfile]
    roles: dict[str, WorkflowRole] = Field(min_length=1, max_length=32)
    tasks: dict[str, TaskDefinition] = Field(
        min_length=1, max_length=128, validation_alias=AliasChoices("tasks", "steps")
    )

    @property
    def steps(self) -> dict[str, TaskDefinition]:
        """Expose the historical Python name without duplicating task definitions."""
        return self.tasks

    @classmethod
    def parse(cls, text: str) -> "WorkflowTemplate":
        """Parse bounded YAML, rejecting duplicate keys and invalid graphs."""
        if len(text.encode()) > 1048576:
            raise ValueError("template exceeds 1 MiB")
        try:
            return cls.model_validate(load_yaml(text))
        except RecursionError as error:
            raise ValueError("template nesting is too deep") from error

    @classmethod
    def from_snapshot(cls, value: dict) -> "WorkflowTemplate":
        """Read earlier immutable snapshots using their original approval-name policy."""
        value = copy.deepcopy(value)
        for step in value.get("tasks", value.get("steps", {})).values():
            if "verify_input" not in step:
                step["verify_input"] = False
                step["verified_outcomes"] = (
                    ["approved"] if "approved" in step.get("transitions", {}) else []
                )
        return cls.model_validate(value)

    @model_validator(mode="after")
    def validate_graph(self) -> "WorkflowTemplate":
        """Reject unresolved edges, unreachable steps, and graphs without an exit."""
        for key in [self.name, *self.roles, *self.steps, *self.profiles]:
            validate_path_component(key, label="workflow identifier", max_length=64)
            if key == "coordinator":
                raise ValueError("coordinator is reserved")
        for role in self.roles.values():
            if role.kind == "agent" and role.profile not in self.profiles:
                raise ValueError(f"unknown profile: {role.profile}")
        if self.entry not in self.steps or self.steps[self.entry].optional:
            raise ValueError("unknown entry step")
        for name, step in self.steps.items():
            if (
                len(set(step.requires_pr)) != len(step.requires_pr)
                or set(step.requires_pr) - step.transitions.keys()
            ):
                raise ValueError("PR-required outcomes must be unique declared outcomes")
            if len(set(step.specialist_tasks)) != len(step.specialist_tasks):
                raise ValueError("specialist tasks must be unique")
            if set(step.specialist_above_lines) - set(step.specialist_tasks) or any(
                type(n) is not int or not 0 <= n <= 1000000
                for n in step.specialist_above_lines.values()
            ):
                raise ValueError("specialist thresholds must be bounded counts for permitted tasks")
            if any(
                target not in self.tasks or not self.tasks[target].optional
                for target in step.specialist_tasks
            ):
                raise ValueError("specialist requests must reference defined optional tasks")
            if step.optional:
                if (
                    not step.verify_input
                    or step.specialist_tasks
                    or set(step.transitions) != {"approved", "changes_requested"}
                    or step.transitions["approved"] != "terminal:return"
                ):
                    raise ValueError(
                        "optional review tasks must verify input, cannot recurse, and must approve with terminal:return"
                    )
                if (
                    step.transitions["changes_requested"] not in self.tasks
                    or self.tasks[step.transitions["changes_requested"]].optional
                ):
                    raise ValueError("specialist findings must return to a normal task")
            elif "terminal:return" in step.transitions.values():
                raise ValueError("only optional tasks may return to their requester")
            if step.specialist_tasks and "approved" not in step.transitions:
                raise ValueError("specialist requesters require an approved outcome")
            if step.role not in self.roles:
                raise ValueError(f"unknown role: {step.role}")
            for outcome, destination in step.transitions.items():
                validate_path_component(outcome, label="outcome", max_length=64)
                if (
                    destination not in self.steps
                    and destination not in TERMINALS
                    and destination != "terminal:return"
                ):
                    raise ValueError(f"unknown transition destination: {destination}")
                if destination in self.tasks and self.tasks[destination].optional:
                    raise ValueError(
                        "optional tasks are entered only through bounded specialist requests"
                    )
        seen, pending = set(), [self.entry]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            if current in self.steps:
                pending.extend(self.steps[current].transitions.values())
                pending.extend(self.steps[current].specialist_tasks)
        if set(self.steps) - seen:
            raise ValueError("workflow contains unreachable steps")
        if not seen & TERMINALS:
            raise ValueError("workflow has no reachable terminal")
        escapable = {*TERMINALS, "terminal:return"}
        while True:
            added = {
                name
                for name, step in self.steps.items()
                if set(step.transitions.values()) & escapable
            } - escapable
            if not added:
                break
            escapable.update(added)
        if set(self.steps) - escapable:
            raise ValueError("every step must have a path to a terminal")
        return self


class JobInput(Record):
    """Immutable job goal shared across all task executions in a workflow run."""

    id: str
    title: str = Field(min_length=1, max_length=256)
    instructions: str = Field(min_length=1, max_length=65536)

    @field_validator("title", "instructions")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """Reject blank task text before publishing any immutable area files."""
        if not value.strip() or len(value.encode()) > 65536:
            raise ValueError("task text must be nonblank and at most 65536 bytes")
        return value


class TaskExecution(Record):
    """One durable visit; repeated visits always have distinct IDs."""

    id: str
    task: str = Field(validation_alias=AliasChoices("task", "step"), serialization_alias="step")
    participant: str
    status: Literal["waiting", "pending", "acknowledged", "completed", "abandoned"] = "waiting"
    session_id: str | None = None
    generation: int | None = None
    reset_generation: int | None = None
    message: dict | None = None
    input: dict = Field(default_factory=dict)
    outcome: str | None = None
    summary: str | None = None
    artifact: str | None = None


class WorkflowRun(Record):
    """Replayable execution progress, separate from provider process liveness."""

    id: str
    job_id: str = Field(
        validation_alias=AliasChoices("job_id", "task_id"), serialization_alias="task_id"
    )
    status: str = "running"
    active: str | None = None
    executions: list[dict] = Field(default_factory=list)
    reason: str | None = None


# Historical imports and serialized journal keys remain readable.
WorkflowStep = TaskDefinition
StepExecution = TaskExecution
Task = JobInput
