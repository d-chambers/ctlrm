# Ctlrm directory protocol

```text
.ctlrm/
├── room.md
└── participants/
    ├── coordinator.yaml
    ├── coordinator/
    │   └── inbox/
    ├── reviewer.yaml
    └── reviewer/
        └── inbox/
```

## Room definition

`room.md` is an immutable Markdown document with YAML front matter. The body is the first participant's prompt. The front matter records `protocol_version`, a room `id`, the `author`, `created_at`, and the complete list of participant-to-role `assignments`.

The author must assign itself a role. Participant IDs must be unique, and every participant must have exactly one role. Only assigned participant IDs can join.

```bash
ctlrm --root /path/to/project init \
  --id coordinator \
  --name Coordinator \
  --provider codex \
  --assign coordinator=coordinate \
  --assign reviewer=review \
  --prompt-file /path/to/prompt.md
```

Room creation is exclusive. A repeated `init` by the same author with the same prompt and assignments repairs a missing author signature and otherwise leaves the room unchanged. A different author, prompt, or roster is a conflict.

## Participant signatures

Each participant accepts its preassigned role by invoking:

```bash
ctlrm --root /path/to/project join \
  --id reviewer \
  --name Reviewer \
  --provider claude \
  --capability code-review
```

`join` has no role option. It reads the role from `room.md` and creates `participants/<id>.yaml` plus that participant's inbox. A signature contains `protocol_version`, `room_id`, `id`, `name`, `role`, `kind`, optional `provider`, `joined_at`, and optional `capabilities`.

## Messaging

```bash
ctlrm --root /path/to/project send \
  --from coordinator \
  --to reviewer \
  --kind review_request \
  --title "Review the implementation" \
  --file src/ctlrm/runtime/room.py \
  --body "Check role enforcement and recovery."

ctlrm --root /path/to/project inbox --participant reviewer
ctlrm --root /path/to/project participants
```

Both endpoints must have valid signatures bound to the current room and their assigned roles. Duplicate message IDs return conflict exit code 3. Validation returns 2, missing files return 4, and storage failures return 5.

## Ownership and storage

The CLI exclusively creates `room.md`, signatures, and messages. By convention, the first participant authors `room.md`, each participant authors only its own signature, and each declared sender authors its messages. These rules are cooperative rather than authenticated; any process with directory write access can unlink or forge files.

Ctlrm creates files with mode `0660` and directories with mode `2770`. Participants running as different users must share the directory's Unix group. Exclusive creation requires a POSIX-style filesystem supporting hard links and directory `fsync`; ctlrm reports a storage error rather than weakening the no-overwrite guarantee.
