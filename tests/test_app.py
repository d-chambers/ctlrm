import pytest
from pathlib import Path

from ctlrm.app import CtlrmApp
from ctlrm.models import Participant, Project
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
