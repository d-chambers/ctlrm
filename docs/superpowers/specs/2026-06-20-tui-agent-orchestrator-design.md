# TUI Agent Orchestrator Design

Date: 2026-06-20

## Purpose

`ctlrm` will be a tmux-backed TUI workbench for orchestrating workflow participants across projects and worktrees. Participants can be CLI agents or human users. The first CLI agent providers are Codex, Claude Code, and Pi. The app keeps CLI agents' native terminal behavior visible while adding stable project grouping, participant roles, status, file context, mailbox communication, and workflow controls.

## Scope

The first version includes:

- Top tabs for projects and worktrees.
- Left-side roster of participants with names, roles, types, providers where applicable, and state.
- Center read-only current file viewer.
- Bottom selected-participant panel showing captured pane output and prompt input for CLI agents, or mailbox and response controls for humans.
- Right-side project tree.
- Launch, attach, restart, interrupt, send-prompt, mailbox, and workflow-response controls.
- Persistent registry under user config, reconciled against live tmux state at startup.
- Project-local `.ctlrm/` runtime directory for participant mailboxes, state files, and declarative workflow graphs.
- Workflow scheduling convention where one participant is considered active per project at a time, without forcibly pausing other tmux panes.
- Roster indicators for working, idle, blocked, failed, missing, and unmanaged states.

The first version does not include:

- Direct file editing inside `ctlrm`.
- Deep provider APIs beyond CLI launch, input, and output.
- Replacing tmux as the runtime layer.
- Executable workflow scripts or embedded command logic in workflow graphs. v1 workflow graphs are declarative; a future orchestrating agent can add richer coordination.

## Layout

The main screen is a project workbench:

- Project/worktree tabs sit across the top.
- The left column lists participants for the selected project, including humans and CLI agents with role and status indicators such as spinner/working, idle, blocked, failed, missing, and unmanaged.
- The center column shows the current file in a read-only viewer.
- The bottom center panel shows the selected participant. For CLI agents it shows tmux output and prompt input; for humans it shows mailbox messages and response controls.
- The right column shows the project file tree.
- A compact status/help bar shows current branch, active tmux session, recent activity, and key hints.

This layout optimizes for monitoring several participants while still allowing deep interaction with the selected participant. The active workflow participant is visually distinct from CLI agents that are merely open in tmux.

## Architecture

The app has seven main units.

### Registry

The registry loads and saves projects, worktrees, participants, roles, participant types, provider types for CLI agents, launch commands, working directories, and tmux identifiers. By default it is stored at:

```text
~/.config/ctlrm/registry.toml
```

tmux is still the runtime source of truth. The registry stores the user's intent: labels, roles, grouping, participant type, launch preferences for CLI agents, and known tmux targets.

### Tmux Adapter

The tmux adapter wraps command execution for:

- Creating sessions, windows, and panes.
- Attaching to existing panes.
- Sending input.
- Capturing pane output.
- Interrupting processes.
- Checking pane liveness.
- Listing unmanaged panes that can be imported.

The rest of the application should not construct tmux command lines directly.

### Participant Model

Workflow participants are normalized into a shared internal model. CLI agents and human users both appear in the left roster, have roles, have mailboxes, have state files, and can be workflow graph nodes.

```text
Participant {
    id,
    name,
    role,
    kind,
    provider,
    project_id,
    workdir,
    tmux_target,
    state
}
```

`kind` is `agent` or `human`. `provider` and `tmux_target` are required for active CLI agents and absent for humans. Provider-specific behavior starts intentionally small: default launch command, display metadata, and any provider-specific startup arguments needed to run inside the chosen workdir. Human participants receive workflow messages through their mailbox and interact through the TUI rather than a managed tmux pane.

### Project Runtime

The project runtime manages `.ctlrm/` directories, including participant mailboxes, participant state files, workflow definitions, and workflow run state. It validates message front matter, performs atomic message writes and moves, and exposes mailbox counts and workflow status to the workspace model.

### Workflow Scheduler

The workflow scheduler reads declarative graph definitions and mailbox state to decide the next active participant for a project. The scheduler follows a convention of one active participant per project at a time, but it does not enforce this by suspending tmux panes. It routes messages, updates run state, and records which node is active.

### Workspace Model

The workspace model owns UI state:

- Selected project.
- Selected participant.
- Selected file.
- Expanded project tree nodes.
- Scroll positions.
- Focused region.
- Pending prompt text.
- Last known status messages.
- Mailbox counts and pending workflow handoffs.
- Active workflow run and active graph node.

It should be testable without tmux or terminal rendering.

### TUI

