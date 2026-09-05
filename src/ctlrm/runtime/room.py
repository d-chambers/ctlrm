"""Filesystem operations for a shared ctlrm room."""

from __future__ import annotations

import errno
import os
import tempfile
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path

from ctlrm.runtime import ProjectRuntime
from ctlrm.runtime.filesystem import mkdir_shared as _mkdir_shared
from ctlrm.runtime.manifest import RoomManifest
from ctlrm.runtime.messages import MailboxMessage
from ctlrm.runtime.participants import ParticipantSignature, validate_participant_id

_FILE_MODE = 0o660
_UNSUPPORTED_LINK_ERRNOS = {errno.EPERM, errno.EXDEV, errno.EOPNOTSUPP}


def _write_exclusive_atomic(path: Path, text: str) -> None:
    """Atomically create a file without replacing an existing owner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
            encoding="utf-8",
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.fchmod(temporary.fileno(), _FILE_MODE)
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_path, path)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise FileExistsError(errno.EEXIST, "record already exists", str(path)) from exc
            if exc.errno in _UNSUPPORTED_LINK_ERRNOS:
                raise RuntimeError("room storage must support atomic hard links") from exc
            raise
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class InitializationResult:
    """Paths created or recovered while initializing a room."""

    room_path: Path
    signature_path: Path
    created: bool


class RoomRuntime(ProjectRuntime):
    """Manage a room definition, participant signatures, and messages."""

    def init_project(self) -> None:
        """Create the minimal room directories for a shared Unix group."""
        if not self.project_root.is_dir():
            raise NotADirectoryError(f"project root must already exist: {self.project_root}")
        _mkdir_shared(self.root)
        _mkdir_shared(self.root / "participants")

    def init_participant(self, participant_id: str) -> None:
        """Create the inbox directory for one safe participant identifier."""
        validate_participant_id(participant_id)
        inbox = self.root / "participants" / participant_id / "inbox"
        self.init_project()
        _mkdir_shared(inbox.parent)
        _mkdir_shared(inbox)

    @property
    def room_path(self) -> Path:
        """Return the canonical room manifest path."""
        return self.root / "room.md"

    def write_room(self, room: RoomManifest) -> Path:
        """Create the immutable room prompt and role roster."""
        self.init_project()
        _write_exclusive_atomic(self.room_path, room.to_markdown())
        return self.room_path

    def read_room(self) -> RoomManifest:
        """Read the canonical room prompt and role roster."""
        return RoomManifest.read(self.room_path)

    def initialize(
        self,
        room: RoomManifest,
        *,
        name: str,
        kind: str,
        provider: str | None,
        capabilities: list[str],
        joined_at: str,
        restart_command: str | None = None,
    ) -> InitializationResult:
        """Initialize a room and resumably register its author.

        If a matching room already exists for the same author, this method resumes
        registration instead of replacing the room. A different prompt, roster, or
        author remains a conflict.
        """
        if not self.room_path.exists() and (self.root / "participants").exists():
            signatures = sorted((self.root / "participants").glob("*.yaml"))
            if signatures and not self.room_path.exists():
                raise ValueError(
                    f"room manifest is missing but participant data exists: {signatures[0]}; "
                    "restore room.md or use a fresh coordination directory"
                )
        created = True
        try:
            self.write_room(room)
        except FileExistsError:
            existing = self.read_room()
            expected = room.model_copy(
                update={"id": existing.id, "created_at": existing.created_at}
            )
            if existing != expected:
                raise
            room = existing
            created = False
        signature_path = self._join(
            room,
            participant_id=room.author,
            name=name,
            kind=kind,
            provider=provider,
            capabilities=capabilities,
            joined_at=joined_at,
            restart_command=restart_command,
            resume=True,
        )
        return InitializationResult(self.room_path, signature_path, created)

    def join_participant(
        self,
        *,
        participant_id: str,
        name: str,
        kind: str,
        provider: str | None,
        capabilities: list[str],
        joined_at: str,
        restart_command: str | None = None,
    ) -> Path:
        """Create a participant signature using the role assigned by the author."""
        return self._join(
            self.read_room(),
            participant_id=participant_id,
            name=name,
            kind=kind,
            provider=provider,
            capabilities=capabilities,
            joined_at=joined_at,
            restart_command=restart_command,
            resume=False,
        )

    def _join(
        self,
        room: RoomManifest,
        *,
        participant_id: str,
        name: str,
        kind: str,
        provider: str | None,
        capabilities: list[str],
        joined_at: str,
        restart_command: str | None,
        resume: bool,
    ) -> Path:
        """Write a role-bound signature, optionally accepting an identical existing one."""
        signature = ParticipantSignature(
            room_id=room.id,
            id=participant_id,
            name=name,
            role=room.role_for(participant_id),
            kind=kind,
            provider=provider,
            capabilities=capabilities,
            joined_at=joined_at,
            restart_command=restart_command,
        )
        self.init_project()
        self.init_participant(participant_id)
        path = self.signature_path(participant_id)
        try:
            _write_exclusive_atomic(path, signature.to_yaml())
        except FileExistsError:
            existing = self.read_signature(participant_id)
            stable_fields = {
                "room_id",
                "id",
                "name",
                "role",
                "kind",
                "provider",
                "capabilities",
                "restart_command",
            }
            if not resume or existing.model_dump(include=stable_fields) != signature.model_dump(
                include=stable_fields
            ):
                raise
        return path

    def signature_path(self, participant_id: str) -> Path:
        """Return the direct-child signature path for a participant."""
        validate_participant_id(participant_id)
        return self.root / "participants" / f"{participant_id}.yaml"

    def read_signature(self, participant_id: str) -> ParticipantSignature:
        """Read one signature and verify it against the room assignment."""
        signature = ParticipantSignature.read(self.signature_path(participant_id))
        room = self.read_room()
        matches_room = (
            signature.id == participant_id
            and signature.room_id == room.id
            and signature.role == room.role_for(signature.id)
        )
        if not matches_room:
            raise ValueError(
                f"participant signature does not match room assignment: {participant_id}"
            )
        return signature

    def list_signatures(
        self, *, on_error: Callable[[Path, Exception], None] | None = None
    ) -> list[ParticipantSignature]:
        """Read every signature, naming the malformed path on failure."""
        self.read_room()
        participants = self.root / "participants"
        if not participants.exists():
            return []
        signatures = []
        for path in sorted(participants.glob("*.yaml")):
            try:
                signatures.append(self.read_signature(path.stem))
            except Exception as exc:
                if on_error is None:
                    raise ValueError(f"invalid participant signature: {path}") from exc
                on_error(path, exc)
        return signatures

    def send_message(self, message: MailboxMessage) -> Path:
        """Deliver an immutable message between registered participants."""
        self.read_signature(message.from_)
        self.read_signature(message.to)
        self.init_participant(message.to)
        path = self.root / "participants" / message.to / "inbox" / f"{message.id}.md"
        _write_exclusive_atomic(path, message.to_markdown())
        return path

    def read_inbox(
        self, participant_id: str, *, on_error: Callable[[Path, Exception], None] | None = None
    ) -> list[MailboxMessage]:
        """Read every inbox message, naming the malformed path on failure."""
        self.read_signature(participant_id)
        inbox = self.root / "participants" / participant_id / "inbox"
        messages = []
        for path in sorted(inbox.glob("*.md")):
            try:
                message = MailboxMessage.read(path)
                if message.id != path.stem or message.to != participant_id:
                    raise ValueError("message id or recipient does not match its inbox path")
                self.read_signature(message.from_)
                messages.append(message)
            except Exception as exc:
                if on_error is None:
                    raise ValueError(f"invalid mailbox message: {path}") from exc
                on_error(path, exc)
        return messages
