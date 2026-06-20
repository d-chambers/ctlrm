import pytest
from pathlib import Path

from ctlrm.app import CtlrmApp
from ctlrm.launcher import AgentLaunchRequest
from ctlrm.models import Participant, Project, Provider
from ctlrm.workspace import Workspace


@pytest.mark.asyncio
async def test_app_renders_human_participant() -> None:
    project = Project(
        id="ctlrm",
        name="ctlrm",
        root=Path("/repo"),
        participants=[Participant.human("derrick", "Derrick", "product-owner", Path("/repo"))],
    )
    app = CtlrmApp(Workspace.from_projects([project]))

    async with app.run_test() as pilot:
        assert "Derrick" in str(pilot.app.query_one("#participants").render())


def test_quit_binding_is_ctrl_q_only() -> None:
    bindings = {binding[0] for binding in CtlrmApp.BINDINGS}

    assert "ctrl+q" in bindings
    assert "q" not in bindings


def test_focus_bindings_are_ctrl_shift_arrows() -> None:
    bindings = {binding[0] for binding in CtlrmApp.BINDINGS}

    assert "ctrl+shift+left" in bindings
    assert "ctrl+shift+right" in bindings
    assert "ctrl+shift+up" in bindings
    assert "ctrl+shift+down" in bindings


@pytest.mark.asyncio
async def test_app_renders_resize_handles() -> None:
    app = CtlrmApp(Workspace.from_projects([]))

    async with app.run_test() as pilot:
        assert pilot.app.query_one("#split-left")
        assert pilot.app.query_one("#split-right")
        assert pilot.app.query_one("#split-center")


@pytest.mark.asyncio
async def test_active_panel_title_marker_moves_with_focus() -> None:
    app = CtlrmApp(Workspace.from_projects([]))

    async with app.run_test() as pilot:
        assert pilot.app.query_one("#participants").border_title == "● Participants"
        assert pilot.app.query_one("#file").border_title == "Current File"

        await pilot.press("ctrl+shift+right")

        assert pilot.app.query_one("#participants").border_title == "Participants"
        assert pilot.app.query_one("#file").border_title == "● Current File"


def test_new_agent_binding_is_ctrl_n() -> None:
    bindings = {binding[0] for binding in CtlrmApp.BINDINGS}

    assert "ctrl+n" in bindings


class FakeLauncher:
    def __init__(self) -> None:
        self.requests = []

    def launch(self, project, request):
        self.requests.append((project, request))
        participant = Participant.agent(
            request.id,
            request.name,
            request.role,
            request.provider,
            project.root,
        )
        project.participants.append(participant)
        return participant


@pytest.mark.asyncio
async def test_launch_request_updates_roster() -> None:
    project = Project(id="ctlrm", name="ctlrm", root=Path("/repo"), participants=[])
    launcher = FakeLauncher()
    app = CtlrmApp(Workspace.from_projects([project]), launcher=launcher)

    async with app.run_test() as pilot:
        app.launch_agent_from_request(
            AgentLaunchRequest(
                id="codex-impl",
                name="Codex",
                role="implementer",
                provider=Provider.CODEX,
            )
        )
        await pilot.pause()
        assert "Codex" in str(pilot.app.query_one("#participants").render())
        assert launcher.requests[0][1].id == "codex-impl"


@pytest.mark.asyncio
async def test_ctrl_n_opens_new_agent_modal() -> None:
    app = CtlrmApp(Workspace.from_projects([]))

    async with app.run_test() as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert pilot.app.screen.query_one("#new-agent-dialog")
        assert pilot.app.screen.query_one("#agent-provider")


def test_default_app_launcher_persists_to_user_registry() -> None:
    app = CtlrmApp(Workspace.from_projects([]))

    assert app.launcher.registry_path.name == "registry.toml"
    assert app.launcher.registry_path.parent.name == "ctlrm"


def test_hot_reload_requires_dev_mode() -> None:
    reloaded = []
    app = CtlrmApp(Workspace.from_projects([]), hot_reloader=reloaded.append)

    app.action_hot_reload()

    assert reloaded == []
    assert app.workspace.status == "Hot reload requires --dev"


def test_hot_reload_passes_current_workspace_in_dev_mode() -> None:
    reloaded = []
    workspace = Workspace.from_projects([])
    app = CtlrmApp(workspace, dev_mode=True, hot_reloader=reloaded.append)

    app.action_hot_reload()

    assert reloaded == [workspace]


@pytest.mark.asyncio
async def test_dev_mode_mounts_hot_reload_binding() -> None:
    app = CtlrmApp(Workspace.from_projects([]), dev_mode=True, hot_reloader=lambda workspace: None)

    async with app.run_test():
        assert app.dev_mode is True
