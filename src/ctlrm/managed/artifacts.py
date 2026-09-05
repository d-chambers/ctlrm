"""Content-addressed worktree artifacts for attributable handoffs and stale-review checks."""

import hashlib
import os
from pathlib import Path
import stat
import subprocess

from ctlrm.managed.area import Area, git
from ctlrm.managed.storage import digest, publish, read_record


def fingerprint(root: Path) -> dict:
    """Hash tracked and non-ignored untracked files without following symlinks.

    Parameters
    ----------
    root
        Worktree root whose delivered code is being inspected.
    """
    head = git(root, "rev-parse", "HEAD")
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
    except subprocess.SubprocessError as error:
        raise ValueError("Git artifact enumeration failed or timed out") from error
    names = sorted(set(os.fsdecode(item) for item in result.stdout.split(b"\0") if item))
    names = [name for name in names if name != ".ctlrm" and not name.startswith(".ctlrm/")]
    if len(names) > 10000:
        raise ValueError("artifact exceeds 10000 files")
    files, total = {}, 0
    for name in names:
        path = root / name
        if not path.parent.resolve().is_relative_to(root):
            raise ValueError(f"artifact path escapes the worktree: {name}")
        try:
            before = path.lstat()
        except FileNotFoundError:
            files[name] = {"missing": True}
            continue
        if stat.S_ISDIR(before.st_mode):
            nested_head = git(path, "rev-parse", "HEAD")
            if git(path, "status", "--porcelain", "--untracked-files=all"):
                raise ValueError(f"nested repository must be clean for artifact capture: {name}")
            files[name] = {"git_head": nested_head}
            continue
        if stat.S_ISLNK(before.st_mode):
            data = os.fsencode(os.readlink(path))
        elif stat.S_ISREG(before.st_mode):
            if before.st_size + total > 64 * 1024 * 1024:
                raise ValueError("artifact exceeds 64 MiB")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                data = stream.read(64 * 1024 * 1024 - total + 1)
                after = os.fstat(stream.fileno())
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ValueError(f"artifact changed during hashing: {name}")
        else:
            raise ValueError(f"unsupported artifact file type: {name}")
        total += len(data)
        if total > 64 * 1024 * 1024:
            raise ValueError("artifact exceeds 64 MiB")
        files[name] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "mode": stat.S_IMODE(before.st_mode),
            "symlink": stat.S_ISLNK(before.st_mode),
        }
    dirty = bool(git(root, "status", "--porcelain", "--untracked-files=all"))
    if git(root, "rev-parse", "HEAD") != head:
        raise ValueError("HEAD changed during artifact capture")
    return {"head": head, "dirty": dirty, "files": files}


def capture(area: Area, run_id: str, execution_id: str) -> str:
    """Publish immutable evidence; only a committed outcome attributes it to the run."""
    record = {
        "area_id": area.data["id"],
        "run_id": run_id,
        "execution_id": execution_id,
        "version": fingerprint(area.root),
    }
    reference = "artifact-" + digest(record)
    publish(area.room.root / "artifacts" / f"{reference}.json", record)
    return reference


def verify(area: Area, reference: str) -> None:
    """Reject approvals if the delivered content version changed during review."""
    from ctlrm.runtime.paths import validate_path_component

    validate_path_component(reference, label="artifact reference", max_length=80)
    record = read_record(area.room.root / "artifacts" / f"{reference}.json")
    if reference != "artifact-" + digest(record) or record.get("area_id") != area.data["id"]:
        raise ValueError("invalid artifact identity")
    if record["version"] != fingerprint(area.root):
        raise ValueError("input artifact changed; retry review against a new version")
