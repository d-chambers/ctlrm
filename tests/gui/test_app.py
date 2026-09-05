"""test app.py."""

import pytest
from pathlib import Path
from ctlrm.gui.app import CtlrmApp
from ctlrm.agents.launcher import AgentLaunchRequest
from ctlrm.models import Participant, Project, Provider
from ctlrm.workspace import Workspace


class FakeLauncher:
    """FakeLauncher."""

    def __init__(self) -> None:
        """init  ."""
        self.requests = []

    def launch(self, project, request):
        """launch."""
        self.requests.append((project, request))
        participant = Participant.agent(
            request.id, request.name, request.role, request.provider, project.root
        )
        project.participants.append(participant)
        return participant


class TestApp:
    """TestApp."""

    @pytest.mark.asyncio
    async def test_app_renders_human_participant(self) -> None:
        """test app renders human participant."""
        project = Project(
            id="ctlrm",
            name="ctlrm",
            root=Path("/repo"),
            participants=[Participant.human("derrick", "Derrick", "product-owner", Path("/repo"))],
        )
        app = CtlrmApp(Workspace.from_projects([project]))
        async with app.run_test() as pilot:
            assert "Derrick" in str(pilot.app.query_one("#participants").render())

    def test_quit_binding_is_ctrl_q_only(self) -> None:
        """test quit binding is ctrl q only."""
        bindings = {binding[0] for binding in CtlrmApp.BINDINGS}
        assert "ctrl+q" in bindings
        assert "q" not in bindings

    def test_focus_bindings_are_ctrl_shift_arrows(self) -> None:
        """test focus bindings are ctrl shift arrows."""
        bindings = {binding[0] for binding in CtlrmApp.BINDINGS}
        assert "ctrl+shift+left" in bindings
        assert "ctrl+shift+right" in bindings
        assert "ctrl+shift+up" in bindings
        assert "ctrl+shift+down" in bindings

    @pytest.mark.asyncio
    async def test_app_renders_resize_handles(self) -> None:
        """test app renders resize handles."""
        app = CtlrmApp(Workspace.from_projects([]))
        async with app.run_test() as pilot:
            assert pilot.app.query_one("#split-left")
            assert pilot.app.query_one("#split-right")
            assert pilot.app.query_one("#split-center")

    @pytest.mark.asyncio
    async def test_active_panel_title_marker_moves_with_focus(self) -> None:
        """test active panel title marker moves with focus."""
        app = CtlrmApp(Workspace.from_projects([]))
        async with app.run_test() as pilot:
            assert pilot.app.query_one("#participants").border_title == "● Participants"
            assert pilot.app.query_one("#file").border_title == "Current File"
            await pilot.press("ctrl+shift+right")
            assert pilot.app.query_one("#participants").border_title == "Participants"
            assert pilot.app.query_one("#file").border_title == "● Current File"

    def test_new_agent_binding_is_ctrl_n(self) -> None:
        """test new agent binding is ctrl n."""
        bindings = {binding[0] for binding in CtlrmApp.BINDINGS}
        assert "ctrl+n" in bindings

    @pytest.mark.asyncio
    async def test_launch_request_updates_roster(self) -> None:
        """test launch request updates roster."""
        project = Project(id="ctlrm", name="ctlrm", root=Path("/repo"), participants=[])
        launcher = FakeLauncher()
        app = CtlrmApp(Workspace.from_projects([project]), launcher=launcher)
        async with app.run_test() as pilot:
            app.launch_agent_from_request(
                AgentLaunchRequest(
                    id="codex-impl", name="Codex", role="implementer", provider=Provider.CODEX
                )
            )
            await pilot.pause()
            assert "Codex" in str(pilot.app.query_one("#participants").render())
            assert launcher.requests[0][1].id == "codex-impl"

    @pytest.mark.asyncio
    async def test_ctrl_n_opens_new_agent_modal(self) -> None:
        """test ctrl n opens new agent modal."""
        app = CtlrmApp(Workspace.from_projects([]))
        async with app.run_test() as pilot:
            await pilot.press("ctrl+n")
            await pilot.pause()
            assert pilot.app.screen.query_one("#new-agent-dialog")
            assert pilot.app.screen.query_one("#agent-provider")

    def test_default_app_launcher_persists_to_user_registry(self) -> None:
        """test default app launcher persists to user registry."""
        app = CtlrmApp(Workspace.from_projects([]))
        assert app.launcher.registry_path.name == "registry.toml"
        assert app.launcher.registry_path.parent.name == "ctlrm"


class TestLaunchErrors:
    """Expected launch failures are visible without exiting the TUI."""

    @pytest.mark.asyncio
    async def test_invalid_launch(self, tmp_path: Path) -> None:
        """A duplicate participant error leaves the application running."""

        class RejectingLauncher:
            """Simulate a validated launch rejection."""

            def launch(self, project, request):
                """Reject a duplicate before returning a participant."""
                raise ValueError("participant already registered")

        project = Project(id="p", name="P", root=tmp_path)
        workspace = Workspace.from_projects([project])
        app = CtlrmApp(workspace, launcher=RejectingLauncher())
        async with app.run_test() as pilot:
            app.launch_agent_from_request(
                AgentLaunchRequest(
                    id="worker", name="Worker", role="work", provider=Provider.CLAUDE
                )
            )
            await pilot.pause()
            assert "already registered" in workspace.status
            assert not project.participants
