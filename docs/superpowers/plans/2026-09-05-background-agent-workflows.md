# Background Agent Sessions and Reusable Workflows

Date: 2026-09-05

Status: Implementation in progress. Milestone 0 has measured native recovery evidence in `docs/provider-session-recovery.md`. Remaining schemas and operations describe intended behavior, not features already available in the CLI.

## Product contract

Control room manages agents in background terminals, receives instructions for resuming their native sessions, and passes a task through a reusable workflow. It also supports a single agent session without requiring a multi-agent workflow.

**One Git worktree and its branch are the coordination area for one workflow instance.** A repository can have multiple workflow instances in separate worktrees. All participants in an instance share that worktree and its `.ctlrm/` directory. Reusing a workflow means applying the saved template in another coordination area; it does not mean launching unrelated workflows into the same worktree.

Use [libtmux](https://github.com/tmux-python/libtmux) for all tmux management. Put its server/session/window/pane operations behind a small application adapter. Replace the existing hand-built tmux command runner; any necessary low-level tmux operation goes through libtmux inside that adapter. Select a supported release range and verify compatibility during implementation.

A standalone session occupies its own worktree coordination area, with the same launch, readiness, recovery, and inspection behavior as workflow agents. It needs no YAML graph or synthetic task. A user may also choose a one-agent workflow when explicit task completion is useful. Multiple agent sessions can remain open within a workflow, but only its active step receives work. This is cooperative scheduling: it cannot prevent manual edits or an inactive agent from changing shared files.

The first release supports one host, main and linked Git worktrees, local POSIX storage meeting the existing atomic publication requirements, sequential steps, conditional review loops, and human steps. Worktree creation/removal, parallel branches, concurrent unrelated workflows in one worktree, cron scheduling, remote agents, and embedded editing are deferred. Users select an existing worktree. Closing the UI must not stop agents or workflow execution.

This plan supersedes the TUI-first implementation order in [the earlier plan](2026-06-20-tui-agent-orchestrator.md). Retain the earlier [design](../specs/2026-06-20-tui-agent-orchestrator-design.md) as background; the explicit contracts here govern this milestone.

## Starting point

- `runtime/room.py` supports immutable room manifests, author-assigned roles, participant signatures, and direct mailbox delivery.
- `__main__.py` exposes room initialization, joining, sending, and listing. `--restart-command` is optional and stored only.
- `agents/launcher.py` and `gui/app.py` reference moved modules. Launch persistence currently replaces the registry with a one-project registry.
- `runtime/workflows.py` parses participant-bound graphs. `scheduler.py` only looks up the next participant; there is no persisted execution loop.
- There is no background supervisor, task submission, readiness handshake, session recovery executor, or automatic handoff.
- The evaluation baseline has 18 passing tests and 45% coverage, passing Ruff lint, and three files failing formatting. Twelve older test modules are deleted in the working tree but remain in Git history. Recheck at implementation start and preserve unrelated in-progress edits.

## Domain model

| Object | Meaning and ownership |
| --- | --- |
| Coordination area | One worktree/branch, its runtime identity, and one workflow instance or standalone session |
| Task | User-defined unit of work, instructions, inputs, and final outputs |
| Workflow template | Reusable, versioned steps, required roles, transitions, and execution limits |
| Run | The execution of the task using a snapshot of its template and role bindings in the selected worktree |
| Step execution | One visit to a step, with a unique execution ID, inputs, outcome, and artifact references |
| Participant | Stable identity and accepted role in the area's immutable roster; an agent or human |
| Agent session | Provider execution instance, participant association, terminal identity, native session ID, generation, and recovery information |
| Message/artifact | Immutable communication or identifiable output, attributed to its producing execution |

The same task moves through implementation, review, and fix. Each loop visit or explicitly authorized retry creates a new step-execution ID. Reuse a healthy agent session across visits to the same role within the run. A completed execution remains completed. An independent task/workflow starts in another coordination area; archiving and recycling an area is a later lifecycle feature.

A participant ID and its role acceptance remain stable across native resume and explicit session replacement. Session generation changes; the immutable signature does not. Add a managed registration operation that accepts identical stable signature fields idempotently and rejects conflicting identity or role claims. Use a separate readiness acknowledgment for each generation. Do not put changing native session IDs or recovery instructions into the immutable signature.

Keep step status separate from session liveness. A dead process means an unfinished execution needs recovery, not that the task completed. Human workflow approval is distinct from permission for an individual agent tool call.

## Worktree isolation and runtime layout

Resolve a selected directory to its Git worktree root, including subdirectory invocation and linked worktrees whose `.git` is a file. Place `.ctlrm/` at that root, never in the shared Git common directory. Persist a coordination ID, worktree root, and expected branch ref; use the coordination ID for terminal namespaces rather than the branch display name. Detect branch changes or moved/missing roots and pause for explicit reconciliation before further dispatch or relaunch. Require a branch for a new managed area; detached worktrees receive an actionable error in v1.

Different worktrees of the same repository must have separate supervisors, locks, sessions, and messages. Keep `.ctlrm/` out of Git using an existing ignore rule or an idempotent entry in the Git-resolved local `info/exclude` file, preserving its content. Linked worktrees may share that exclude file; only the ignore pattern is shared, never coordination state. Do not modify tracked `.gitignore` as a side effect of starting a task. Preserve existing room data and access through the legacy CLI. Initializing managed state over a populated legacy area requires an explicit adoption/migration operation or a fresh worktree; never silently overwrite its prompt or roster.

Proposed storage for one coordination area:

```text
.ctlrm/
  area.yaml                       # Immutable identity, expected branch, mode, run ID if any
  definition.yaml                 # Immutable workflow/profile/role snapshot; workflow mode only
  task.md                         # User task snapshot; workflow mode only
  room.md                         # Immutable prompt and assigned roster
  participants/                   # Existing signature/inbox format plus work acknowledgments
  sessions/<session-id>/recovery/  # Immutable agent-authored recovery revisions
  artifacts/
  events/                         # Single supervisor writer; committed state transitions
  supervisor/
    lock
    requests/                     # Immutable CLI/UI requests, published by multiple clients
    logs/
```

Use two durable authority surfaces: participant/client submissions and supervisor-committed events. The request spool is necessary because multiple clients can submit while only the supervisor may commit transitions. Request IDs and resulting dispositions are recorded in the event log; derive status and request results by replay, without separate mutable `state.json` or result files in v1. Session state is derived from events plus validated recovery revisions. Bound file reads and tolerate incomplete/corrupt records with visible diagnostics; do not discard accepted history.

Existing `runtime/state.py` and participant `state.md` remain legacy compatibility inputs only. Managed areas take progress from accepted events and readiness/liveness reports; legacy files cannot advance a managed run. Avoid two authoritative status systems.

Generate compact UUID-based IDs that reuse `validate_path_component` and the existing participant/message length limits. Allocate each outbound assignment ID and complete payload once when committing its transition event; replay reuses that recorded ID, timestamp, and payload. Independent messages receive independent IDs, even between the same executions. Compare validated models with the same body-whitespace and optional-field normalization applied to both sides; do not compare raw YAML serialization. The same ID with different normalized content is a conflict. User retry commands must retain the request/message ID; omitting an ID creates new work. Document this intentional extension of the current duplicate-message behavior.

## Supervisor and terminal boundary

Use `CLI/TUI -> application services -> runtime storage`, with provider adapters and the libtmux adapter behind small explicit interfaces. The supervisor uses the same services. Fake those interfaces in unit tests; integration tests use an isolated tmux server/socket. Resolve terminals by socket path, server process identity (PID and OS process start time), dedicated session identity, pane ID, and ctlrm ownership tags. Pane IDs alone are insufficient after a tmux server restart. Persist and revalidate the server process identity; ambiguous identity blocks adoption or relaunch. Never adopt or terminate unrelated panes.

Run one background supervisor per coordination area, holding `fcntl.flock` on its lock file for its lifetime. A second supervisor must refuse to start; process death releases the lock. Never unlink or replace the lock file during ordinary stop/start, since two different inodes would break exclusion. Poll submissions and managed processes at a configurable bounded interval (one second by default), using an injectable clock in tests. Provide explicit start/status/stop operations, a foreground mode for debugging/service managers, and persistent logs. Start it independently of the CLI/TUI lifetime. Login/boot service installation is deferred; restarting the host requires starting the supervisor again.

Persist launch intent before spawning, including a unique deterministic tmux session name derived from the recorded ctlrm session ID and generation. Create that dedicated session with its initial pane through libtmux; the name is part of creation, while later ownership tags are supplementary. If reconciliation finds the intended session after a crash, it must verify the process/workdir/bootstrap identity and adopt it or block for inspection, never blindly launch another copy because tags are missing. Add coordination, participant, session, and generation tags after creation. Do not reuse a name already occupied before launch; test crashes between creation, tagging, bootstrap, and readiness. In workflow mode, launch an agent when its role first becomes active and reuse it on later visits; do not launch all roles eagerly. Before starting a run, validate every required profile and its demonstrated recovery capabilities so a missing later provider does not become a mid-run surprise.

A standalone session supports launch, prompt, attach, inspect, stop, resume, and replacement without a workflow definition. Persist prompt IDs and acknowledgments for CLI/TUI delivery. Store all standalone prompts and workflow assignments in the mailbox, including short prompts; type only a bounded ASCII message-ID reference (at most 256 bytes) into the pane. Oversized references are validation errors. This keeps multiline/large user content out of terminal keystroke delivery. Direct terminal interaction is also available, but individual effects of manually entered prompts are outside the durable dispatch guarantee.

## Recovery contract and feasibility gate

Before building the scheduler, perform a manual spike demonstrating actual native session recovery for the first provider. Use temporary bootstrap instructions and a manually written recovery record; the managed registration command does not exist yet. Save the resulting capability matrix in `docs/provider-session-recovery.md` as an implementation deliverable. Record provider/version, how an agent obtains its native session ID, exact launch/resume argument vectors, required environment names, and evidence that the resumed conversation retains earlier context. Verify a second provider through the same adapter contract before calling multi-provider operation supported. Do not assume that a restart command can recover arbitrary sessions.

1. Launch the provider inside the selected worktree with bootstrap instructions for its assigned identity and room, identity/recovery registration, work acknowledgment, outcome reporting, and status reconciliation operations. Deliver a step or initial user prompt only after readiness succeeds.
2. The agent registers its identity and publishes a structured recovery revision: schema version, ctlrm/native session IDs, generation, workdir, executable/arguments, required environment names, and timestamp. Also record a non-secret provider context identifier, such as its state-directory path or available account identifier, and verify it before resume. The adapter may help discover the native ID; the agent must supply/acknowledge its recovery instructions. A human-readable explanation may supplement the executable record.
3. Compose the executed launch/resume vector from the authoritative provider profile and acknowledged native session ID; an agent may supply a minimal executable/ID acknowledgment without repeating profile flags. Validate its executable against that profile and verify the workdir and session association. Keep secret values out of the record. Resolve required environment values from the configured runtime environment; missing names block resume with a specific diagnostic instead of triggering repeated failed launches.
4. Require a valid recovery record before declaring the agent ready. Registration timeouts or unsupported native resume are visible blocked conditions. Preserve `--restart-command` for legacy rooms, but it alone is not the managed readiness contract.
5. Monitor the actual provider process and generation. A live pane containing a shell is not a healthy agent. Distinguish exit, registration timeout, and uncertain responsiveness; inactivity alone is not grounds to kill a session.
6. On confirmed unexpected death, use bounded retries/backoff to resume the specific native session. Require a new generation acknowledgment. Send a status-reconciliation request referencing the existing work ID, not an instruction to repeat the work.
7. Accept one of: a validated completion for that execution; a report that it has not started; a report that work is in progress with an explicit continuation acknowledgment; or an uncertain disposition. Dispatch not-started work with the same ID, resume acknowledged in-progress work, and block uncertain cases for human reconciliation. A standalone session follows the same rule for its last acknowledged prompt. Do not infer the outcome from terminal text or a dirty worktree.
8. If native resume/history recovery fails, pause visibly. Explicit replacement creates a fresh native session with the same participant role, a new session generation, task/step context, accepted artifacts, and known outcomes. Record that replacement; do not label it a native resume. Stale old-generation completions cannot advance the run; retain and surface them as reconciliation evidence. A human can explicitly resolve an uncertain execution using that evidence through the normal accepted-event path.

Stopping the supervisor leaves managed terminals intact. Explicitly stopping a standalone session disables automatic recovery until resume. Canceling a run prevents further dispatch and requests interruption; keep it canceling until the process stops or the operator resolves the uncertainty. For completion/cancellation races, the first committed event determines the accepted disposition; record later arrivals without advancing twice.

## Reusable workflow definition

Templates reference roles and provider profiles, not pre-existing participant IDs. Resolve them into an immutable run roster and snapshot the template, role instructions, and profile configuration. Later edits affect newly created areas only. Control room preassigns participant IDs for each role, authors the room, and joins as coordinator; agents claim their assigned IDs and accept the recorded roles through idempotent managed registration. Human participants explicitly accept their assigned role through CLI/UI.

Illustrative schema:

```yaml
schema_version: 1
name: implement-review
entry: implement
limits:
  max_step_executions: 12
roles:
  implementer:
    kind: agent
    profile: coding-agent
    instructions: Implement the task and report checks and changed files.
  reviewer:
    kind: agent
    profile: review-agent
    instructions: Review the referenced implementation and report its outcome.
  owner:
    kind: human
    instructions: Accept or reject the reviewed result.
steps:
  implement:
    role: implementer
    transitions:
      completed: review
  review:
    role: reviewer
    transitions:
      approved: approve
      changes_requested: implement
  approve:
    role: owner
    transitions:
      approved: terminal:completed
      rejected: terminal:rejected
```

Profiles contain executable arguments, registration timeouts, recovery limits, and provider-specific settings. Graphs contain no executable expressions. Validate schema version, duplicate YAML keys, string keys/outcomes, entry/destination steps, roles/profiles, reserved terminal identifiers, unreachable steps, and a reachable terminal. Reject implicit YAML boolean keys instead of accepting coerced names. Intentional cycles are bounded by the execution limit. Unknown outcomes block with an error; they do not mean completion. Execution failure/recovery exhaustion pauses for retry, replacement, or cancellation. Business rejection follows an explicit transition.

## Durable handoff

An active agent emits a structured completion/handoff with area/run ID, step-execution ID, participant/session generation, outcome, summary, and artifact references. Control room validates ownership, activity, expected worktree root and branch, commits the transition, then publishes the next assignment and wakes the next participant. Recheck the expected branch before accepting an outcome as well as before dispatch or relaunch; a mismatch pauses execution. Agents report outcomes; the workflow selects recipients.

Each assignment contains the area/run/step-execution IDs, assigned participant and generation, role instructions from the snapshot, task snapshot reference, triggering outcome and summary, input artifact references and versions, allowed outcomes, and acknowledgment/report operations. A review-to-implementation loop explicitly includes the reviewer's requested changes and the implementation version under review. The committed event includes the next execution ID, newly allocated outbound message ID, and full assignment payload. Restart replay reconstructs pending deliveries. Mailbox storage is durable; terminal input is a wake-up hint. The recipient acknowledges the work ID before acting. A repeated wake-up must not create a new assignment. Human responses use the same event and outcome-validation path.

Every code handoff includes a commit SHA or per-file content hashes covering the delivered changes, including relevant untracked files. Record HEAD and dirty status at handoff. The reviewer acknowledges the artifact version it received and its outcome references that version; verify relevant hashes again before accepting approval. If content changed during review, block or create a new review execution rather than accepting stale approval. This detects relevant drift without pretending to prevent concurrent filesystem writes.

No filesystem transaction atomically completes an external agent operation. Guarantee one accepted transition and one logical assignment per execution, with retryable delivery. Do not promise exactly-once agent side effects. Ambiguous acknowledged work follows the recovery status protocol and may require human reconciliation.

## Implementation milestones

### 0. Prove provider recovery

- [x] In a disposable Git worktree, launch one real provider, obtain agent-acknowledged recovery information for a controller-allocated native ID, terminate it, and resume the exact native conversation.
- [x] Record the capability matrix and demonstrate retained context plus a readiness/status acknowledgment after resume.
- [x] Use this result to select the interactive shell-parent transport and native recovery contract. The mailbox bootstrap remains a milestone 2 deliverable. A failed feasibility check blocks claims of native recovery; replacement remains a separate operation.

Acceptance: evidence that the core recovery requirement is achievable before building the execution machinery around it.

### 1. Restore the supported baseline

- [x] Repair moved imports and the registry save that discards other entries; establish worktree-based registry identity and safe concurrent updates.
- [x] Restore/adapt retained boundary tests from Git history. Preserve unrelated working-tree edits. Keep CLI, room, registry, models, and workspace coverage; replace old tmux argv assertions and participant-only scheduler tests with the new adapter/transition contract tests. The deleted `dev.py` hot-reload feature and its tests are outside this milestone; remove remaining obsolete UI hooks rather than reviving them incidentally.
- [x] Fix prompt normalization on init recovery, malformed-YAML error handling, and inbox sender/recipient/filename validation while keeping valid messages visible.
- [x] Resolve formatting failures, consolidate role placeholders, use `AGENTS.md`, and ignore temporary review/coverage artifacts.
- [x] Add CI for tests, lint/format, supported Python versions, and package build/install smoke checks.

Acceptance: the retained CLI/package imports and checks pass; persistence preserves other worktrees.

### 2. Manage single-agent sessions in worktrees

- [x] Implement worktree/branch validation, immutable area identity, namespace isolation, ignore handling, and explicit legacy adoption rules.
- [x] Add libtmux with a tested dependency range. Replace `communication/tmux.py` internals with the terminal adapter; test literal/multiline prompts and real pane ownership instead of old argv construction.
- [x] Extend `agents/launcher.py` with the proven provider/bootstrap adapter, launch intent, readiness, and recovery revisions.
- [x] Implement the singleton supervisor, request spool, committed events, replay, logs, and standalone session lifecycle operations.
- [x] Add native recovery, status reconciliation, explicit stop/resume/replacement, and minimal crash/replay tests as these components land.

Measured acceptance is recorded in `docs/managed-sessions.md` and `docs/evidence/managed-native-recovery-2026-09-05.json`. The runtime initially targets Linux process identity APIs. Provider defaults are preserved: automatic terminal input requires an explicitly configured unattended profile; interactive profiles keep prompts in the durable mailbox for manual delivery. Interactive attach uses the native tmux client with inherited I/O after libtmux ownership verification because libtmux 0.62 captures attach output.

Acceptance: a single agent needs no task/graph, survives UI closure, and resumes its native session after death. Explicit stop does not trigger restart. Separate worktrees of one repository operate independently. A second supervisor or a crash between spawn and commit cannot create a duplicate managed session.

### 3. Define reusable workflows and tasks

- [x] Implement task/template/run/step models and validated transitions in `runtime/workflows.py` and `scheduler.py`, replacing participant-only transition results.
- [x] Snapshot a template into a fresh coordination area, bind roles to participants, and initialize the immutable roster.
- [x] Expose validate-template, submit-task, inspect-progress, report-outcome, and human-response operations through Typer and services. Choose final command names consistently with the existing CLI.
- [x] Implement replayable transition events with recorded outbound IDs/payloads and idempotent publication/acknowledgment; add replay tests immediately.

Acceptance: simulated agents traverse a review loop and human approval. A one-agent workflow works. The same template initializes a second independent worktree without sharing mutable runtime state. An area rejects a second unrelated workflow.

### 4. Connect automatic launch, handoff, and recovery

- [ ] Validate all profiles up front, then lazily launch roles through the existing session service and require readiness before dispatch.
- [ ] Connect outcome validation, artifact attribution, committed transitions, mailbox publication, acknowledgments, and next-agent wake-ups.
- [ ] Implement retry/replacement/cancellation decisions, stale-generation rejection, and bounded workflow loops.
- [ ] Prove a second provider's native recovery contract and run a mixed-provider workflow.
- [ ] Inject crashes at launch, event commit, delivery, wake-up, and acknowledgment boundaries; verify reconciliation or an explicit uncertainty block.

Acceptance: task submission drives implement/review/approval without manual launching or routing, and recovery never silently repeats ambiguous work or accepts a handoff twice.

### 5. Present the working runtime

- [ ] Connect the TUI to worktree/branch selection, standalone sessions, task submission, role/session display, pane output, human responses, and recovery actions.
- [ ] Show task progress, active step, process health, and waiting/recovery reasons separately.
- [ ] Update the README with installation, libtmux/tmux requirements, worktree isolation, a reusable example, and recovery limits. Correct currently advertised but deferred cron/editing capabilities. Document manual retirement: explicitly stop every managed session, verify owned provider processes have exited, stop the supervisor, preserve any wanted logs/artifacts outside the area, then explicitly remove its runtime/worktree using normal filesystem/Git tools. Supervisor stop alone does not stop agents. Automatic recycling remains deferred.

Acceptance: CLI and TUI use the same services; reopening the TUI reconstructs current state without launching work again.

## Verification and definition of done

Use fake processes for deterministic lifecycle/failure testing and isolated real tmux servers through libtmux for adapter tests. Native-provider acceptance uses disposable worktrees, bounded prompts, configured credentials, and recorded provider versions/session identities. Fake tests do not establish real provider resume support. Coverage percentage is informational for this milestone; required boundary/recovery tests and passing checks are the gates, not an arbitrary global percentage over legacy code.

- [ ] Run `uv run pytest --cov ctlrm --cov-report term-missing`, `uv run ruff check .`, `uv run ruff format --check .`, and `uvx prek run --all-files`.
- [ ] Test duplicate IDs with identical/conflicting payloads, unknown outcomes, malformed files, template snapshot independence, one-agent workflows, and loop limits.
- [ ] Test missing executables/environment, bootstrap timeout, live-shell/dead-agent detection, unsupported resume, idempotent identity registration, and replacement generations.
- [ ] Test crashes before/after launch, accepted outcomes, publication, wake-up, and acknowledgment; test stale completions and cancellation races.
- [ ] Test worktree subdirectory resolution, main/linked worktrees, `.git` files, expected branch enforcement, removed/moved roots, and independent areas sharing a Git common directory.
- [ ] Test standalone launch without a graph, prompt acknowledgments, direct-terminal limitations, explicit stop, and uncertain prompt recovery.
- [ ] Build/install a wheel outside the source tree, check supported Python versions, and document supported libtmux/tmux/storage versions.
- [ ] Self-review each substantive milestone, obtain the required counterpart CLI review under untracked `.scratch/`, address actionable findings, and rerun relevant checks. Answer every unresolved review comment if an implementation PR exists.

Final demonstrations: first launch a single agent in one worktree, obtain restart instructions, close the UI, recover its native conversation after death, and explicitly stop it. In another worktree, submit a task using the saved implement-review template, let control room launch the required roles, interrupt/resume the implementer, deliver the accepted implementation once, handle requested changes and human approval, and finish with attributable outputs. Restart the supervisor at a handoff boundary. Reuse the unchanged template in a third worktree and verify complete runtime isolation. All tmux management goes through libtmux.

## Follow-on protocol decisions

After local acceptance, choose an adapter against an actual integration target: MCP tools/resources for existing services, ACP where structured coding-agent session control is useful, and A2A for external delegation. Pin protocol revisions and document mappings before claiming interoperability. Reuse the same task/run state and avoid adding competing schedulers. Protocol adapters do not gate the local background-management milestone.
