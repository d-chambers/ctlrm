from pathlib import Path

from ctlrm.app import CtlrmApp
from ctlrm.models import Participant, Project
from ctlrm.workspace import Workspace


def main() -> None:
    cwd = Path.cwd()
    project = Project(
        id=cwd.name or "project",
        name=cwd.name or "project",
        root=cwd,
        tmux_session=cwd.name or None,
        participants=[Participant.human("you", "You", "human", cwd)],
    )
    CtlrmApp(Workspace.from_projects([project])).run()


if __name__ == "__main__":
    main()
