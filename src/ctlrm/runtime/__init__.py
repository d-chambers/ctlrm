from pathlib import Path


class ProjectRuntime:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.root = project_root / ".ctlrm"

    def init_project(self) -> None:
        (self.root / "participants").mkdir(parents=True, exist_ok=True)
        (self.root / "workflows").mkdir(parents=True, exist_ok=True)
        (self.root / "runs").mkdir(parents=True, exist_ok=True)

    def init_participant(self, participant_id: str) -> None:
        participant_root = self.root / "participants" / participant_id
        (participant_root / "inbox").mkdir(parents=True, exist_ok=True)
        (participant_root / "outbox").mkdir(parents=True, exist_ok=True)
