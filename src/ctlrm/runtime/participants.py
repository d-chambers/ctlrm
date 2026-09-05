"""Participant signatures for filesystem-backed rooms."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from ctlrm.runtime.documents import load_yaml

from pydantic import BaseModel, Field, field_validator

from ctlrm.runtime.paths import validate_path_component
from ctlrm.runtime.documents import normalize_timestamp


def validate_participant_id(value: str) -> str:
    """Return a participant id that is safe to use as one path component."""
    return validate_path_component(value, label="participant id", max_length=64)


class ParticipantSignature(BaseModel):
    """Immutable acceptance of a room role written by one participant."""

    protocol_version: Literal[1] = 1
    room_id: str
    id: str
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    provider: str | None = None
    restart_command: str | None = None
    joined_at: str
    capabilities: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        """Reject identifiers that could escape the participants directory."""
        return validate_participant_id(value)

    @field_validator("room_id")
    @classmethod
    def _validate_room_id(cls, value: str) -> str:
        """Reject unsafe room identifiers."""
        return validate_path_component(value, label="room id", max_length=128)

    @field_validator("name", "role", "kind")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        """Reject required identity values containing only whitespace."""
        value = value.strip()
        if not value:
            raise ValueError("participant identity values cannot be blank")
        return value

    @field_validator("restart_command")
    @classmethod
    def _validate_restart_command(cls, value: str | None) -> str | None:
        """Normalize an optional command used to resume the participant session."""
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("restart command cannot be blank")
        return value

    @field_validator("joined_at", mode="before")
    @classmethod
    def _coerce_joined_at(cls, value: object) -> str:
        """Normalize datetime-like values loaded by YAML."""
        return normalize_timestamp(value)

    @classmethod
    def parse(cls, text: str) -> "ParticipantSignature":
        """Parse a signature from YAML text."""
        return cls.model_validate(load_yaml(text))

    @classmethod
    def read(cls, path: Path) -> "ParticipantSignature":
        """Read and validate a signature file."""
        return cls.parse(path.read_text(encoding="utf-8"))

    def to_yaml(self) -> str:
        """Serialize the signature as stable, human-readable YAML."""
        data = self.model_dump(mode="json", exclude_none=True)
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
