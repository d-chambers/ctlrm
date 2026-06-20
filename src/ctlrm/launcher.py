from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from ctlrm.models import Participant, Project, Provider, TmuxTarget
from ctlrm.registry import Registry
from ctlrm.runtime import ProjectRuntime
from ctlrm.tmux import CommandRunner, TmuxCommand


class AgentLaunchRequest(BaseModel):
    id: str
    name: str
    role: str
    provider: Provider


def provider_command(provider: Provider) -> list[str]:
    return {
        Provider.CODEX: ["codex"],
        Provider.CLAUDE: ["claude"],
        Provider.PI: ["pi"],
    }[provider]


class AgentLauncher:
    def __init__(self, runner: CommandRunner, registry_path: Path | None = None) -> None:
        self.runner = runner
        self.registry_path = registry_path

    def launch(self, project: Project, request: AgentLaunchRequest) -> Participant:
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
        project.participants.append(participant)
        self._save_registry(project)
        return participant

    def _save_registry(self, project: Project) -> None:
        if self.registry_path is None:
            return
        Registry(projects=[project]).save_atomic(self.registry_path)
