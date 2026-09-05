"""Retain completed code and verify self-contained project ZIP archives."""

from collections.abc import Iterator
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import zipfile

from ctlrm.managed.artifacts import fingerprint
from ctlrm.managed.storage import encoded, publish, read_record

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


def retain(root: Path, destination: Path, *, expected_version: dict | None = None) -> dict:
    """Freeze a job's completed code, including Git history and uncommitted file content."""
    manifest_path = destination / "manifest.json"
    if manifest_path.exists():
        manifest = read_record(manifest_path)
        verify_retained(destination, manifest, expected_version=expected_version)
        return manifest
    version = fingerprint(root)
    if expected_version is not None and version != expected_version:
        raise ValueError(
            "worktree changed after completion; restore its completed version before retaining"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".retain-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        result = subprocess.run(
            ["git", "-C", str(root), "bundle", "create", str(stage / "code.bundle"), "HEAD"],
            capture_output=True,
            timeout=120,
        )
        if result.returncode:
            raise ValueError(
                "could not retain Git history: " + result.stderr.decode(errors="replace")
            )
        checked = subprocess.run(
            ["git", "-C", str(root), "bundle", "verify", str(stage / "code.bundle")],
            capture_output=True,
            timeout=120,
        )
        if checked.returncode:
            raise ValueError("retained Git bundle verification failed")
        for name, info in version["files"].items():
            if "sha256" not in info:
                continue
            path = root / name
            data = (
                os.fsencode(os.readlink(path))
                if info["symlink"]
                else _read_regular(path, 64 * 1024 * 1024)
            )
            if hashlib.sha256(data).hexdigest() != info["sha256"]:
                raise ValueError(f"code changed during retention: {name}")
            blob = stage / "blobs" / info["sha256"]
            blob.parent.mkdir(exist_ok=True)
            blob.write_bytes(data)
        patch = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "diff",
                "--no-textconv",
                "--no-ext-diff",
                "--binary",
                "--cached",
                "HEAD",
                "--",
            ],
            capture_output=True,
            timeout=30,
        )
        if patch.returncode:
            raise ValueError("could not retain staged changes")
        (stage / "index.patch").write_bytes(patch.stdout)
        if fingerprint(root) != version:
            raise ValueError("worktree changed during retention; retry after writers stop")
        files = {}
        total = 0
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                data = _read_regular(path, MAX_ARCHIVE_BYTES - total)
                total += len(data)
                files[path.relative_to(stage).as_posix()] = hashlib.sha256(data).hexdigest()
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        manifest = {
            "schema_version": 1,
            "version": version,
            "files": files,
            "limitations": "Gitlinks identify nested repositories but do not include their object databases. Provider credentials and external native conversation stores are excluded.",
        }
        publish(stage / "manifest.json", manifest)
        verify_retained(stage, manifest)
        if destination.exists():
            raise ValueError("retention destination already exists without a valid manifest")
        for directory in [stage / "blobs", stage]:
            if directory.exists():
                _sync_directory(directory)
        stage.rename(destination)
        _sync_directory(destination.parent)
    return manifest


def _read_regular(path: Path, limit: int) -> bytes:
    """Read bounded regular files without following symlink targets."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError(f"unsupported or oversized archive file: {path}")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("archive exceeds 512 MiB")
    return data


def verify_retained(
    directory: Path, manifest: dict, *, expected_version: dict | None = None
) -> None:
    """Check the completed version, required inventory, and retained file checksums."""
    if expected_version is not None and manifest["version"] != expected_version:
        raise ValueError("retained code differs from the completed artifact")
    required = {"code.bundle", "index.patch"} | {
        "blobs/" + info["sha256"]
        for info in manifest["version"]["files"].values()
        if "sha256" in info
    }
    if manifest["schema_version"] != 1 or set(manifest["files"]) != required:
        raise ValueError("retained inventory does not match the completed code")
    for name, expected in manifest["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe retained file name")
        if not (directory / relative).parent.resolve().is_relative_to(directory.resolve()):
            raise ValueError("retained file escapes its directory")
        if (
            hashlib.sha256(_read_regular(directory / relative, MAX_ARCHIVE_BYTES)).hexdigest()
            != expected
        ):
            raise ValueError(f"retained code checksum mismatch: {name}")


def _sync_directory(path: Path) -> None:
    """Persist directory entries before committing their lifecycle reference."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _entries(project: Path) -> Iterator[tuple[str, bytes, bool]]:
    """Enumerate bounded project records without traversing symlinked directories."""
    total = 0
    count = 0
    for path in sorted(project.rglob("*")):
        if any(part.startswith(".retain-") for part in path.relative_to(project).parts):
            continue
        relative = path.relative_to(project).as_posix()
        if path.parent.name == "events" and path.name.startswith("."):
            continue
        if path.is_symlink():
            data = os.fsencode(os.readlink(path))
            link = True
        elif path.is_dir():
            continue
        else:
            if path.name == "lock" and path.parent.name == "supervisor":
                continue
            data = _read_regular(path, MAX_ARCHIVE_BYTES - total)
            link = False
        count += 1
        total += len(data)
        if total > MAX_ARCHIVE_BYTES or count > 100000:
            raise ValueError("archive exceeds its file or byte limit")
        yield relative, data, link


