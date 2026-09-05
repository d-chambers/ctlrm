"""Canonical room definition and role assignments."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from ctlrm.runtime.documents import parse_markdown_document
from ctlrm.runtime.participants import validate_participant_id
from ctlrm.runtime.paths import validate_path_component
from ctlrm.runtime.documents import normalize_timestamp


class RoleAssignment(BaseModel):
    """One participant role assigned by the room author."""

    participant: str
    role: str = Field(min_length=1)

    @field_validator("participant")
    @classmethod
    def _validate_participant(cls, value: str) -> str:
        """Require a path-safe participant identifier."""
        return validate_participant_id(value)

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        """Reject blank roles."""
        value = value.strip()
        if not value:
            raise ValueError("role cannot be blank")
        return value


class RoomManifest(BaseModel):
    """Immutable room prompt and participant role roster."""

    protocol_version: Literal[1] = 1
    id: str
    author: str
    created_at: str
    assignments: list[RoleAssignment] = Field(min_length=1)
    prompt: str = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        """Require a compact, path-safe room identifier."""
        return validate_path_component(value, label="room id", max_length=128)

    @field_validator("author")
    @classmethod
    def _validate_author(cls, value: str) -> str:
        """Require a path-safe author identifier."""
        return validate_participant_id(value)

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: object) -> str:
        """Normalize datetime-like values loaded by YAML."""
        return normalize_timestamp(value)

    @field_validator("prompt")
    @classmethod
    def _validate_prompt(cls, value: str) -> str:
        """Reject an empty or whitespace-only prompt."""
        if not value.strip():
            raise ValueError("room prompt cannot be blank")
        return value.strip()

    @model_validator(mode="after")
    def _validate_assignments(self) -> RoomManifest:
        """Require unique assignments and a role for the author."""
        participants = [item.participant for item in self.assignments]
        if len(participants) != len(set(participants)):
            raise ValueError("each participant must have exactly one assignment")
        if self.author not in participants:
            raise ValueError("room author must have an assigned role")
        return self

    def role_for(self, participant_id: str) -> str:
        """Return the role assigned to a participant.

        Parameters
        ----------
        participant_id
            Participant whose role should be returned.

        Returns
        -------
        str
            The role assigned by the room author.

        Raises
        ------
        ValueError
            If the participant has no assignment in this room.
        """
        validate_participant_id(participant_id)
        for assignment in self.assignments:
            if assignment.participant == participant_id:
                return assignment.role
        raise ValueError(f"participant {participant_id!r} has no role assignment")

    @classmethod
    def parse(cls, text: str) -> RoomManifest:
        """Parse a room manifest from Markdown with YAML front matter."""
        metadata, prompt = parse_markdown_document(text)
        return cls.model_validate({**metadata, "prompt": prompt})

    @classmethod
    def read(cls, path: Path) -> RoomManifest:
        """Read and validate a room manifest."""
        return cls.parse(path.read_text(encoding="utf-8"))

    def to_markdown(self) -> str:
        """Serialize the room manifest as Markdown with YAML front matter."""
        data = self.model_dump(mode="json", exclude={"prompt"})
        front_matter = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).rstrip()
        return f"---\n{front_matter}\n---\n{self.prompt.rstrip()}\n"
