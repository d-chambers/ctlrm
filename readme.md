# ctlrm

Control room coordinates CLI agents and human participants through a shared directory. The CLI manages background Codex and Claude sessions, native recovery, reusable sequential workflows, review loops, and human response steps. Projects organize improvements to a codebase; each job uses its own Git worktree and branch to execute a workflow. Standalone managed sessions are also supported. The local web workbench provides project and participant tabs, workflow graphs, human responses, artifacts, code browsing, and direct access to managed agent sessions.

## Open the workbench

Run `uv run ctlrm serve` and open the complete loopback URL printed in the terminal. Installed packages use `ctlrm serve`. The browser uses the same central project records as the CLI; `--data PATH` selects an isolated store and `--port PORT` changes the default port 8766. The server binds only to `127.0.0.1`. Its generated access capability is carried in the initial URL fragment, removed from the address bar, and retained for that browser tab.

Create a project, add a job using a packaged workflow or custom YAML, then explicitly start it. Opening an agent terminal attaches to its existing tmux session. Interactive providers accept direct keyboard input; native Codex sessions use a conversation message composer between turns. Closing the terminal or browser leaves the agent running. Drag the sidebar divider or terminal divider, or focus a divider and use arrow keys; Full screen and Escape preserve the same terminal view. See the [web workbench guide](https://github.com/d-chambers/ctlrm/blob/main/docs/web-workbench.md) for controls and boundaries.

## Data model

```text
Codebase
└── Project — a goal or related goals, with an optional design document
    └── Job — a deliverable, usually a PR, defined by a workflow
        └── Task — a fixed, generic assignment defined in the workflow
            └── Execution — one attempt, assigned to a participant and optional agent session
```

A **workflow** is a reusable YAML template defining the task graph, task instructions, participant roles, provider/model profiles, and outcome transitions. For example, a job might run “implement the goal,” “review blast radius,” and “review performance.” The job supplies the concrete goal and acceptance criteria; its workflow tasks provide the instructions for carrying it out.

An **execution** records the task's inputs, participant/session assignment, status, outputs, and outcome within a particular job. Review loops and explicit retries create new executions of the same task. Restarting an agent session continues its existing execution. A session can perform multiple executions; human participants need no agent session. A single-agent job can use a one-task workflow. Each execution records its initial commit, timestamps, and outgoing mailbox messages. Its accepted outcome retains commit messages and diffs alongside the worktree manifest. In task details, double-click a commit or sent message to view its full retained text in a read-only editor window; Enter and Open are also supported.

Projects can contain dependent jobs, each with an immutable workflow snapshot and its own worktree/branch. PR identity belongs to the job: assign a repository and PR number, then search by number with an optional repository filter. Workflow completion, PR merge, project completion, and archival are distinct milestones.

The OS-specific user data directory owns durable project and job records. Each job worktree exposes its records through `.scratch/ctlrm`, a symlink into the central job directory. See [project/job commands, storage, and PR lookup](https://github.com/d-chambers/ctlrm/blob/main/docs/projects.md) and the [backend implementation plan](https://github.com/d-chambers/ctlrm/blob/main/docs/superpowers/plans/2026-09-05-project-jobs.md). Workflows can request bounded specialist-review tasks from a predefined allowlist. The [ship workflow](https://github.com/d-chambers/ctlrm/blob/main/src/ctlrm/web/templates/ship.yaml) uses review lenses, fresh reviewer sessions, PR publication, and CI/review handling; the [single-agent workflow](https://github.com/d-chambers/ctlrm/blob/main/src/ctlrm/web/templates/single-agent.yaml) keeps simple jobs small. Project completion retires owned processes and retains the accepted code; `project archive` creates a verified ZIP while keeping PR metadata searchable. See [completion and archival](https://github.com/d-chambers/ctlrm/blob/main/docs/projects.md#completion-and-archival).

## Development

Use Linux, Python 3.11 or newer, tmux 3.2a or newer, and local POSIX storage. `libtmux` 0.62.x is installed with the package. Install and authenticate the provider CLIs separately. Run `uv sync`, then `uv run ctlrm --help`. Run checks with `uv run pytest --cov ctlrm --cov-report term-missing`, `uv run ruff check .`, `uv run ruff format --check .`, and `uvx prek run --all-files`. Browser assets are packaged locally. After editing frontend source, run `npm --prefix frontend ci`, `npm --prefix frontend run build`, and `npm --prefix frontend run check`; `npm --prefix frontend run format` formats authored assets. Node is needed for frontend development, not to run the installed workbench.

## Releases

CI checks pull requests and `main` on Python 3.11 and 3.14. Publishing a GitHub release runs those checks again and uploads the tested packages to PyPI through Trusted Publishing. See [release setup and versioning](https://github.com/d-chambers/ctlrm/blob/main/docs/releases.md), including the one-time PyPI account configuration.

## Managed sessions and workflows

```sh
ctlrm --root /path/to/worktree session start --provider codex
ctlrm --root /path/to/worktree session status
ctlrm workflow validate --template src/ctlrm/web/templates/implement-review.yaml
ctlrm --root /path/to/another-worktree workflow submit --template src/ctlrm/web/templates/implement-review.yaml --title "Implement the task" --file task.md --request-id my-task
ctlrm --root /path/to/another-worktree workflow status
```

Use a fresh worktree for each independent job. The [example template](https://github.com/d-chambers/ctlrm/blob/main/src/ctlrm/web/templates/implement-review.yaml) binds implementer and reviewer roles to separate Codex sessions, then waits for a human owner. Profiles explicitly select provider permissions; Claude automatic input requires an unattended profile. See [session operations and recovery limits](https://github.com/d-chambers/ctlrm/blob/main/docs/managed-sessions.md) and [workflow acknowledgment, approval, retry, and cancellation](https://github.com/d-chambers/ctlrm/blob/main/docs/workflows.md).

For project jobs, use `project complete` and `project archive`. To retire a standalone area, explicitly stop every managed session and verify its owned provider processes have exited, then stop the supervisor. Preserve wanted logs and artifacts outside the area before explicitly removing runtime files or the worktree with normal filesystem/Git tools. Supervisor stop alone leaves agents running. Project jobs can create dedicated worktrees automatically. Automatic worktree removal, recycling, parallel workflows in one area, cron, remote delegation, and embedded editing are deferred.

## Room example

```sh
ctlrm --root /path/to/worktree init --id coordinator --name Coordinator --assign coordinator=coordinate --assign reviewer=review --prompt "Review the change."
ctlrm --root /path/to/worktree join --id reviewer --name Reviewer
ctlrm --root /path/to/worktree send --from coordinator --to reviewer --title Review --body "Please review."
ctlrm --root /path/to/worktree inbox --participant reviewer
```

Restart commands in room signatures are stored only; use managed sessions for executable recovery. Room identity is cooperative rather than authenticated. All participants need appropriate shared filesystem permissions.

### Conversation input

Managed native Codex sessions accept conversation messages between turns with `ctlrm --root WORKTREE session interact --session-id ID --generation N --text "Explain the current change"`. This resumes the recorded native conversation and does not assign, acknowledge, or complete a workflow task. The supervisor accepts up to eight pending messages, each at most 16 KiB. A runner claims each message before invoking the provider; interrupted turns are retained as `uncertain` and are never automatically replayed. Explicitly replacing a conversation cancels its queued messages. Recent messages remain in the session snapshot and older messages remain in the event journal. Interactive providers continue to accept direct input through their owned tmux terminal.
