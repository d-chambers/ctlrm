from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field


class ParticipantKind(str, Enum):
    AGENT = "agent"
    HUMAN = "human"


class Provider(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"
    PI = "pi"


class ParticipantState(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    MISSING = "missing"
    UNMANAGED = "unmanaged"


class TmuxTarget(BaseModel):
    session: str
    window: str
    pane: str

    @property
    def selector(self) -> str:
        return f"{self.session}:{self.window}.{self.pane}"


class Participant(BaseModel):
    id: str
    name: str
    role: str
    kind: ParticipantKind
    provider: Provider | None = None
    workdir: Path
    tmux_target: TmuxTarget | None = None
    state: ParticipantState = ParticipantState.IDLE

    @classmethod
    def human(cls, id: str, name: str, role: str, workdir: Path) -> Participant:
        return cls(id=id, name=name, role=role, kind=ParticipantKind.HUMAN, workdir=workdir)

    @classmethod
    def agent(
        cls,
        id: str,
        name: str,
        role: str,
        provider: Provider,
        workdir: Path,
        tmux_target: TmuxTarget | None = None,
    ) -> Participant:
        return cls(
            id=id,
            name=name,
            role=role,
            kind=ParticipantKind.AGENT,
            provider=provider,
            workdir=workdir,
            tmux_target=tmux_target,
        )


class Project(BaseModel):
    id: str
    name: str
    root: Path
    tmux_session: str | None = None
    participants: list[Participant] = Field(default_factory=list)
