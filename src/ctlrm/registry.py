from __future__ import annotations

import os
import tomllib
from pathlib import Path

import tomli_w
from pydantic import BaseModel, Field

from ctlrm.models import Project


class Registry(BaseModel):
    projects: list[Project] = Field(default_factory=list)

    @classmethod
    def from_toml(cls, data: str) -> Registry:
        return cls.model_validate(tomllib.loads(data))

    @classmethod
    def load(cls, path: Path) -> Registry:
        if not path.exists():
            return cls()
        return cls.from_toml(path.read_text())

    def to_toml(self) -> str:
        return tomli_w.dumps(self.model_dump(mode="json", exclude_none=True))

    def save_atomic(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(self.to_toml())
        os.replace(tmp_path, path)
