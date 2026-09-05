"""Workspace state and navigation helpers for the ctlrm TUI."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ctlrm.models import Participant, Project

PanelId = Literal["participants", "file", "selected", "tree"]
CenterPanelId = Literal["file", "selected"]
Direction = Literal["left", "right", "up", "down"]


class Workspace(BaseModel):
    """Mutable UI state for projects, selections, focus, and layout."""

    projects: list[Project]
    selected_project_id: str | None = None
    selected_participant_id: str | None = None
    selected_file: Path | None = None
    status: str | None = None
    active_panel: PanelId = "participants"
    center_panel: CenterPanelId = "file"
    participants_width: int = 28
    tree_width: int = 32
    selected_height: int = 8

    @classmethod
    def from_projects(cls, projects: list[Project]) -> Workspace:
        """Create a workspace with sensible initial selections.

        Parameters
        ----------
        projects
            Projects available in the workspace.

        Returns
        -------
        Workspace
            Workspace selecting the first project and participant when present.
        """
        selected_project = projects[0] if projects else None
        selected_participant = (
            selected_project.participants[0]
            if selected_project and selected_project.participants
            else None
        )
        return cls(
            projects=projects,
            selected_project_id=selected_project.id if selected_project else None,
            selected_participant_id=selected_participant.id if selected_participant else None,
        )

    def selected_project(self) -> Project | None:
        """Return the selected project, if it still exists."""
        if self.selected_project_id is None:
            return None
        for project in self.projects:
            if project.id == self.selected_project_id:
                return project
        return None

    def add_participant(self, participant: Participant) -> None:
        """Append a participant to the selected project and select it."""
        project = self.selected_project()
        if project is None:
            return
        project.participants.append(participant)
        self.selected_participant_id = participant.id

    def move_focus(self, direction: Direction) -> None:
        """Move keyboard focus in a spatial direction."""
        next_panel = self._next_panel(direction)
        self.active_panel = next_panel
        if next_panel in ("file", "selected"):
            self.center_panel = next_panel

    def resize_participants(self, delta: int) -> None:
        """Resize the participant panel by a signed delta."""
        self.participants_width = self._clamp(self.participants_width + delta, 18, 60)

    def resize_tree(self, delta: int) -> None:
        """Resize the project tree panel by a signed delta."""
        self.tree_width = self._clamp(self.tree_width + delta, 18, 70)

    def resize_selected(self, delta: int) -> None:
        """Resize the selected participant panel by a signed delta."""
        self.selected_height = self._clamp(self.selected_height + delta, 5, 20)

    def _next_panel(self, direction: Direction) -> PanelId:
        """Resolve the next active panel for a direction."""
        if self.active_panel == "participants" and direction == "right":
            return self.center_panel
        if self.active_panel == "file":
            if direction == "left":
                return "participants"
            if direction == "right":
                return "tree"
            if direction == "down":
                return "selected"
        if self.active_panel == "selected":
            if direction == "left":
                return "participants"
            if direction == "right":
                return "tree"
            if direction == "up":
                return "file"
        if self.active_panel == "tree" and direction == "left":
            return self.center_panel
        return self.active_panel

    @staticmethod
    def _clamp(value: int, minimum: int, maximum: int) -> int:
        """Constrain a value to an inclusive range."""
        return max(minimum, min(value, maximum))
