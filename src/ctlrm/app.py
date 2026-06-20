from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Static

from ctlrm.workspace import Direction, Workspace

ResizeHandleId = Literal["split-left", "split-right", "split-center"]

PANEL_TITLES = {
    "participants": "Participants",
    "file": "Current File",
    "selected": "Selected",
    "tree": "Project Tree",
}


@dataclass
class DragState:
    handle_id: ResizeHandleId
    start_x: int
    start_y: int
    participants_width: int
    tree_width: int
    selected_height: int


class ResizeHandle(Static):
    def __init__(self, label: str, handle_id: ResizeHandleId) -> None:
        super().__init__(label, id=handle_id, classes="resize-handle")
        self.handle_id = handle_id

    def on_mouse_down(self, event: events.MouseDown) -> None:
        event.stop()
        event.prevent_default()
        self.capture_mouse()
        self.app.begin_resize(self.handle_id, event.screen_x, event.screen_y)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        event.stop()
        self.release_mouse()
        self.app.end_resize()


class CtlrmApp(App[None]):
    CSS = """
    #main { height: 1fr; }
    #participants { border: solid $primary; }
    #center { width: 1fr; }
    #file { height: 1fr; border: solid $primary; }
    #selected { border: solid $primary; }
    #tree { border: solid $primary; }
    .resize-handle { width: 1; height: 1fr; content-align: center middle; color: $accent; }
    #split-center { width: 1fr; height: 1; }
    .active-panel { border: heavy $accent; }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+shift+left", "focus_left", "Focus left"),
        ("ctrl+shift+right", "focus_right", "Focus right"),
        ("ctrl+shift+up", "focus_up", "Focus up"),
        ("ctrl+shift+down", "focus_down", "Focus down"),
    ]

    def __init__(self, workspace: Workspace) -> None:
        super().__init__()
        self.workspace = workspace
        self._drag_state: DragState | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            yield Static(self._participants_text(), id="participants")
            yield ResizeHandle("│", "split-left")
            with Vertical(id="center"):
                yield Static("Read-only file viewer", id="file")
                yield ResizeHandle("─", "split-center")
                yield Static("Selected participant panel", id="selected")
            yield ResizeHandle("│", "split-right")
            yield Static("Project tree", id="tree")
        yield Footer()

    def on_mount(self) -> None:
        self._sync_layout_extents()
        self._sync_active_panel_class()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag_state is None:
            return
        event.stop()
        self._resize_from_drag(event.screen_x, event.screen_y)

    def action_focus_left(self) -> None:
        self._move_focus("left")

    def action_focus_right(self) -> None:
        self._move_focus("right")

    def action_focus_up(self) -> None:
        self._move_focus("up")

    def action_focus_down(self) -> None:
        self._move_focus("down")

    def begin_resize(self, handle_id: ResizeHandleId, start_x: int, start_y: int) -> None:
        self._drag_state = DragState(
            handle_id=handle_id,
            start_x=start_x,
            start_y=start_y,
            participants_width=self.workspace.participants_width,
            tree_width=self.workspace.tree_width,
            selected_height=self.workspace.selected_height,
        )

    def end_resize(self) -> None:
        self._drag_state = None

    def _resize_from_drag(self, screen_x: int, screen_y: int) -> None:
        if self._drag_state is None:
            return
        drag = self._drag_state
        delta_x = screen_x - drag.start_x
        delta_y = screen_y - drag.start_y
        if drag.handle_id == "split-left":
            self.workspace.participants_width = drag.participants_width
            self.workspace.resize_participants(delta_x)
        elif drag.handle_id == "split-right":
            self.workspace.tree_width = drag.tree_width
            self.workspace.resize_tree(-delta_x)
        elif drag.handle_id == "split-center":
            self.workspace.selected_height = drag.selected_height
            self.workspace.resize_selected(-delta_y)
        self._sync_layout_extents()

    def _move_focus(self, direction: Direction) -> None:
        self.workspace.move_focus(direction)
        self._sync_active_panel_class()

    def _sync_layout_extents(self) -> None:
        self.query_one("#participants").styles.width = self.workspace.participants_width
        self.query_one("#tree").styles.width = self.workspace.tree_width
        self.query_one("#selected").styles.height = self.workspace.selected_height

    def _sync_active_panel_class(self) -> None:
        for panel_id, title in PANEL_TITLES.items():
            widget = self.query_one(f"#{panel_id}")
            is_active = panel_id == self.workspace.active_panel
            widget.set_class(is_active, "active-panel")
            widget.border_title = f"● {title}" if is_active else title

    def _participants_text(self) -> str:
        if not self.workspace.projects:
            return "No participants"
        lines = []
        for participant in self.workspace.projects[0].participants:
            lines.append(f"{participant.name} · {participant.role} · {participant.kind.value}")
        return "\n".join(lines)
