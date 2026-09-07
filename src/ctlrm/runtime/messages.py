"""Mailbox message parsing for runtime communication files."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ctlrm.runtime.documents import parse_markdown_document
from ctlrm.runtime.participants import validate_participant_id
from ctlrm.runtime.paths import validate_path_component
from ctlrm.runtime.documents import normalize_timestamp


class MailboxMessage(BaseModel):
    """Message read from a participant mailbox markdown file."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    from_: str = Field(alias="from")
    to: str
    kind: str
    title: str
    status: str
    created_at: str
    run_id: str | None = None
    execution_id: str | None = None
    files: list[str] = Field(default_factory=list)
    body: str = ""

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        """Reject message identifiers that are unsafe filenames."""
        return validate_path_component(value, label="message id", max_length=128)

    @field_validator("from_", "to")
    @classmethod
    def _validate_participant_ids(cls, value: str) -> str:
        """Reject sender and recipient identifiers that are unsafe paths."""
        return validate_participant_id(value)

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: object) -> str:
        """Normalize datetime-like values from YAML front matter."""
        return normalize_timestamp(value)

    @classmethod
    def parse(cls, text: str) -> MailboxMessage:
        """Parse a mailbox message from markdown with YAML front matter."""
        metadata, body = parse_markdown_document(text)
        message = cls.model_validate(metadata)
        message.body = body
        return message

    @classmethod
    def read(cls, path: Path) -> MailboxMessage:
        """Read and parse a mailbox message file."""
        return cls.parse(path.read_text(encoding="utf-8"))

    def to_markdown(self) -> str:
        """Serialize the message as Markdown with YAML front matter."""
        data = self.model_dump(mode="json", by_alias=True, exclude={"body"}, exclude_none=True)
        front_matter = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).rstrip()
        body = self.body.strip("\n")
        return f"---\n{front_matter}\n---\n{body}\n"
