"""Mailbox text parsing preserves metadata and multiline bodies."""

from ctlrm.runtime.messages import MailboxMessage


class TestMailboxMessage:
    """Messages remain independently readable from immutable mailbox files."""

    def test_parses_mailbox_message(self) -> None:
        """test parses mailbox message."""
        message = MailboxMessage.parse(
            "---\nid: msg-1\nfrom: codex-impl\nto: derrick\nkind: question\ntitle: Need decision\nstatus: new\ncreated_at: 2026-06-20T16:30:00+02:00\nfiles:\n  - src/ctlrm/tmux.py\n---\nShould restart reuse the same pane?\n"
        )
        assert message.id == "msg-1"
        assert message.to == "derrick"
        assert message.body.strip() == "Should restart reuse the same pane?"
