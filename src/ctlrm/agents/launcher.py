"""Agent launch requests and tmux-backed launcher implementation."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from ctlrm.models import Participant, Project, Provider, TmuxTarget
from ctlrm.registry import Registry
from ctlrm.runtime import ProjectRuntime
from ctlrm.communication.tmux import CommandRunner, TmuxCommand


class AgentLaunchRequest(BaseModel):
    """User request to launch a new CLI agent."""

    id: str
    name: str
    role: str
    provider: Provider


def provider_command(provider: Provider) -> list[str]:
    """Return the shell command used to start an agent provider.

    Parameters
    ----------
    provider
        Agent provider to launch.

    Returns
    -------
    list[str]
        Command and arguments suitable for tmux.
    """
    return {
        Provider.CODEX: ["codex"],
        Provider.CLAUDE: ["claude"],
        Provider.PI: ["pi"],
    }[provider]


class AgentLauncher:
    """Launch CLI agents into tmux panes and persist them."""

    def __init__(self, runner: CommandRunner, registry_path: Path | None = None) -> None:
        """Initialize the launcher with command execution and storage."""
        self.runner = runner
        self.registry_path = registry_path

    def launch(self, project: Project, request: AgentLaunchRequest) -> Participant:
        """Launch an agent and attach it to a project.

        Parameters
        ----------
        project
            Project that should receive the agent.
        request
            Agent launch details from the UI.

        Returns
        -------
        Participant
            Participant record for the launched agent.
        """
        if any(item.id == request.id for item in project.participants):
            raise ValueError(f"participant already registered: {request.id}")
        workdir = project.root
        session = project.tmux_session or project.id
        runtime = ProjectRuntime(project.root)
        runtime.init_project()
        runtime.init_participant(request.id)

        self.runner.run(TmuxCommand.new_session(session, str(workdir)))
        pane = self.runner.run(
            TmuxCommand.split_window(session, str(workdir), provider_command(request.provider))
        ).strip()

        participant = Participant.agent(
            request.id,
            request.name,
            request.role,
            request.provider,
            Path(workdir),
            TmuxTarget(session=session, window="agents", pane=pane),
        )
        self._save_registry(project, participant)
        project.participants.append(participant)
        return participant

    def _save_registry(self, project: Project, participant: Participant) -> None:
        """Merge a participant into the latest persisted registry when configured."""
        if self.registry_path is None:
            return
        Registry.add_participant(self.registry_path, project, participant)
