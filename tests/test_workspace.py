"""test workspace.py."""

from pathlib import Path
from ctlrm.models import Participant, Project
from ctlrm.workspace import Workspace


class TestWorkspace:
    """TestWorkspace."""

    def test_selects_first_project_and_participant(self) -> None:
        """test selects first project and participant."""
        project = Project(
            id="ctlrm",
            name="ctlrm",
            root=Path("/repo"),
            tmux_session="ctlrm",
            participants=[Participant.human("derrick", "Derrick", "product-owner", Path("/repo"))],
        )
        workspace = Workspace.from_projects([project])
        assert workspace.selected_project_id == "ctlrm"
        assert workspace.selected_participant_id == "derrick"

    def test_moves_active_panel_spatially(self) -> None:
        """test moves active panel spatially."""
        workspace = Workspace.from_projects([])
        assert workspace.active_panel == "participants"
        workspace.move_focus("right")
        assert workspace.active_panel == "file"
        workspace.move_focus("down")
        assert workspace.active_panel == "selected"
        workspace.move_focus("right")
        assert workspace.active_panel == "tree"
        workspace.move_focus("left")
        assert workspace.active_panel == "selected"
        workspace.move_focus("up")
        assert workspace.active_panel == "file"
        workspace.move_focus("left")
        assert workspace.active_panel == "participants"

    def test_tree_left_returns_to_last_center_panel(self) -> None:
        """test tree left returns to last center panel."""
        workspace = Workspace.from_projects([])
        workspace.move_focus("right")
        assert workspace.active_panel == "file"
        workspace.move_focus("right")
        assert workspace.active_panel == "tree"
        workspace.move_focus("left")
        assert workspace.active_panel == "file"
        workspace.move_focus("down")
        assert workspace.active_panel == "selected"
        workspace.move_focus("right")
        assert workspace.active_panel == "tree"
        workspace.move_focus("left")
        assert workspace.active_panel == "selected"

    def test_resizes_panel_extents_with_minimums(self) -> None:
        """test resizes panel extents with minimums."""
        workspace = Workspace.from_projects([])
        workspace.resize_participants(6)
        assert workspace.participants_width == 34
        workspace.resize_participants(-100)
        assert workspace.participants_width == 18
        workspace.resize_tree(-4)
        assert workspace.tree_width == 28
        workspace.resize_tree(-100)
        assert workspace.tree_width == 18
        workspace.resize_selected(5)
        assert workspace.selected_height == 13
        workspace.resize_selected(-100)
        assert workspace.selected_height == 5

    def test_add_participant_to_selected_project_selects_it(self) -> None:
        """test add participant to selected project selects it."""
        project = Project(id="ctlrm", name="ctlrm", root=Path("/repo"))
        workspace = Workspace.from_projects([project])
        participant = Participant.human("derrick", "Derrick", "product-owner", Path("/repo"))
        workspace.add_participant(participant)
        assert workspace.projects[0].participants == [participant]
        assert workspace.selected_participant_id == "derrick"
