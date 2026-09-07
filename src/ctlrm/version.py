"""Package version metadata."""

from __future__ import annotations

from contextlib import suppress
from importlib.metadata import PackageNotFoundError, version

__version__ = "0.0.0"

with suppress(PackageNotFoundError):
    __version__ = version("ctlrm")
