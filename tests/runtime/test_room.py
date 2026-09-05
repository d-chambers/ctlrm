"""Tests for the filesystem-backed room protocol and CLI."""

from __future__ import annotations

import errno
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ctlrm.__main__ import app
from ctlrm.runtime.manifest import RoleAssignment, RoomManifest
from ctlrm.runtime.messages import MailboxMessage
from ctlrm.runtime.room import RoomRuntime

NOW = "2026-09-04T12:00:00+00:00"


def room(**changes: object) -> RoomManifest:
    """Create a deterministic room definition."""
    values: dict[str, object] = {
        "id": "room-test",
        "author": "coordinator",
        "created_at": NOW,
        "assignments": [
            RoleAssignment(participant="coordinator", role="coordinate"),
            RoleAssignment(participant="reviewer", role="review"),
        ],
        "prompt": "Review the change.",
    }
    values.update(changes)
    return RoomManifest.model_validate(values)


def initialize(runtime: RoomRuntime) -> None:
    """Create the standard test room and author signature."""
    runtime.initialize(
        room(),
        name="Coordinator",
        kind="agent",
        provider="test",
        capabilities=["delegate"],
        joined_at=NOW,
    )


def join_reviewer(runtime: RoomRuntime) -> Path:
    """Join the standard reviewer assignment."""
    return runtime.join_participant(
        participant_id="reviewer",
        name="Réviseur",
        kind="agent",
        provider="test",
        capabilities=["code-review"],
        joined_at=NOW,
    )


def message(**changes: object) -> MailboxMessage:
    """Create a deterministic mailbox message."""
    values: dict[str, object] = {
        "id": "msg-1",
        "from_": "coordinator",
        "to": "reviewer",
        "kind": "request",
        "title": "Vérifier",
        "status": "new",
        "created_at": NOW,
        "body": "Résumé demandé.",
    }
    values.update(changes)
    return MailboxMessage.model_validate(values)


class TestRoomManifest:
    """Canonical prompt and role roster behavior."""

    def test_round_trip(self) -> None:
        """Round trip."""
        definition = room()

        assert RoomManifest.parse(definition.to_markdown()) == definition
        assert definition.role_for("reviewer") == "review"

    def test_author_requires_assignment(self) -> None:
        """Author requires assignment."""
        with pytest.raises(ValueError, match="author must have an assigned role"):
            room(assignments=[RoleAssignment(participant="reviewer", role="review")])

    def test_assignments_are_unique(self) -> None:
        """Assignments are unique."""
        duplicate = [
            RoleAssignment(participant="coordinator", role="coordinate"),
            RoleAssignment(participant="coordinator", role="review"),
        ]

        with pytest.raises(ValueError, match="exactly one assignment"):
            room(assignments=duplicate)


