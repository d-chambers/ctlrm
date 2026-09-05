"""Domain models for ctlrm projects and participants."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field


class ParticipantKind(str, Enum):
    """Supported participant categories."""

    AGENT = "agent"
    HUMAN = "human"


class Provider(str, Enum):
    """Supported CLI agent providers."""

    CODEX = "codex"
    CLAUDE = "claude"
    PI = "pi"


class ParticipantState(str, Enum):
    """Lifecycle states displayed for a participant."""

    IDLE = "idle"
    WORKING = "working"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    MISSING = "missing"
    UNMANAGED = "unmanaged"


class TmuxTarget(BaseModel):
    """Serializable tmux pane target."""

    session: str
    window: str
    pane: str

    @property
    def selector(self) -> str:
        """Return the tmux selector string for this target."""
        if self.pane.startswith("%"):
            return self.pane
        return f"{self.session}:{self.window}.{self.pane}"


class Participant(BaseModel):
    """Project participant, either human or CLI-backed agent."""

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
        """Create a human participant.

        Parameters
        ----------
        id
            Stable participant identifier.
        name
            Display name.
        role
            Workflow role description.
        workdir
            Working directory for the participant.

        Returns
        -------
        Participant
            Human participant without provider or tmux metadata.
        """
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
        """Create a CLI agent participant.

        Parameters
        ----------
        id
            Stable participant identifier.
        name
            Display name.
        role
            Workflow role description.
        provider
            CLI provider used to launch the agent.
        workdir
            Working directory for the participant.
        tmux_target
            Optional tmux target backing the participant.

        Returns
        -------
        Participant
            Agent participant with provider metadata.
        """
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
    """A ctlrm-managed project workspace."""

    id: str
    name: str
    root: Path
    tmux_session: str | None = None
    participants: list[Participant] = Field(default_factory=list)