The TUI renders the workbench and handles keyboard input. It reads from the workspace model and invokes registry/tmux operations through service boundaries. It refreshes on a polling tick so pane output, mailbox changes, state file changes, workflow state, and tmux liveness changes appear without requiring user input.

## Data Flow

### Startup

1. Load the registry from `~/.config/ctlrm/registry.toml`.
2. Ensure each selected project has a `.ctlrm/` directory or report that project initialization is needed.
3. Load project runtime state: participant state files, mailbox counts, workflow definitions, and active workflow runs.
4. Query tmux for sessions, windows, and panes.
5. Reconcile each registered participant:
   - `running` if it is a CLI agent and its pane exists; human participants use their state file.
   - `missing` if it is a CLI agent and its saved pane no longer exists.
   - `unmanaged` for extra tmux panes that can be imported as CLI agents.
6. Reconcile workflow active-participant state with live participant states.
7. Build project/worktree tabs from the registry.
8. Scan the selected project root for the file tree.
9. Render the selected project.

### Launching A CLI Agent

1. The user chooses provider, role/name, and project/worktree.
2. `ctlrm` creates or reuses the project's tmux session and agent window.
3. It launches the provider command in a pane with the correct workdir.
4. It records the tmux target and metadata in the registry.
5. The CLI agent appears in the roster and selected-participant panel.

### Attaching To An Existing CLI Agent

1. The user selects an unmanaged tmux pane or enters a tmux target.
2. `ctlrm` captures basic pane metadata.
3. The user assigns provider, name, role, and project.
4. The registry stores the mapping.
5. The pane becomes a managed CLI agent participant.

### Interacting With A Participant

- Typed prompts are sent to the selected CLI agent's tmux pane. For a selected human participant, the panel shows their mailbox and lets the current user write or resolve messages.
- Interrupt sends Ctrl-C to the selected CLI agent pane and is disabled for human participants.
- Restart relaunches the provider command in a new or existing pane for CLI agents and is disabled for human participants.
- File actions can send the current file path or file contents to the selected participant mailbox or selected CLI agent pane.
- File viewing is read-only. Opening an external editor is a command, not an embedded editor feature.

### Routing Workflow Messages

1. The active participant receives work through its mailbox. CLI agents can also receive the message through their tmux pane when `ctlrm` sends or surfaces the mailbox item.
2. The participant updates their `state.md` to `working`, `blocked`, `done`, or `failed`.
3. When the participant has output for another participant, it writes a Markdown mailbox message with front matter.
4. `ctlrm` detects the new message, validates the front matter, and associates it with the current workflow run if `workflow_id` and `run_id` are present.
5. The workflow scheduler evaluates the declarative graph edge matching the message kind or status.
6. `ctlrm` routes the message to the next participant mailbox and marks that participant as the active workflow participant by updating state files and run state.
7. The TUI shows mailbox counts, active-participant status, and a spinner next to participants whose state is `working`.

The single-active-participant rule is advisory. `ctlrm` chooses one active participant for the next unit of workflow work, but it does not suspend or kill other participant processes.

## Registry Shape

The initial registry can be represented as TOML:

```toml
[[projects]]
id = "ctlrm"
name = "ctlrm"
root = "/home/derrick/Gits/ctlrm"
tmux_session = "ctlrm"

[[projects.participants]]
id = "codex-impl"
name = "Codex"
role = "implementer"
kind = "agent"
provider = "codex"
tmux_window = "agents"
tmux_pane = "%12"
workdir = "/home/derrick/Gits/ctlrm"

[[projects.participants]]
id = "claude-review"
name = "Claude"
role = "reviewer"
kind = "agent"
provider = "claude"
tmux_window = "agents"
tmux_pane = "%13"
workdir = "/home/derrick/Gits/ctlrm"

[[projects.participants]]
id = "derrick"
name = "Derrick"
role = "product-owner"
kind = "human"
workdir = "/home/derrick/Gits/ctlrm"
```

The registry file should be written atomically so failed saves do not corrupt existing state.

## Project Runtime Directory

Each project or worktree managed by `ctlrm` has a git-ignored `.ctlrm/` directory. This directory is the coordination substrate that CLI agents and human participants can inspect and update directly. It is separate from the user-level registry: the registry tracks projects, participants, and tmux targets; `.ctlrm/` tracks project-local workflow state.

Initial structure:

```text
.ctlrm/
  participants/
    <participant-id>/
      state.md
      inbox/
      outbox/
  workflows/
    <workflow-id>.yaml
  runs/
    <run-id>/
      state.md
```

`.ctlrm/` should be added to the project `.gitignore` when `ctlrm` initializes a project.

### Mailbox Messages

Mailbox messages are human-readable Markdown files with YAML front matter. The front matter carries routing and workflow metadata; the Markdown body carries the task, result, question, or handoff text.