class TestInitialization:
    """Room creation and recovery behavior."""

    def test_initializes_room_and_author_signature(self, tmp_path: Path) -> None:
        """Initializes room and author signature."""
        runtime = RoomRuntime(tmp_path)

        result = runtime.initialize(
            room(),
            name="Coordinator",
            kind="agent",
            provider="test",
            capabilities=[],
            joined_at=NOW,
        )

        assert result.created
        assert result.room_path == tmp_path / ".ctlrm" / "room.md"
        assert runtime.read_signature("coordinator").role == "coordinate"
        assert stat.S_IMODE(result.room_path.stat().st_mode) == 0o660
        assert stat.S_IMODE(runtime.root.stat().st_mode) == 0o2770

    def test_recovers_missing_author_signature(self, tmp_path: Path) -> None:
        """Recovers missing author signature."""
        runtime = RoomRuntime(tmp_path)
        initialize(runtime)
        runtime.signature_path("coordinator").unlink()
        candidate = room(id="ignored-on-resume", created_at="2026-09-04T13:00:00+00:00")

        result = runtime.initialize(
            candidate,
            name="Coordinator",
            kind="agent",
            provider="test",
            capabilities=["delegate"],
            joined_at="2026-09-04T13:00:00+00:00",
        )

        assert not result.created
        assert runtime.read_room().id == "room-test"
        assert runtime.read_signature("coordinator").room_id == "room-test"

    def test_changed_room_cannot_replace_existing(self, tmp_path: Path) -> None:
        """Changed room cannot replace existing."""
        runtime = RoomRuntime(tmp_path)
        initialize(runtime)

        with pytest.raises(FileExistsError):
            runtime.initialize(
                room(prompt="Replace the prompt."),
                name="Coordinator",
                kind="agent",
                provider="test",
                capabilities=["delegate"],
                joined_at=NOW,
            )

        assert runtime.read_room().prompt == "Review the change."

    def test_hard_link_failure_is_clear_and_cleans_temp_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hard link failure is clear and cleans temp file."""
        runtime = RoomRuntime(tmp_path)

        def unsupported_link(source: Path, target: Path) -> None:
            """Unsupported link."""
            raise OSError(errno.EOPNOTSUPP, "unsupported", target)

        monkeypatch.setattr("ctlrm.runtime.room.os.link", unsupported_link)

        with pytest.raises(RuntimeError, match="must support atomic hard links"):
            runtime.write_room(room())

        assert list(runtime.root.glob(".room.md.*")) == []


class TestParticipants:
    """Assigned-role signature behavior."""

    def test_participant_accepts_assigned_role(self, tmp_path: Path) -> None:
        """Participant accepts assigned role."""
        runtime = RoomRuntime(tmp_path)
        initialize(runtime)

        path = join_reviewer(runtime)

        signature = runtime.read_signature("reviewer")
        assert signature.role == "review"
        assert signature.room_id == "room-test"
        assert "Réviseur" in path.read_text(encoding="utf-8")
        inbox = path.with_suffix("") / "inbox"
        assert stat.S_IMODE(inbox.stat().st_mode) == 0o2770

    def test_unassigned_participant_cannot_join(self, tmp_path: Path) -> None:
        """Unassigned participant cannot join."""
        runtime = RoomRuntime(tmp_path)
        initialize(runtime)

        with pytest.raises(ValueError, match="no role assignment"):
            runtime.join_participant(
                participant_id="intruder",
                name="Intruder",
                kind="agent",
                provider=None,
                capabilities=[],
                joined_at=NOW,
            )

        assert not runtime.signature_path("intruder").exists()

    def test_signature_cannot_be_replaced(self, tmp_path: Path) -> None:
        """Signature cannot be replaced."""
        runtime = RoomRuntime(tmp_path)
        initialize(runtime)
        join_reviewer(runtime)

        with pytest.raises(FileExistsError):
            join_reviewer(runtime)

    def test_signature_id_must_match_filename(self, tmp_path: Path) -> None:
        """Signature id must match filename."""
        runtime = RoomRuntime(tmp_path)
        initialize(runtime)
        path = runtime.signature_path("coordinator")
        signature = runtime.read_signature("coordinator").model_copy(
            update={"id": "reviewer", "role": "review"}
        )
        path.write_text(signature.to_yaml(), encoding="utf-8")

        with pytest.raises(ValueError, match="does not match room assignment"):
            runtime.read_signature("coordinator")


class TestMessages:
    """Immutable mailbox delivery behavior."""

    def test_registered_participant_sends_message(self, tmp_path: Path) -> None:
        """Registered participant sends message."""
        runtime = RoomRuntime(tmp_path)
        initialize(runtime)
        join_reviewer(runtime)

        path = runtime.send_message(message())

        assert path.name == "msg-1.md"
        assert runtime.read_inbox("reviewer") == [message()]

    def test_duplicate_message_cannot_replace_content(self, tmp_path: Path) -> None:
        """Duplicate message cannot replace content."""
        runtime = RoomRuntime(tmp_path)
        initialize(runtime)
        join_reviewer(runtime)
        runtime.send_message(message())

        with pytest.raises(FileExistsError):
            runtime.send_message(message(body="Replacement"))

        assert runtime.read_inbox("reviewer")[0].body == "Résumé demandé."


class TestCli:
    """User-facing Typer command behavior."""

    def test_single_ctlrm_entrypoint_workflow(self, tmp_path: Path) -> None:
        """Single ctlrm entrypoint workflow."""
        runner = CliRunner()
        prefix = ["--root", str(tmp_path)]

        created = runner.invoke(
            app,
            [
                *prefix,
                "init",
                "--id",
                "coordinator",
                "--name",
                "Coordinator",
                "--assign",
                "coordinator=coordinate",
                "--assign",
                "reviewer=review",
                "--prompt",
                "Review the change.",
            ],
        )
        joined = runner.invoke(
            app,
            [
                *prefix,
                "join",
                "--id",
                "reviewer",
                "--name",
                "Reviewer",
                "--restart-command",
                "claude --resume session-123",
            ],
        )
        sent = runner.invoke(
            app,
            [
                *prefix,
                "send",
                "--from",
                "coordinator",
                "--to",
                "reviewer",
                "--title",
                "Review",
                "--body",
                "Please review.",
            ],
        )
        listed = runner.invoke(app, [*prefix, "inbox", "--participant", "reviewer"])

        assert created.exit_code == 0, created.output
        assert joined.exit_code == 0, joined.output
        assert sent.exit_code == 0, sent.output
        assert listed.exit_code == 0, listed.output
        assert "coordinate" in (
            tmp_path / ".ctlrm" / "participants" / "coordinator.yaml"
        ).read_text(encoding="utf-8")
        assert (
            RoomRuntime(tmp_path).read_signature("reviewer").restart_command
            == "claude --resume session-123"
        )
        assert "Review" in listed.output

    def test_join_has_no_role_option(self) -> None:
        """Join has no role option."""
        result = CliRunner().invoke(app, ["join", "--help"])

        assert result.exit_code == 0
        assert "--role" not in result.output

    def test_invalid_assignment_has_validation_exit(self, tmp_path: Path) -> None:
        """Invalid assignment has validation exit."""
        result = CliRunner().invoke(
            app,
            [
                "--root",
                str(tmp_path),
                "init",
                "--id",
                "coordinator",
                "--name",
                "Coordinator",
                "--assign",
                "not-an-assignment",
                "--prompt",
                "Prompt",
            ],
        )

        assert result.exit_code == 2
        assert "invalid:" in result.output

    def test_malformed_room_has_validation_exit(self, tmp_path: Path) -> None:
        """Malformed room has validation exit."""
        runtime = RoomRuntime(tmp_path)
        runtime.init_project()
        runtime.room_path.write_text("---\n[]\n---\nPrompt\n", encoding="utf-8")

        result = CliRunner().invoke(
            app,
            [
                "--root",
                str(tmp_path),
                "join",
                "--id",
                "reviewer",
                "--name",
                "Reviewer",
            ],
        )

        assert result.exit_code == 2
        assert "front matter must be a mapping" in result.output

    def test_body_sources_are_mutually_exclusive(self) -> None:
        """Body sources are mutually exclusive."""
        result = CliRunner().invoke(
            app,
            [
                "send",
                "--from",
                "sender",
                "--to",
                "recipient",
                "--title",
                "Title",
                "--body",
                "Body",
                "--body-file",
                "body.md",
            ],
        )

        assert result.exit_code == 2


class TestRecoveryBoundaries:
    """Regression coverage for malformed files and initialization retries."""

    def test_prompt_whitespace_retry(self, tmp_path: Path) -> None:
        """A file-style trailing newline does not turn a retry into a conflict."""
        runtime = RoomRuntime(tmp_path)
        for _ in range(2):
            result = runtime.initialize(
                room(prompt="\nReview the change.\n"),
                name="Coordinator",
                kind="agent",
                provider="test",
                capabilities=[],
                joined_at=NOW,
            )
        assert not result.created
        assert runtime.read_room().prompt == "Review the change."

    @pytest.mark.parametrize(
        "changes", [{"to": "coordinator"}, {"id": "different"}, {"from_": "unregistered"}]
    )
    def test_rejects_misrouted_files(self, tmp_path: Path, changes: dict[str, str]) -> None:
        """Direct file delivery must satisfy the same routing checks as sending."""
        runtime = RoomRuntime(tmp_path)
        initialize(runtime)
        join_reviewer(runtime)
        path = runtime.root / "participants/reviewer/inbox/msg-1.md"
        path.write_text(message(**changes).to_markdown())
        with pytest.raises(ValueError, match="invalid mailbox message"):
            runtime.read_inbox("reviewer")

    def test_cli_keeps_valid_mail_visible(self, tmp_path: Path) -> None:
        """One malformed neighbor is diagnosed without hiding delivered work."""
        runtime = RoomRuntime(tmp_path)
        initialize(runtime)
        join_reviewer(runtime)
        runtime.send_message(message())
        invalid = runtime.root / "participants/reviewer/inbox/broken.md"
        invalid.write_text("not a message")
        result = CliRunner().invoke(
            app, ["--root", str(tmp_path), "inbox", "--participant", "reviewer"]
        )
        assert result.exit_code == 2
        assert "Vérifier" in result.stdout
        assert "broken.md" in result.stderr

    def test_cli_reports_yaml_syntax(self, tmp_path: Path) -> None:
        """YAML parser exceptions follow the documented validation exit code."""
        runtime = RoomRuntime(tmp_path)
        runtime.init_project()
        runtime.room_path.write_text("---\nid: [\n---\nPrompt\n")
        result = CliRunner().invoke(
            app, ["--root", str(tmp_path), "join", "--id", "reviewer", "--name", "Reviewer"]
        )
        assert result.exit_code == 2
        assert "invalid:" in result.stderr

    @pytest.mark.parametrize("value", ["not-a-date", "2026-09-05T12:00:00", None])
    def test_timestamp_validation(self, value: object) -> None:
        """Malformed or timezone-free timestamps are not valid protocol data."""
        with pytest.raises(ValueError):
            message(created_at=value)

    @pytest.mark.parametrize("value", ["../escape", "/absolute", ".", "a/b", "a\\b"])
    def test_identifier_validation(self, value: str) -> None:
        """All room/message path components remain within their directories."""
        with pytest.raises(ValueError):
            message(id=value)

    def test_default_root_is_call_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Library callers can change directories after importing the CLI."""
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(
            app, ["init", "--id", "me", "--name", "Me", "--assign", "me=work", "--prompt", "Task"]
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / ".ctlrm/room.md").is_file()


