"""Runtime filesystem helpers for project-local ctlrm state."""

from pathlib import Path

from ctlrm.runtime.participants import validate_participant_id


class ProjectRuntime:
    """Manage the ``.ctlrm`` runtime directory for a project."""

    def __init__(self, project_root: Path) -> None:
        """Initialize paths for a project runtime."""
        self.project_root = project_root
        self.root = project_root / ".ctlrm"

    def init_project(self) -> None:
        """Create project runtime directories."""
        (self.root / "participants").mkdir(parents=True, exist_ok=True)
        (self.root / "workflows").mkdir(parents=True, exist_ok=True)
        (self.root / "runs").mkdir(parents=True, exist_ok=True)

    def init_participant(self, participant_id: str) -> None:
        """Create runtime directories for one participant."""
        validate_participant_id(participant_id)
        participant_root = self.root / "participants" / participant_id
        (participant_root / "inbox").mkdir(parents=True, exist_ok=True)
        (participant_root / "outbox").mkdir(parents=True, exist_ok=True)
