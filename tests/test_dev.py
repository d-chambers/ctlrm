from pathlib import Path

from ctlrm.dev import CTLRM_RELOAD_STATE, decode_workspace, encode_workspace, restart_with_state
from ctlrm.models import Participant, Project
from ctlrm.workspace import Workspace


def test_workspace_state_round_trips_for_dev_reload() -> None:
    project = Project(
        id="ctlrm",
        name="ctlrm",
        root=Path("/repo"),
        participants=[Participant.human("derrick", "Derrick", "operator", Path("/repo"))],
    )
    workspace = Workspace.from_projects([project])
    workspace.active_panel = "tree"
    workspace.center_panel = "selected"
    workspace.participants_width = 36
    workspace.tree_width = 44
    workspace.selected_height = 11

    restored = decode_workspace(encode_workspace(workspace))

    assert restored == workspace


def test_restart_with_state_execs_current_command_with_reload_env() -> None:
    workspace = Workspace.from_projects([])
    calls = []

    def fake_execvpe(file: str, args: list[str], env: dict[str, str]) -> None:
        calls.append((file, args, env))
        raise RuntimeError("stop")

    try:
        restart_with_state(
            workspace,
            argv=["ctlrm", "--dev"],
            env={"PATH": "/bin"},
            execvpe=fake_execvpe,
        )
    except RuntimeError:
        pass

    file, args, env = calls[0]
    assert file == "ctlrm"
    assert args == ["ctlrm", "--dev"]
    assert CTLRM_RELOAD_STATE in env
    assert decode_workspace(env[CTLRM_RELOAD_STATE]) == workspace
