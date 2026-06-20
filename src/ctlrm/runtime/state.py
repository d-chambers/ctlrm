from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator

from ctlrm.runtime.messages import _split_front_matter


class ParticipantStateFile(BaseModel):
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
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    @classmethod
    def parse(cls, text: str) -> ParticipantStateFile:
        front_matter, body = _split_front_matter(text)
        state = cls.model_validate(yaml.safe_load(front_matter))
        state.body = body
        return state

    @classmethod
    def read(cls, path: Path) -> ParticipantStateFile:
        return cls.parse(path.read_text())
