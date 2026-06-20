from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping

from ctlrm.workspace import Workspace

CTLRM_RELOAD_STATE = "CTLRM_RELOAD_STATE"

Execvpe = Callable[[str, list[str], dict[str, str]], None]


def encode_workspace(workspace: Workspace) -> str:
    return workspace.model_dump_json()


def decode_workspace(state: str) -> Workspace:
    return Workspace.model_validate_json(state)


def restart_with_state(
    workspace: Workspace,
    *,
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    execvpe: Execvpe = os.execvpe,
) -> None:
    args = list(argv or sys.argv)
    if "--dev" not in args:
        args.append("--dev")

    next_env = dict(os.environ if env is None else env)
    next_env[CTLRM_RELOAD_STATE] = encode_workspace(workspace)
    execvpe(args[0], args, next_env)
