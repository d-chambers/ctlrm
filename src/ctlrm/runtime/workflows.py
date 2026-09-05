"""Versioned reusable workflow definitions and task/execution records."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class WorkflowStep(Record):
    """A role visit with explicitly named business outcomes."""

    role: str
    transitions: dict[str, str] = Field(min_length=1, max_length=32)


class ExecutionLimits(Record):
    """Bound intentional cycles and explicit retries."""

    max_step_executions: int = Field(default=12, ge=1, le=1000)


class WorkflowTemplate(Record):
    """A validated graph independent of mutable participants and run state."""

    schema_version: Literal[1] = 1
    name: str
    entry: str
    limits: ExecutionLimits = Field(default_factory=ExecutionLimits)
    profiles: dict[str, ProviderProfile]
    roles: dict[str, WorkflowRole] = Field(min_length=1, max_length=32)
    steps: dict[str, WorkflowStep] = Field(min_length=1, max_length=128)

    @classmethod
    def parse(cls, text: str) -> "WorkflowTemplate":
        """Parse bounded YAML, rejecting duplicate keys and invalid graphs."""
        if len(text.encode()) > 1048576:
            raise ValueError("template exceeds 1 MiB")
        try:
            return cls.model_validate(load_yaml(text))
        except RecursionError as error:
            raise ValueError("template nesting is too deep") from error

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
        if self.entry not in self.steps:
            raise ValueError("unknown entry step")
        for step in self.steps.values():
            if step.role not in self.roles:
                raise ValueError(f"unknown role: {step.role}")
            for outcome, destination in step.transitions.items():
                validate_path_component(outcome, label="outcome", max_length=64)
                if destination not in self.steps and destination not in TERMINALS:
                    raise ValueError(f"unknown transition destination: {destination}")
        seen, pending = set(), [self.entry]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            if current in self.steps:
                pending.extend(self.steps[current].transitions.values())
        if set(self.steps) - seen:
            raise ValueError("workflow contains unreachable steps")
        if not seen & TERMINALS:
            raise ValueError("workflow has no reachable terminal")
        return self


class Task(Record):
    """Immutable user work shared across every visit in a workflow run."""

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


class StepExecution(Record):
    """One durable visit; repeated visits always have distinct IDs."""

    id: str
    step: str
    participant: str
    status: Literal["waiting", "pending", "acknowledged", "completed", "abandoned"] = "waiting"
    session_id: str | None = None
    generation: int | None = None
    message: dict | None = None
    input: dict = Field(default_factory=dict)
    outcome: str | None = None
    summary: str | None = None
    artifact: str | None = None


class WorkflowRun(Record):
    """Replayable execution progress, separate from provider process liveness."""

    id: str
    task_id: str
    status: str = "running"
    active: str | None = None
    executions: list[dict] = Field(default_factory=list)
    reason: str | None = None
