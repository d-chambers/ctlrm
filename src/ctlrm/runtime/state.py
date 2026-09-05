"""Participant state file parsing for runtime status documents."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ctlrm.runtime.documents import load_yaml

from pydantic import BaseModel, field_validator

from ctlrm.runtime.documents import split_front_matter


class ParticipantStateFile(BaseModel):
    """State record read from a participant runtime markdown file."""

    participant_id: str
    kind: str
    state: str
    active: bool
    updated_at: str
    current_run_id: str | None = None
    current_node_id: str | None = None
    summary: str | None = None
    body: str = ""

    @field_validator("updated_at", mode="before")
    @classmethod
    def _coerce_updated_at(cls, value: object) -> str:
        """Normalize datetime-like values from YAML front matter."""
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    @classmethod
    def parse(cls, text: str) -> ParticipantStateFile:
        """Parse a participant state file from markdown text."""
        front_matter, body = split_front_matter(text)
        state = cls.model_validate(load_yaml(front_matter))
        state.body = body
        return state

    @classmethod
    def read(cls, path: Path) -> ParticipantStateFile:
        """Read and parse a participant state file."""
        return cls.parse(path.read_text(encoding="utf-8"))
