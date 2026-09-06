"""Filesystem operations for a shared ctlrm room."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path

from ctlrm.runtime.filesystem import mkdir_shared, write_exclusive_atomic
from ctlrm.runtime.manifest import RoomManifest
from ctlrm.runtime.location import runtime_path
from ctlrm.runtime.messages import MailboxMessage
from ctlrm.runtime.participants import ParticipantSignature, validate_participant_id


@dataclass(frozen=True)
class InitializationResult:
    """Paths created or recovered while initializing a room."""

    room_path: Path
    signature_path: Path
    created: bool


class RoomRuntime:
    """Manage a room definition, participant signatures, and messages."""

    def __init__(self, project_root: Path) -> None:
        """Initialize paths for a project runtime."""
        self.project_root = project_root

    @property
    def root(self) -> Path:
        """Resolve storage only for operations that need a local runtime."""
        return runtime_path(self.project_root)

    def ensure_writable(self) -> None:
        """Reject public runtime mutations after a managed job has retired."""
        if (self.root / "area.yaml").exists():
            from ctlrm.managed.storage import Journal

            if Journal(self.project_root).replay()[0].get("retired"):
                raise ValueError("job is retired; its runtime is read-only")

    def init_project(self) -> None:
        """Create the minimal room directories for a shared Unix group."""
        self.ensure_writable()
        if not self.project_root.is_dir():
            raise NotADirectoryError(f"project root must already exist: {self.project_root}")
        if self.root != self.project_root / ".ctlrm" and not (self.root / "area.yaml").exists():
            raise ValueError("central rooms must be initialized through job start")
        mkdir_shared(self.root)
        mkdir_shared(self.root / "participants")

    def init_participant(self, participant_id: str) -> None:
        """Create the inbox directory for one safe participant identifier."""
        validate_participant_id(participant_id)
        inbox = self.root / "participants" / participant_id / "inbox"
        self.init_project()
        mkdir_shared(inbox.parent)
        mkdir_shared(inbox)

    @property
    def room_path(self) -> Path:
        """Return the canonical room manifest path."""
        return self.root / "room.md"

    def write_room(self, room: RoomManifest) -> Path:
        """Create the immutable room prompt and role roster."""
        self.init_project()
        write_exclusive_atomic(self.room_path, room.to_markdown())
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
        resume: bool = False,
    ) -> Path:
        """Accept the assigned role, optionally resuming an identical signature."""
        return self._join(
            self.read_room(),
            participant_id=participant_id,
            name=name,
            kind=kind,
            provider=provider,
            capabilities=capabilities,
            joined_at=joined_at,
            restart_command=restart_command,
            resume=resume,
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
            write_exclusive_atomic(path, signature.to_yaml())
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
        write_exclusive_atomic(path, message.to_markdown())
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
