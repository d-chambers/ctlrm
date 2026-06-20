from __future__ import annotations

import argparse
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from ctlrm.app import CtlrmApp
from ctlrm.dev import CTLRM_RELOAD_STATE, decode_workspace, restart_with_state
from ctlrm.models import Participant, Project
from ctlrm.workspace import Workspace


def build_initial_workspace(cwd: Path) -> Workspace:
    project = Project(
        id=cwd.name or "project",
        name=cwd.name or "project",
        root=cwd,
        tmux_session=cwd.name or None,
        participants=[Participant.human("you", "You", "human", cwd)],
    )
    return Workspace.from_projects([project])


def workspace_from_environment(
    *,
    dev_mode: bool,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Workspace:
    source_env = os.environ if env is None else env
    if dev_mode and (state := source_env.get(CTLRM_RELOAD_STATE)):
        return decode_workspace(state)
    return build_initial_workspace(cwd or Path.cwd())


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ctlrm")
    parser.add_argument("--dev", action="store_true", help="enable development hot reload")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    workspace = workspace_from_environment(dev_mode=args.dev)
    CtlrmApp(workspace, dev_mode=args.dev, hot_reloader=restart_with_state).run()


if __name__ == "__main__":
    main()
