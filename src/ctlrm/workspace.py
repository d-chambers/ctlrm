from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ctlrm.models import Project

PanelId = Literal["participants", "file", "selected", "tree"]
CenterPanelId = Literal["file", "selected"]
Direction = Literal["left", "right", "up", "down"]


class Workspace(BaseModel):
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

    def move_focus(self, direction: Direction) -> None:
        next_panel = self._next_panel(direction)
        self.active_panel = next_panel
        if next_panel in ("file", "selected"):
            self.center_panel = next_panel

    def resize_participants(self, delta: int) -> None:
        self.participants_width = self._clamp(self.participants_width + delta, 18, 60)

    def resize_tree(self, delta: int) -> None:
        self.tree_width = self._clamp(self.tree_width + delta, 18, 70)

    def resize_selected(self, delta: int) -> None:
        self.selected_height = self._clamp(self.selected_height + delta, 5, 20)

    def _next_panel(self, direction: Direction) -> PanelId:
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
        return max(minimum, min(value, maximum))