Minimum front matter fields:

```yaml
id: msg-20260620-001
from: codex-impl
to: claude-review
workflow_id: implement_review_fix
run_id: run-20260620-001
node_id: review
kind: review_request
title: Review tmux adapter changes
status: new
created_at: 2026-06-20T16:30:00+02:00
files:
  - src/tmux.rs
```

The message body is plain Markdown. Participants can write messages by creating new files in another participant's `inbox/` or in their own `outbox/` for `ctlrm` to route. Humans can do this through the TUI; CLI agents may do it directly through files. `ctlrm` should use atomic writes when it creates or moves mailbox messages.

### Participant State Files

Each participant has `.ctlrm/participants/<participant-id>/state.md`, also Markdown with YAML front matter. `ctlrm` reads this file to show participant state without inferring completion from terminal prompts.

Minimum state fields:

```yaml
participant_id: codex-impl
kind: agent
state: working
active: true
updated_at: 2026-06-20T16:30:00+02:00
current_run_id: run-20260620-001
current_node_id: implement
summary: Running tests for tmux adapter
```

Valid initial states are `idle`, `working`, `blocked`, `done`, `failed`, `missing`, and `unmanaged`. The `active` flag is a workflow convention, not a hard lock. `ctlrm` may send work to one active participant per project, but it does not forcibly pause other tmux panes.

### Workflow Graphs

Workflow graphs are declarative YAML files in `.ctlrm/workflows/`. They define participant interaction as nodes and edges. v1 graphs do not run embedded shell commands or scripts.

Example:

```yaml
id: implement_review_fix
title: Implement, review, and fix
nodes:
  implement:
    participant: codex-impl
  review:
    participant: claude-review
  fix:
    participant: codex-impl
edges:
  - from: implement
    to: review
    when: result
  - from: review
    to: fix
    when: changes_requested
  - from: review
    to: done
    when: approved
```

The graph controls routing of mailbox messages and the next active participant convention. A future orchestrating agent or human coordinator can be introduced as a normal graph node or a higher-level coordinator, but v1 should keep graph behavior declarative.

Human participants can be added to a workflow graph the same way as CLI agents. A human can assign themselves to a node, receive mailbox messages in the TUI, write output messages, and advance the workflow by marking their state or message status. This lets a person act as reviewer, product owner, approver, dispatcher, or temporary coordinator without leaving the same roster and mailbox model.

## Error Handling

- If tmux is missing, startup shows a blocking message with installation or configuration guidance.
- If the registry is missing, `ctlrm` starts with an empty registry and offers project creation or import.
- If the registry is malformed, `ctlrm` refuses to overwrite it, reports the parse error, and suggests a backup path.
- If a registered CLI-agent pane no longer exists, the participant is marked `missing` with actions to relaunch, reattach, or remove it from the registry.
- If sending input fails, the UI keeps the prompt text and marks the selected participant with the error.
- If project tree scanning hits permission errors, those paths are shown as unreadable instead of crashing the app.
- Provider launch failures are captured in the selected-participant panel and reflected in CLI-agent participant state.
- If `.ctlrm/` is missing for a project, `ctlrm` offers to initialize it and add `.ctlrm/` to `.gitignore`.
- If a mailbox message has malformed front matter, `ctlrm` leaves it in place, marks it invalid in the TUI, and does not route it.
- If a workflow graph is malformed or references unknown participants, `ctlrm` disables that workflow and reports the validation errors.
- If multiple participants claim `active: true`, `ctlrm` treats this as a state conflict, shows it in the roster, and lets the user choose which participant remains active.

## Testing Strategy

- Unit tests for registry load/save behavior, malformed config handling, and future migrations.
- Unit tests for tmux target parsing and tmux command construction.
- Adapter tests using a fake tmux command runner.
- Workspace model tests for project, participant, file, focus, prompt, mailbox, and active-workflow state transitions.
- Project runtime tests for `.ctlrm/` initialization, `.gitignore` updates, mailbox parsing, state-file parsing, and atomic message writes.
- Workflow scheduler tests for declarative graph validation, edge routing, unknown-participant errors, and active-participant conflict handling.
- Snapshot or layout tests for important TUI regions where practical.
- Opt-in integration test against real tmux. CI should not require tmux unless explicitly configured for that test.

## Implementation Planning Baseline

The implementation plan will assume Python. The TUI will use Textual so the app can provide a rich terminal interface while keeping rendering testable. Registry persistence will use TOML. Project runtime files will use Markdown with YAML front matter for messages and state, and YAML for workflow graph definitions. The implementation plan still needs to define exact dependencies, keyboard bindings, provider launch defaults for Codex, Claude Code, and Pi, and the initial project skeleton.
