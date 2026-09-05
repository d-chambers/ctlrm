"""Atomic TOML registry persistence with serialized worktree updates."""

from __future__ import annotations

from collections.abc import Callable
import fcntl
import os
from pathlib import Path
import tempfile
import tomllib
from typing import TypeVar

import tomli_w
from pydantic import BaseModel, Field

from ctlrm.models import Participant, Project

_Result = TypeVar("_Result")


class RegistryDurabilityError(OSError):
    """The registry was replaced, but its crash durability could not be confirmed."""


class Registry(BaseModel):
    """Collection of projects identified by their canonical worktree roots."""

    projects: list[Project] = Field(default_factory=list)

    @classmethod
    def from_toml(cls, data: str) -> Registry:
        """Load a registry from TOML text."""
        return cls.model_validate(tomllib.loads(data))

    @classmethod
    def load(cls, path: Path) -> Registry:
        """Read a registry, returning an empty registry only when absent."""
        try:
            data = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls()
        return cls.from_toml(data)

    def to_toml(self) -> str:
        """Serialize the registry to TOML text."""
        return tomli_w.dumps(self.model_dump(mode="json", exclude_none=True))

    def save_atomic(self, path: Path) -> None:
        """Replace a snapshot atomically; use update for concurrent mutations."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(self.to_toml())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            try:
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError as error:
                raise RegistryDurabilityError(
                    f"registry updated at {path}, but durability could not be confirmed: {error}"
                ) from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @classmethod
    def update(cls, path: Path, change: Callable[[Registry], _Result]) -> _Result:
        """Serialize a read-modify-write operation using a persistent lock.

        Parameters
        ----------
        path
            Registry path shared by writers.
        change
            Callback mutating the freshly loaded registry.

        Returns
        -------
        object
            Value returned by the callback after the registry was saved.
        """
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_name(path.name + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            registry = cls.load(path)
            result = change(registry)
            registry.save_atomic(path)
            return result

    def project_for(self, project: Project) -> Project:
        """Find or register a canonical worktree without dropping other entries."""
        root = project.root.resolve()
        for existing in self.projects:
            if existing.root.resolve() == root:
                known = {item.id for item in existing.participants}
                for item in project.participants:
                    if item.id not in known:
                        existing.participants.append(item.model_copy(deep=True))
                        known.add(item.id)
                return existing
        for existing in self.projects:
            if existing.id == project.id:
                raise ValueError(f"project id already belongs to another worktree: {project.id}")
        entry = project.model_copy(deep=True)
        entry.root = root
        self.projects.append(entry)
        return entry

    @classmethod
    def add_participant(cls, path: Path, project: Project, participant: Participant) -> None:
        """Merge a new participant into the latest persisted worktree record."""

        def add(registry: Registry) -> None:
            """Apply one registration without replacing concurrent participants."""
            entry = registry.project_for(project)
            if any(item.id == participant.id for item in entry.participants):
                raise ValueError(f"participant already registered: {participant.id}")
            entry.participants.append(participant)

        cls.update(path, add)
