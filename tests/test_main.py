from pathlib import Path

from ctlrm.__main__ import build_initial_workspace, workspace_from_environment
from ctlrm.dev import CTLRM_RELOAD_STATE, encode_workspace
from ctlrm.models import Project
from ctlrm.workspace import Workspace


def test_build_initial_workspace_uses_current_directory() -> None:
    workspace = build_initial_workspace(Path("/repo/ctlrm"))

    assert workspace.selected_project_id == "ctlrm"
    assert workspace.projects[0].name == "ctlrm"
    assert workspace.projects[0].participants[0].id == "you"


def test_dev_mode_restores_workspace_from_reload_environment() -> None:
    workspace = Workspace.from_projects([Project(id="p", name="Project", root=Path("/repo"))])
    workspace.active_panel = "tree"

    restored = workspace_from_environment(
        dev_mode=True,
        env={CTLRM_RELOAD_STATE: encode_workspace(workspace)},
        cwd=Path("/other"),
    )

    assert restored == workspace
