"""Shared POSIX directory creation and durability helpers."""

import os
from pathlib import Path

DIRECTORY_MODE = 0o2770


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
