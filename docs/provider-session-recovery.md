# Provider Session Recovery

Date: 2026-09-05

## Capability matrix

| Provider | Installed version | Native session identity | Resume command | Evidence |
| --- | --- | --- | --- | --- |
| Claude Code | 2.1.261 | Controller allocates a UUID through `--session-id`; agent acknowledges it in its recovery response | `claude --resume <session-id>` with the same transport options | Passed in streamed and interactive terminal modes: native ID and conversation token retained after SIGKILL |
| Codex | 0.153.4 | To be verified in the second-provider milestone | Not yet verified | No recovery claim yet |

Terminal management used libtmux 0.62.0 and tmux 3.4 on Linux. The probe ran in a disposable linked Git worktree on its own branch and isolated tmux socket with `/dev/null` as the tmux configuration. It did not access application source or credentials through agent tools.

## Verified streamed transport

The probe used Claude's persistent print-mode stream interface, with a FIFO kept open for successive user messages and JSONL output written to a temporary evidence file. A second probe also verified the native interactive terminal path described below. Neither probe implements the future ctlrm mailbox bootstrap.

Initial argument vector:

```text
claude --print --input-format stream-json --output-format stream-json --verbose --tools '' --strict-mcp-config --permission-mode plan --session-id <uuid>
```

Resume argument vector:

```text
claude --print --input-format stream-json --output-format stream-json --verbose --tools '' --strict-mcp-config --permission-mode plan --resume <same-uuid>
```

Here `''` denotes one empty argument, disabling tools for this bounded probe. Production profiles must specify their intended tools and permissions separately. `--fork-session` must be absent: the test requires retaining the native ID rather than creating a fork. Session persistence must remain enabled; `--no-session-persistence` is incompatible with this contract. The probe had no `ANTHROPIC_*`, `CLAUDE_*`, or `CLAUDECODE` environment variables set, including no `CLAUDE_CONFIG_DIR` override. It used the existing authenticated local Claude installation and the default native state context under `~/.claude` (`~/.claude/projects` was present). Resume ran as the same OS user, with the same home directory and workdir. These variables were observed absent before the isolated server was started; they were not silently scrubbed. No extra provider environment names were required for this tested profile; `PATH` must resolve the same Claude executable. Other profiles must record their required variable names and explicit state-directory context. No credential values were captured in the recovery record.

Each input line was a JSON object with `type: user` and a `message` containing `role: user` and text `content`. The first prompt supplied a random conversation token plus the allocated session ID and requested readiness and restart instructions. The agent returned `ready: true`, the supplied session ID, and `resume_argv` referring to that exact ID. A successful provider result event recorded the same native session ID.

After that first turn completed, the probe read the managed pane PID, queried its direct children with `ps -o pid= --ppid <pane-pid>`, asserted exactly one provider child, and sent SIGKILL to that verified child while it awaited more input. It started another managed terminal using `--resume` and requested status reconciliation and the remembered token without including that token in the resume prompt. The response returned `ready: true` and the exact original token; the result event retained the original session ID. Both provider result events reported success. The isolated tmux server was then stopped.

The streamed probe establishes recovery after a completed result; the interactive probe establishes recovery after a persisted readiness response. Neither proves recovery of an unfinished tool call, tools-enabled execution, other permission modes, exactly-once effects, missing state files, kills during transcript writes or streaming turns, changed credentials, or host reboot. Malformed transcript data must block recovery; these probes did not test provider repair behavior. Those cases require the planned uncertain-work and replacement paths.

## Adapter implications

- Allocate or discover the native ID before declaring managed readiness; require the agent's acknowledgment and resume instructions. The controller composes the executed vector from the selected profile and acknowledged native ID. A minimal agent acknowledgment need not repeat profile flags; validation checks the executable/session association, and profile settings remain authoritative.
- Preserve the native state directory/provider context and the transport options alongside executable arguments. An absent session or unavailable provider context is a recovery failure, never evidence that a new conversation successfully resumed.
- Readiness must refer to the current managed generation as well as the native session ID.
- A first-launch workspace trust dialog can block interactive startup. Surface it as waiting for user trust, preserve the pane for attachment, and do not declare readiness or silently approve unfamiliar worktrees.
- Supervise the provider process, not just the pane. The successful probe retained a shell parent; both `new_session(window_command="exec claude ... < fifo > output")` and typing that `exec` form into an explicit shell pane ended with SIGHUP in this environment; the latter used `remain-on-exit=on`. These are observations, not a diagnosed cause or the validated launch path. Diagnose that difference in the terminal/provider adapter implementation rather than assuming both forms work.
- Use libtmux with an isolated/configured server and an explicit shell wrapper for the verified path. In the probe, the shell and provider were distinct processes, so the shell surviving provider death did not count as recovery.
- Production launch arguments carry only a bounded bootstrap/mailbox reference; the inline nonce and instructions in these probes were disposable test data.
- Resume with a status query for the prior work ID. The token test establishes context retention, not whether arbitrary interrupted work already took effect. No bogus-ID negative control was run; the probe requires nonce equality regardless of the CLI exit behavior, so a newly created conversation would not satisfy the test.
- Keep full machine-specific logs under temporary `.scratch/`. A small committed evidence excerpt records matching IDs and the burned, disposable test token without credentials.

