from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator


class MailboxMessage(BaseModel):
    id: str
    from_: str = Field(alias="from")
    to: str
    kind: str
    title: str
    status: str
    created_at: str
    workflow_id: str | None = None
    run_id: str | None = None
    node_id: str | None = None
    files: list[str] = Field(default_factory=list)
    body: str = ""

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: object) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    @classmethod
    def parse(cls, text: str) -> MailboxMessage:
        front_matter, body = _split_front_matter(text)
        message = cls.model_validate(yaml.safe_load(front_matter))
        message.body = body
        return message

    @classmethod
    def read(cls, path: Path) -> MailboxMessage:
        return cls.parse(path.read_text())


def _split_front_matter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        raise ValueError("missing YAML front matter")
    try:
        front_matter, body = text[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise ValueError("front matter is not closed") from exc
    return front_matter, body