class TestYamlBoundaries:
    """Ambiguous YAML must not change who owns a room or message."""

    def test_duplicate_author(self) -> None:
        """Two author keys cannot silently select the last one."""
        text = (
            room()
            .to_markdown()
            .replace("author: coordinator", "author: coordinator\nauthor: reviewer")
        )
        with pytest.raises(ValueError, match="duplicate YAML key"):
            RoomManifest.parse(text)

    def test_non_string_key(self) -> None:
        """Implicit YAML booleans are not valid protocol field names."""
        text = room().to_markdown().replace("id: room-test", "id: room-test\nyes: ignored")
        with pytest.raises(ValueError, match="keys must be strings"):
            RoomManifest.parse(text)


class TestSharedRoomOwnership:
    """Joining must work with shared writable directories owned by another user."""

    def test_existing_directories_are_not_chmodded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Group membership allows creation, not chmod of the author's directories."""
        runtime = RoomRuntime(tmp_path)
        initialize(runtime)
        shared = {runtime.root, runtime.root / "participants"}
        original = Path.chmod

        def chmod(path: Path, mode: int, *, follow_symlinks: bool = True) -> None:
            """Simulate Unix ownership on the author's shared directories."""
            if path in shared:
                raise PermissionError("not the directory owner")
            original(path, mode, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(Path, "chmod", chmod)
        join_reviewer(runtime)
        assert runtime.read_signature("reviewer").role == "review"

    @pytest.mark.parametrize("identifier", ["alice.yaml", "alice.YAML"])
    def test_reserved_signature_suffix(self, identifier: str) -> None:
        """Participant directories must not collide with signature filenames."""
        with pytest.raises(ValueError, match="reserved .yaml"):
            RoleAssignment(participant=identifier, role="review")


class TestConflictDiagnostics:
    """Conflicts identify the immutable destination, not a temporary link source."""

    def test_duplicate_message_names_destination(self, tmp_path: Path) -> None:
        """CLI callers can use the error filename to locate the existing record."""
        runtime = RoomRuntime(tmp_path)
        initialize(runtime)
        join_reviewer(runtime)
        destination = runtime.send_message(message())
        with pytest.raises(FileExistsError) as caught:
            runtime.send_message(message())
        assert caught.value.filename == str(destination)


class TestRoomDirectoryDurability:
    """A successful room operation persists the directory chain to its records."""

    def test_parent_directories_are_synced(self, tmp_path: Path, monkeypatch) -> None:
        """Creation and retry both sync every mailbox ancestor within the project."""
        import os
        import stat

        synced: set[int] = set()
        original = os.fsync

        def record(descriptor: int) -> None:
            """Observe durability barriers while preserving real filesystem calls."""
            info = os.fstat(descriptor)
            if stat.S_ISDIR(info.st_mode):
                synced.add(info.st_ino)
            original(descriptor)

        monkeypatch.setattr(os, "fsync", record)
        runtime = RoomRuntime(tmp_path)
        for _ in range(2):
            synced.clear()
            runtime.init_participant("worker")
            inbox = runtime.root / "participants/worker/inbox"
            chain = [tmp_path, runtime.root, inbox.parent.parent, inbox.parent, inbox]
            assert {path.stat().st_ino for path in chain} <= synced


class TestMarkdownWhitespace:
    """Round trips preserve indentation and Markdown hard line breaks."""

    @pytest.mark.parametrize("body", ["    first()\n    second()", "line  ", "\tcode()"])
    def test_message_formatting(self, tmp_path: Path, body: str) -> None:
        """Mailbox delivery must not change meaningful spaces or tabs."""
        runtime = RoomRuntime(tmp_path)
        initialize(runtime)
        join_reviewer(runtime)
        runtime.send_message(message(body=body))
        assert runtime.read_inbox("reviewer")[0].body == body

    def test_prompt_formatting(self) -> None:
        """Prompt normalization removes newlines while retaining indentation."""
        prompt = "    first()\n    second()  "
        manifest = room(prompt=prompt)
        assert RoomManifest.parse(manifest.to_markdown()).prompt == prompt


class TestPromptLineEndings:
    """Prompts from binary stdin normalize like prompts read from disk."""

    @pytest.mark.parametrize("newline", ["\r\n", "\r"])
    def test_retry_with_crlf(self, tmp_path: Path, newline: str) -> None:
        """The same prompt remains resumable after universal newline decoding."""
        runtime = RoomRuntime(tmp_path)
        manifest = room(prompt=f"First{newline}    Second{newline}")
        for _ in range(2):
            runtime.initialize(
                manifest,
                name="Coordinator",
                kind="human",
                provider=None,
                capabilities=[],
                joined_at="2026-09-05T12:00:00+00:00",
            )
        assert runtime.read_room().prompt == "First\n    Second"


class TestRoomRecoveryFailures:
    """Recovery preserves explicit roots and shared mailbox ownership."""

    def test_missing_root(self, tmp_path: Path) -> None:
        """A typo must not create a new project tree."""
        with pytest.raises(NotADirectoryError, match="already exist"):
            initialize(RoomRuntime(tmp_path / "typo"))
        assert not (tmp_path / "typo").exists()

    def test_missing_inbox(self, tmp_path: Path) -> None:
        """Recreated recipient mailboxes retain group write access."""
        runtime = RoomRuntime(tmp_path)
        initialize(runtime)
        join_reviewer(runtime)
        inbox = runtime.root / "participants/reviewer/inbox"
        inbox.rmdir()
        runtime.send_message(message())
        assert inbox.stat().st_mode & 0o2770 == 0o2770

    def test_orphaned_signature(self, tmp_path: Path) -> None:
        """A missing manifest cannot be replaced over another room's identities."""
        runtime = RoomRuntime(tmp_path)
        initialize(runtime)
        runtime.room_path.unlink()
        with pytest.raises(ValueError, match="restore room.md"):
            initialize(runtime)
        assert not runtime.room_path.exists()


class TestConcurrentInitialization:
    """A concurrently completed matching room remains resumable."""

    def test_manifest_appears_during_scan(self, tmp_path: Path, monkeypatch) -> None:
        """Recheck the manifest after observing another initializer's signatures."""
        runtime = RoomRuntime(tmp_path)
        original = Path.exists
        room_path = runtime.room_path
        injected = False

        def exists(path: Path) -> bool:
            """Complete another initialization after the first absence observation."""
            nonlocal injected
            if path == room_path and not injected:
                injected = True
                initialize(runtime)
                return False
            return original(path)

        monkeypatch.setattr(Path, "exists", exists)
        initialize(runtime)
        assert runtime.read_signature("coordinator").id == "coordinator"


class TestSharedDirectories:
    """Terminal initialization uses the same shared directory contract."""

    def test_shared_initialization(self, tmp_path: Path) -> None:
        """Launching first does not prevent later shared-group room access."""
        from ctlrm.runtime import ProjectRuntime

        ProjectRuntime(tmp_path).init_participant("worker")
        runtime = RoomRuntime(tmp_path)
        initialize(runtime)
        assert runtime.root.stat().st_mode & 0o2770 == 0o2770
        assert (runtime.root / "participants").stat().st_mode & 0o2770 == 0o2770
