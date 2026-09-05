"""Validation for identifiers used as room path components."""

from __future__ import annotations

import re

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_path_component(value: str, *, label: str, max_length: int) -> str:
    """Return an identifier that is safe to use as one path component."""
    if len(value) > max_length or not _SAFE_COMPONENT.fullmatch(value):
        raise ValueError(
            f"{label} must start with a letter or digit and contain only "
            "letters, digits, dots, underscores, or hyphens"
        )
    return value