## Streamed reproduction checklist

1. Verify provider-related environment names without printing their values and preserve the same native state context for both launches. The observed run had none set; do not silently change authentication settings to reproduce it. Create a disposable linked worktree and isolated libtmux server; open an explicit shell pane and retain the FIFO's writer endpoint.
2. Launch the initial argument vector with a new UUID. Supply an unpredictable token and ask for a recovery acknowledgment.
3. Wait for a successful result carrying that UUID, then terminate only that provider child with SIGKILL.
4. Launch the resume vector using the same provider environment and workdir; request the earlier token without resupplying it.
5. Assert identical native IDs, successful result events, readiness, and exact token equality. Stop only the isolated test server and preserve any wanted evidence before manual cleanup.

## Verified interactive terminal path

The second probe used an explicit `/bin/sh` pane on an isolated libtmux server and launched `claude --tools '' --strict-mcp-config --permission-mode plan --session-id <uuid> <bootstrap-prompt>`. On resume it replaced `--session-id` with `--resume` and supplied only a status-reconciliation prompt. The initial prompt supplied a fresh nonce and the controller-allocated native ID; the agent acknowledged that ID and a resume command for it. The controller retained the profile options separately. The probe waited for a complete assistant text record containing the readiness acknowledgment in that session's transcript, then killed the shell's verified sole provider child. It did not use an inferred terminal prompt as a workflow-completion signal. On resume it read another assistant text record containing readiness, the same ID, and the token.

The resumed interactive agent returned the same native ID and the earlier nonce, which was not present in its new prompt. The probe inspected only the generated session's transcript and captured pane output; it did not inspect unrelated conversations. The renderer reported a fallback to its classic interactive interface, so this establishes interactive terminal operation without making a claim about a particular full-screen renderer. A workspace trust prompt was accepted only for the disposable worktree created by the probe; such prompts must remain explicit user actions for real worktrees.

The successful interactive response is retained in [the evidence excerpt](evidence/provider-recovery-2026-09-05.json). The native-state context and exact working directory were unchanged on resume. The observed Claude transcript layout uses a directory encoding the working directory under `~/.claude/projects`, making the original workdir part of the tested lookup context. Moving or deleting the original worktree is outside this proof and must trigger the planned reconciliation behavior. The terminal adapter should use this interactive shell-parent path initially, while the stream interface remains an independently demonstrated option.

## Interactive reproduction checklist

1. Verify the provider environment and create a disposable worktree and isolated libtmux server as above. Start an explicit shell pane; no FIFO is used for this transport.
2. Launch the interactive vector with a new UUID and a prompt supplying a fresh token, asking for readiness and a resume command. Handle a workspace trust prompt explicitly for this known disposable directory.
3. Read only the transcript file for that generated UUID. Wait for the complete assistant text record acknowledging readiness and that ID. Find the pane PID, assert exactly one provider child in the process table, and SIGKILL that child. This confirms persistence of the response, not a general workflow-completion detector.
4. Start another shell pane in the same workdir and invoke the interactive resume vector with the same profile options. Ask for the earlier token without including it in the new prompt.
5. Read the resumed assistant text from the same native transcript and assert readiness, matching ID, and exact token equality. Shut down only the isolated server.

The [streamed evidence excerpt](evidence/provider-recovery-streamed-2026-09-05.json) retains the independent streamed result. Both evidence files include launch/resume vectors, recorded timestamps, and the intentionally restricted test profile.

The evidence JSON files are versioned experiment records, not templates for the future runtime recovery schema. They retain vectors, responses, IDs, and token assertions. Trust-dialog handling, renderer fallback, and the sole-child process check are recorded in the procedure prose; full terminal/process logs remain temporary.
