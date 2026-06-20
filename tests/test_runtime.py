from pathlib import Path

from ctlrm.runtime import ProjectRuntime
from ctlrm.runtime.messages import MailboxMessage
from ctlrm.runtime.state import ParticipantStateFile


def test_initializes_ctlrm_directory(tmp_path: Path) -> None:
    runtime = ProjectRuntime(tmp_path)

    runtime.init_project()
    runtime.init_participant("derrick")

    assert (tmp_path / ".ctlrm" / "participants" / "derrick" / "inbox").is_dir()
    assert (tmp_path / ".ctlrm" / "participants" / "derrick" / "outbox").is_dir()
    assert (tmp_path / ".ctlrm" / "workflows").is_dir()
    assert (tmp_path / ".ctlrm" / "runs").is_dir()


def test_parses_mailbox_message() -> None:
    message = MailboxMessage.parse(
        """---
id: msg-1
from: codex-impl
to: derrick
kind: question
title: Need decision
status: new
created_at: 2026-06-20T16:30:00+02:00
files:
  - src/ctlrm/tmux.py
---
Should restart reuse the same pane?
"""
    )

    assert message.id == "msg-1"
    assert message.to == "derrick"
    assert message.body.strip() == "Should restart reuse the same pane?"


def test_parses_participant_state_file() -> None:
    state = ParticipantStateFile.parse(
        """---
participant_id: derrick
kind: human
state: idle
active: false
updated_at: 2026-06-20T16:30:00+02:00
summary: Waiting for review
---
No active task.
"""
    )

    assert state.participant_id == "derrick"
    assert state.kind == "human"
    assert state.active is False
