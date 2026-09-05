"""test launcher.py."""

from pathlib import Path
from ctlrm.agents.launcher import AgentLaunchRequest, AgentLauncher, provider_command
from ctlrm.models import ParticipantKind, Project, Provider
from ctlrm.communication.tmux import TmuxCommand


class RecordingRunner:
    """RecordingRunner."""

    def __init__(self) -> None:
        """init  ."""
        self.commands: list[TmuxCommand] = []

    def run(self, command: TmuxCommand) -> str:
        """run."""
        self.commands.append(command)
        if command.args[:2] == ["split-window", "-P"]:
            return "%42\n"
        return ""


class TestLauncher:
    """TestLauncher."""

    def test_provider_command_defaults(self) -> None:
        """test provider command defaults."""
        assert provider_command(Provider.CODEX) == ["codex"]
        assert provider_command(Provider.CLAUDE) == ["claude"]
        assert provider_command(Provider.PI) == ["pi"]

    def test_launch_agent_creates_tmux_pane_participant_and_runtime(self, tmp_path: Path) -> None:
        """test launch agent creates tmux pane participant and runtime."""
        runner = RecordingRunner()
        launcher = AgentLauncher(runner)
        project = Project(id="ctlrm", name="ctlrm", root=tmp_path, tmux_session="ctlrm")
        request = AgentLaunchRequest(
            id="codex-impl", name="Codex", role="implementer", provider=Provider.CODEX
        )
        participant = launcher.launch(project, request)
        assert participant.kind is ParticipantKind.AGENT
        assert participant.provider is Provider.CODEX
        assert participant.tmux_target is not None
        assert participant.tmux_target.pane == "%42"
        assert project.participants == [participant]
        assert (tmp_path / ".ctlrm" / "participants" / "codex-impl" / "inbox").is_dir()
        assert runner.commands[0].args[:4] == ["new-session", "-d", "-A", "-s"]
        assert runner.commands[1].args[:4] == ["split-window", "-P", "-F", "#{pane_id}"]
        assert runner.commands[1].args[-1] == "codex"

    def test_launch_agent_persists_registry_when_path_is_provided(self, tmp_path: Path) -> None:
        """test launch agent persists registry when path is provided."""
        runner = RecordingRunner()
        registry_path = tmp_path / "registry.toml"
        launcher = AgentLauncher(runner, registry_path=registry_path)
        project = Project(id="ctlrm", name="ctlrm", root=tmp_path, tmux_session="ctlrm")
        launcher.launch(
            project,
            AgentLaunchRequest(
                id="claude-review", name="Claude", role="reviewer", provider=Provider.CLAUDE
            ),
        )
        registry_text = registry_path.read_text()
        assert 'id = "claude-review"' in registry_text
        assert 'provider = "claude"' in registry_text
