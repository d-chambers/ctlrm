"""Shared POSIX directory creation and durability helpers."""

import errno
import os
import tempfile
from pathlib import Path

DIRECTORY_MODE = 0o2770
_FILE_MODE = 0o660
_UNSUPPORTED_LINK_ERRNOS = {errno.EPERM, errno.EXDEV, errno.EOPNOTSUPP}


def mkdir_shared(path: Path) -> None:
    """Create group-writable directories and persist their parent entries."""
    if not path.parent.exists():
        mkdir_shared(path.parent)
    try:
        path.mkdir(mode=DIRECTORY_MODE)
    except FileExistsError:
        if not path.is_dir():
            raise NotADirectoryError(str(path))
    else:
        path.chmod(DIRECTORY_MODE)
    # Also sync on retries after an earlier mkdir succeeded but its fsync failed.
    for directory in (path, path.parent):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def write_exclusive_atomic(path: Path, text: str) -> None:
    """Atomically create a file without replacing an existing owner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
            encoding="utf-8",
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.fchmod(temporary.fileno(), _FILE_MODE)
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_path, path)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise FileExistsError(errno.EEXIST, "record already exists", str(path)) from exc
            if exc.errno in _UNSUPPORTED_LINK_ERRNOS:
                raise RuntimeError("storage must support atomic hard links") from exc
            raise
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
