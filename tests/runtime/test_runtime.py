"""test runtime.py."""

from pathlib import Path
from ctlrm.runtime import ProjectRuntime
from ctlrm.runtime.messages import MailboxMessage
from ctlrm.runtime.state import ParticipantStateFile


class TestRuntime:
    """TestRuntime."""

    def test_initializes_ctlrm_directory(self, tmp_path: Path) -> None:
        """test initializes ctlrm directory."""
        runtime = ProjectRuntime(tmp_path)
        runtime.init_project()
        runtime.init_participant("derrick")
        assert (tmp_path / ".ctlrm" / "participants" / "derrick" / "inbox").is_dir()
        assert (tmp_path / ".ctlrm" / "participants" / "derrick" / "outbox").is_dir()
        assert (tmp_path / ".ctlrm" / "workflows").is_dir()
        assert (tmp_path / ".ctlrm" / "runs").is_dir()

    def test_parses_mailbox_message(self) -> None:
        """test parses mailbox message."""
        message = MailboxMessage.parse(
            "---\nid: msg-1\nfrom: codex-impl\nto: derrick\nkind: question\ntitle: Need decision\nstatus: new\ncreated_at: 2026-06-20T16:30:00+02:00\nfiles:\n  - src/ctlrm/tmux.py\n---\nShould restart reuse the same pane?\n"
        )
        assert message.id == "msg-1"
        assert message.to == "derrick"
        assert message.body.strip() == "Should restart reuse the same pane?"

    def test_parses_participant_state_file(self) -> None:
        """test parses participant state file."""
        state = ParticipantStateFile.parse(
            "---\nparticipant_id: derrick\nkind: human\nstate: idle\nactive: false\nupdated_at: 2026-06-20T16:30:00+02:00\nsummary: Waiting for review\n---\nNo active task.\n"
        )
        assert state.participant_id == "derrick"
        assert state.kind == "human"
        assert state.active is False
