"""Resolve legacy worktree storage or an explicitly bound central job runtime."""

from pathlib import Path


def runtime_path(root: Path) -> Path:
    """Resolve a bounded locator without treating arbitrary symlinks as runtime roots."""
    local = root / ".ctlrm"
    if local.is_symlink():
        raise ValueError("coordination runtime must not be a symlink")
    locator = local / "location.json"
    if not locator.exists():
        return local
    if locator.is_symlink() or locator.stat().st_size > 8192:
        raise ValueError("invalid central runtime locator")
    from ctlrm.managed.storage import read_record

    record = read_record(locator)
    if record.get("schema_version") != 1 or record.get("worktree") != str(root.resolve()):
        raise ValueError("central runtime locator does not match this worktree")
    job = Path(record["job_directory"])
    if not job.is_absolute() or job.resolve() != job or not (job / "job.json").is_file():
        raise ValueError("central job directory is missing or moved")
    link = root / ".scratch/ctlrm"
    if not link.is_symlink() or link.resolve() != job:
        raise ValueError("managed scratch link is missing or changed")
    return job / "runtime"


def runtime_reference(root: Path) -> str:
    """Return an agent-readable short path through the managed scratch link."""
    return ".ctlrm" if runtime_path(root) == root / ".ctlrm" else ".scratch/ctlrm/runtime"