def archive_result(path: Path, files: int) -> dict:
    """Compute the published archive identity without loading its whole ZIP into memory."""
    with path.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path.resolve()), "sha256": checksum, "files": files}


def archive(project: Path, output: Path) -> dict:
    """Create or recover a verified ZIP without deleting source records or worktrees."""
    if output.resolve().is_relative_to(project.resolve()):
        raise ValueError("archive output must be outside the project records")
    if output.exists():
        manifest = verify_archive(output)
        current = {name: hashlib.sha256(data).hexdigest() for name, data, _ in _entries(project)}
        if manifest["project_id"] != project.name or manifest["files"] != current:
            raise ValueError("existing archive does not match this completed project")
        return archive_result(output, len(current))
    output.parent.mkdir(parents=True, exist_ok=True)
    entries = {}
    with tempfile.NamedTemporaryFile(
        prefix=".archive-", suffix=".zip", dir=output.parent, delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for relative, data, link in _entries(project):
                if link:
                    info = zipfile.ZipInfo(relative)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    bundle.writestr(info, data)
                else:
                    bundle.writestr(relative, data)
                entries[relative] = hashlib.sha256(data).hexdigest()
            manifest = {"schema_version": 1, "project_id": project.name, "files": entries}
            manifest_text = encoded(manifest)
            if len(manifest_text.encode()) > 8 * 1024 * 1024:
                raise ValueError("archive manifest is too large")
            bundle.writestr("archive-manifest.json", manifest_text)
        verify_archive(temporary_path)
        with temporary_path.open("rb") as stream:
            os.fsync(stream.fileno())
        os.link(temporary_path, output)
        _sync_directory(output.parent)
        return archive_result(output, len(entries))
    finally:
        temporary_path.unlink(missing_ok=True)


def verify_archive(path: Path) -> dict:
    """Verify ZIP membership, safe names, and recorded checksums without extracting."""
    try:
        with zipfile.ZipFile(path) as bundle:
            names = bundle.namelist()
            if len(names) != len(set(names)) or len(names) > 100001:
                raise ValueError("archive contains duplicate or excessive paths")
            if (
                sum(item.file_size for item in bundle.infolist())
                > MAX_ARCHIVE_BYTES + 8 * 1024 * 1024
            ):
                raise ValueError("archive expands beyond its size limit")
            for name in names:
                relative = Path(name)
                if relative.is_absolute() or ".." in relative.parts or chr(92) in name:
                    raise ValueError("archive contains unsafe paths")
            if bundle.getinfo("archive-manifest.json").file_size > 8 * 1024 * 1024:
                raise ValueError("archive manifest is too large")
            manifest = json.loads(bundle.read("archive-manifest.json"))
            if (
                not isinstance(manifest, dict)
                or manifest.get("schema_version") != 1
                or not isinstance(manifest.get("files"), dict)
                or not isinstance(manifest.get("project_id"), str)
            ):
                raise ValueError("invalid archive manifest")
            if set(names) != {*manifest["files"], "archive-manifest.json"}:
                raise ValueError("archive membership does not match its manifest")
            for name, checksum in manifest["files"].items():
                if hashlib.sha256(bundle.read(name)).hexdigest() != checksum:
                    raise ValueError(f"archive checksum mismatch: {name}")
            return manifest
    except (zipfile.BadZipFile, KeyError, TypeError) as error:
        raise ValueError("invalid project archive") from error
