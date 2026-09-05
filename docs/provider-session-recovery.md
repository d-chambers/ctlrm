# Provider Session Recovery

Date: 2026-09-05

## Capability matrix

| Provider | Installed version | Native session identity | Resume command | Evidence |
| --- | --- | --- | --- | --- |
| Claude Code | 2.1.261 | Controller allocates a UUID through `--session-id`; agent acknowledges it in its recovery response | `claude --resume <session-id>` with the same transport options | Passed: native ID and previously supplied conversation token retained after SIGKILL |
| Codex | 0.153.4 | To be verified in the second-provider milestone | Not yet verified | No recovery claim yet |

Terminal management used libtmux 0.62.0 and tmux 3.4 on Linux. The probe ran in a disposable linked Git worktree on its own branch and isolated tmux socket with `/dev/null` as the tmux configuration. It did not access application source or credentials through agent tools.

## Verified Claude transport

The probe used Claude's persistent print-mode stream interface, with a FIFO kept open for successive user messages and JSONL output written to a temporary evidence file. This verifies native conversation persistence and restart in a managed terminal; it does not yet verify the interactive full-screen UI or the future ctlrm mailbox bootstrap.

Initial argument vector:

```text
claude --print --input-format stream-json --output-format stream-json --verbose --tools '' --strict-mcp-config --permission-mode plan --session-id <uuid>
```

Resume argument vector:

```text
claude --print --input-format stream-json --output-format stream-json --verbose --tools '' --strict-mcp-config --permission-mode plan --resume <same-uuid>
```

Here `''` denotes one empty argument, disabling tools for this bounded probe. Production profiles must specify their intended tools and permissions separately. Session persistence must remain enabled; `--no-session-persistence` is incompatible with this contract. The probe had no `ANTHROPIC_*`, `CLAUDE_*`, or `CLAUDECODE` environment variables set, including no `CLAUDE_CONFIG_DIR` override. It used the existing authenticated local Claude installation and the default native state context under `~/.claude` (`~/.claude/projects` was present). Resume ran as the same OS user, with the same home directory and workdir. No extra provider environment names were required for this tested profile; `PATH` must resolve the same Claude executable. Other profiles must record their required variable names and explicit state-directory context. No credential values were captured in the recovery record.

Each input line was a JSON object with `type: user` and a `message` containing `role: user` and text `content`. The first prompt supplied a random conversation token plus the allocated session ID and requested readiness and restart instructions. The agent returned `ready: true`, the supplied session ID, and `resume_argv` referring to that exact ID. A successful provider result event recorded the same native session ID.

After that first turn completed, the probe identified the provider child of its managed shell and sent SIGKILL to that child while it awaited more input. It started another managed terminal using `--resume` and requested status reconciliation and the remembered token without including that token in the resume prompt. The response returned `ready: true` and the exact original token; the result event retained the original session ID. Both provider result events reported success. The isolated tmux server was then stopped.

This is evidence of recovery after a completed turn followed by unexpected process death. It does not prove recovery of an unfinished tool call, exactly-once effects, missing state files, changed credentials, or host reboot. Those cases require the planned uncertain-work and replacement paths.

## Adapter implications

- Allocate or discover the native ID before declaring managed readiness; require the agent's acknowledgment and resume instructions.
- Preserve the native state directory/provider context and the transport options alongside executable arguments. An absent session or unavailable provider context is a recovery failure, never evidence that a new conversation successfully resumed.
- Readiness must refer to the current managed generation as well as the native session ID.
- Supervise the provider process, not just the pane. The successful probe retained a shell parent; direct `exec` startup attempts ended with SIGHUP in this environment and are not the validated launch path. Diagnose that difference in the terminal/provider adapter implementation rather than assuming both forms work.
- Use libtmux with an isolated/configured server and an explicit shell wrapper for the verified path. In the probe, the shell and provider were distinct processes, so the shell surviving provider death did not count as recovery.
- Resume with a status query for the prior work ID. The token test establishes context retention, not whether arbitrary interrupted work already took effect.
- Keep machine-specific logs, session IDs, and the disposable token under temporary `.scratch/`; the committed capability matrix contains the reproducible procedure and its measured limits.

## Reproduction checklist

1. Create a disposable linked worktree and isolated libtmux server; open an explicit shell pane and retain the FIFO's writer endpoint.
2. Launch the initial argument vector with a new UUID. Supply an unpredictable token and ask for a recovery acknowledgment.
3. Wait for a successful result carrying that UUID, then terminate only that provider child with SIGKILL.
4. Launch the resume vector using the same provider environment and workdir; request the earlier token without resupplying it.
5. Assert identical native IDs, successful result events, readiness, and exact token equality. Stop only the isolated test server and preserve any wanted evidence before manual cleanup.
