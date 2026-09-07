"""Content-addressed worktree artifacts for attributable handoffs and stale-review checks."""

import hashlib
import os
from pathlib import Path
import stat
import subprocess

from ctlrm.managed.area import Area, git
from ctlrm.managed.commits import retain_commits
from ctlrm.runtime.location import runtime_path
from ctlrm.managed.storage import digest, publish, read_record


def _files(root: Path, *arguments: str) -> list[bytes]:
    """Enumerate NUL-delimited Git index/worktree entries with bounded execution."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", *arguments],
            capture_output=True,
            check=True,
            timeout=15,
        )
    except subprocess.SubprocessError as error:
        raise ValueError("Git artifact enumeration failed or timed out") from error
    return [item for item in result.stdout.split(b"\0") if item]


def _nested(root: Path, name: str) -> dict:
    """Capture a clean nested checkout only when Git identifies it as its own root."""
    path = root / name
    if Path(git(path, "rev-parse", "--show-toplevel")).resolve() != path.resolve():
        raise ValueError(f"directory is not a nested repository root: {name}")
    head = git(path, "rev-parse", "HEAD")
    if git(path, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError(f"nested repository must be clean for artifact capture: {name}")
    return {"git_head": head}


def fingerprint(root: Path) -> dict:
    """Hash tracked and non-ignored untracked files without following symlinks.

    Parameters
    ----------
    root
        Worktree root whose delivered code is being inspected.
    """
    head = git(root, "rev-parse", "HEAD")
    index = {}
    index_entries = _files(root, "--stage")
    for item in index_entries:
        metadata, name = item.split(b"\t", 1)
        mode, object_id, stage = metadata.split()
        if mode == b"160000":
            if stage != b"0":
                raise ValueError("resolve submodule index conflicts before artifact capture")
            index[os.fsdecode(name)] = object_id.decode("ascii")
    names = sorted(
        set(
            os.fsdecode(item).rstrip("/")
            for item in _files(root, "--cached", "--others", "--exclude-standard")
        )
    )
    names = [name for name in names if name != ".ctlrm" and not name.startswith(".ctlrm/")]
    if runtime_path(root) != root / ".ctlrm":
        names = [
            name
            for name in names
            if name != ".scratch/ctlrm" and not name.startswith(".scratch/ctlrm/")
        ]
    if len(names) > 10000:
        raise ValueError("artifact exceeds 10000 files")
    files, total = {}, 0
    for name in names:
        path = root / name
        if not path.parent.resolve().is_relative_to(root):
            raise ValueError(f"artifact path escapes the worktree: {name}")
        if name in index and not path.is_symlink() and (not path.exists() or path.is_dir()):
            if (path / ".git").exists():
                files[name] = {"index_gitlink": index[name], **_nested(root, name)}
            else:
                if path.exists() and any(path.iterdir()):
                    raise ValueError(f"uninitialized gitlink has unversioned contents: {name}")
                files[name] = {"index_gitlink": index[name], "initialized": False}
            continue
        try:
            before = path.lstat()
        except FileNotFoundError:
            files[name] = {"missing": True}
            continue
        if stat.S_ISDIR(before.st_mode):
            files[name] = (
                _nested(root, name)
                if (path / ".git").exists()
                else {"directory": True, "mode": stat.S_IMODE(before.st_mode)}
            )
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
    return {
        "head": head,
        "dirty": dirty,
        "index_sha256": hashlib.sha256(b"\0".join(index_entries)).hexdigest(),
        "files": files,
    }


def capture(area: Area, run_id: str, execution_id: str, *, base_commit: str | None = None) -> str:
    """Publish immutable evidence; only a committed outcome attributes it to the run."""
    record = {
        "area_id": area.data["id"],
        "run_id": run_id,
        "execution_id": execution_id,
        "version": fingerprint(area.root),
    }
    record.update(retain_commits(area, base_commit, record["version"]["head"]))
    reference = "artifact-" + digest(record)
    publish(area.runtime / "artifacts" / f"{reference}.json", record)
    return reference


def read_artifact(directory: Path, area_id: str, reference: str) -> dict:
    """Read an artifact only after checking its content-addressed identity and owning area."""
    from ctlrm.runtime.paths import validate_path_component

    validate_path_component(reference, label="artifact reference", max_length=80)
    record = read_record(directory / "artifacts" / f"{reference}.json")
    if reference != "artifact-" + digest(record) or record.get("area_id") != area_id:
        raise ValueError("invalid artifact identity")
    return record


def verify(area: Area, reference: str, *, version: dict | None = None) -> None:
    """Reject approvals if the delivered content version changed during review."""
    record = read_artifact(area.runtime, area.data["id"], reference)
    if record["version"] != (version if version is not None else fingerprint(area.root)):
        raise ValueError("input artifact changed; retry review against a new version")
