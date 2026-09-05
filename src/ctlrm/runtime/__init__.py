"""Runtime filesystem helpers for project-local ctlrm state."""

from pathlib import Path

from ctlrm.runtime.filesystem import mkdir_shared
from ctlrm.runtime.location import runtime_path
from ctlrm.runtime.participants import validate_participant_id


class ProjectRuntime:
    """Manage the ``.ctlrm`` runtime directory for a project."""

    def __init__(self, project_root: Path) -> None:
        """Initialize paths for a project runtime."""
        self.project_root = project_root

    @property
    def root(self) -> Path:
        """Resolve storage only for operations that need a local runtime."""
        return runtime_path(self.project_root)

    def ensure_writable(self) -> None:
        """Reject public runtime mutations after a managed job has retired."""
        if (self.root / "area.yaml").exists():
            from ctlrm.managed.storage import Journal

            if Journal(self.project_root).replay()[0].get("retired"):
                raise ValueError("job is retired; its runtime is read-only")

    def init_project(self) -> None:
        """Create project runtime directories."""
        self.ensure_writable()
        if not self.project_root.is_dir():
            raise NotADirectoryError(f"project root must already exist: {self.project_root}")
        mkdir_shared(self.root)
        for directory in ("participants", "workflows", "runs"):
            mkdir_shared(self.root / directory)

    def init_participant(self, participant_id: str) -> None:
        """Create runtime directories for one participant."""
        validate_participant_id(participant_id)
        participant_root = self.root / "participants" / participant_id
        self.init_project()
        mkdir_shared(participant_root)
        mkdir_shared(participant_root / "inbox")
        mkdir_shared(participant_root / "outbox")
