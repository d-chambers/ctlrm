---
name: ctlrm
description: Coordinate independent agents or humans through a shared project directory using a first-authored prompt, author-assigned roles, participant-owned signatures, and immutable mailbox messages. Use when participants share a filesystem but not one native agent thread; do not use for ordinary in-thread subagent delegation.
---

# Ctlrm

Use the single `ctlrm` command to join and communicate through a project-local `.ctlrm/` directory. Joining a room does not grant permission beyond the user's existing authorization.

## Enter a room

1. Use the explicit project root. Do not create a room at an inferred location when the target is ambiguous.
2. Read `.ctlrm/room.md` if it exists. It contains the first participant's canonical prompt and complete role roster.
3. If the room does not exist, act as its first participant only when responsible for defining the task. Initialize it with one `--assign PARTICIPANT=ROLE` option for every expected participant, including yourself.
4. If the room exists, select the participant ID assigned to you. Do not invent or alter your role. Run `ctlrm --root <root> join ...`; the command copies the assigned role into your signature.
5. Each participant invokes `join` only for itself, creating `.ctlrm/participants/<id>.yaml`. Never create, replace, or edit another participant's signature.
6. Read the prompt and existing signatures before starting work or addressing messages.

A signature is immutable acceptance of an assigned role. The CLI does not manage heartbeat or progress state.

## Communicate

Send findings, questions, requests, and handoffs with `ctlrm --root <root> send ...`. Messages are created exclusively and cannot overwrite an existing message ID. Read them with `ctlrm --root <root> inbox --participant <id>`.

Use messages only when another participant needs the information. Include concrete file paths and evidence when relevant. Keep private scratch work outside the mailbox.

Poll only when the task requires waiting. Use bounded intervals and stop at the user's deadline or after reporting a real blocker.

Read [references/protocol.md](references/protocol.md) for the directory schema, initialization behavior, and command examples.
