# Web workbench

`ctlrm serve` serves the real project store at a loopback URL. Copy the complete URL, including its initial capability fragment, into a browser on the same computer. Each server start creates a new capability. API reads and writes require it; WebSocket attachment additionally checks the browser origin. The server rejects remote hosts and cross-origin requests. All scripts, styles, and terminal libraries are bundled locally. This is a local application for one OS user, not a remote multi-user service.

## Projects and jobs

The Projects sidebar filters active/completed or archived records. Search matches project goals, codebase paths, job titles, and repository-qualified PR metadata. Click a project for its summary, or double-click/Open project for a reusable workspace tab. Completed jobs have a checkmark in both the summary and design lists. The project workspace groups participants by job; its job selector changes the workflow and code context.

New project records a local committed Git codebase, goals, and optional design text. Add job snapshots the selected YAML workflow, job instructions, acceptance criteria, dependencies, and base revision. The packaged ship, implement/review, and single-agent workflows are editable before creating the job. Provider profiles, model arguments, and permissions remain explicit in the YAML. Start job provisions the recorded worktree and starts the existing background supervisor. Reads and terminal attachment never launch an agent. Assign PR records a number, repository, and optional link; it does not publish a PR.

Overview displays the actual task graph, including outcome cycles and optional specialist routes. Clicking a node opens its instructions, outcome policy, execution history, and input/output manifests. The engine runs tasks sequentially through the declared outcome transitions. An active task indicator describes committed assignment/message activity; process state and recovery state are shown separately. It does not infer provider CPU activity or guarantee that a provider is currently generating tokens.

## Human responses and sessions

Needs you lists active assignments to human participants across projects. A response explicitly accepts the assigned human role, acknowledges the execution, and reports its outcome with the input artifact shown in the form. Stale executions and changed review inputs are rejected by the existing supervisor. Optional specialist requests and PR metadata are available when the workflow declares them. Human approval remains separate from provider tool permissions.

Open terminal attaches a disposable PTY client to the current owned tmux generation. Input and output are streamed to the existing agent. The Session button shows current process/recovery details, recorded native restart instructions, recent conversation messages, and explicit Stop, Resume, or Replace operations. Resume and Replace require the live process to be stopped first. Browser operations carry the observed generation, so an old view cannot stop a replacement session.

Managed native Codex uses its existing exec/resume runner, so the terminal displays its real output and the message composer queues conversation input between turns. Messages do not assign or complete workflow tasks. A claimed message whose runner is interrupted is marked uncertain and is never automatically replayed. Inspect its result before explicitly resending. Replacing the native conversation cancels its queued messages. Interactive providers use direct terminal input instead of the composer.

The sidebar and terminal dividers support dragging, arrow keys, Home/End, and remembered sizes. Full screen fills the browser viewport; Restore or Escape returns the same terminal and draft to its panel. Switching project tabs keeps an open terminal attached to its labeled job. Closing the view detaches only its client. Reconnect reattaches the same generation and restores the tmux pane contents; a changed generation requires reopening from Participants.

## Documents, artifacts, and archival

Design shows the project document, planned jobs, goals, and acceptance criteria. Artifacts shows project context and immutable execution reports, including older visits labeled as prior executions. Manifests record the source version, input identity, HEAD, and per-file hashes; they do not by themselves store every historical file's bytes. Browsing a manifest returns the matching retained blob or unchanged worktree file and refuses to present newer content as that version. Code displays bounded UTF-8 text from the current worktree or retained archive inventory. The viewer refuses symlinks, path traversal, binary files, and files over 1 MiB. Editing remains in the agent terminal or an external editor.

Activity lists the latest accepted project/job events. Archive requires all jobs to be completed, retires their owned processes, retains the accepted code, and creates the existing verified ZIP. Central metadata, PR search, reports, and retained code remain readable. Archive does not delete the worktrees. Archive download, remote access, authentication between multiple OS users, automatic provider permission approval, and a document/code editor are outside this initial web workbench.

## Verification

Run `uv run pytest --cov ctlrm --cov-report term-missing` and `uvx prek run --all-files`. The web tests use disposable real Git repositories and the actual supervisor transition engine; the terminal test streams keyboard/output traffic over a WebSocket through a real tmux client and verifies that detaching leaves the agent running. `npm --prefix frontend ci && npm --prefix frontend run build && npm --prefix frontend run check` reproduces the locally bundled terminal assets and checks authored frontend syntax/formatting. `uv build` includes the browser assets, licenses, and canonical YAML templates in the wheel.
