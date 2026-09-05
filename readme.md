# ctlrm

Control room coordinates CLI agents and human participants through a shared directory. The CLI manages background Codex and Claude sessions, native recovery, reusable sequential workflows, review loops, and human response steps. Projects organize improvements to a codebase; each job uses its own Git worktree and branch to execute a workflow. Standalone managed sessions are also supported. The TUI is deferred pending design; see the [plan](docs/superpowers/plans/2026-09-05-background-agent-workflows.md).

## Data model

```text
Codebase
└── Project — a goal or related goals, with an optional design document
    └── Job — a deliverable, usually a PR, defined by a workflow
        └── Task — a fixed, generic assignment defined in the workflow
            └── Execution — one attempt, assigned to a participant and optional agent session
```

A **workflow** is a reusable YAML template defining the task graph, task instructions, participant roles, provider/model profiles, and outcome transitions. For example, a job might run “implement the goal,” “review blast radius,” and “review performance.” The job supplies the concrete goal and acceptance criteria; its workflow tasks provide the instructions for carrying it out.

An **execution** records the task's inputs, participant/session assignment, status, outputs, and outcome within a particular job. Review loops and explicit retries create new executions of the same task. Restarting an agent session continues its existing execution. A session can perform multiple executions; human participants need no agent session. A single-agent job can use a one-task workflow.

Projects can contain dependent jobs, each with an immutable workflow snapshot and its own worktree/branch. PR identity belongs to the job: assign a repository and PR number, then search by number with an optional repository filter. Workflow completion, PR merge, project completion, and archival are distinct milestones.

The OS-specific user data directory owns durable project and job records. Each job worktree exposes its records through `.scratch/ctlrm`, a symlink into the central job directory. Existing worktree-local `.ctlrm` runs remain readable. See [project/job commands, storage, and PR lookup](docs/projects.md) and the [backend implementation plan](docs/superpowers/plans/2026-09-05-project-jobs.md). Project archival and bounded specialist-review tasks are the remaining backend steps before GUI design resumes.

## Development

Use Linux, Python 3.11 or newer, tmux 3.2a or newer, and local POSIX storage. `libtmux` 0.62.x is installed with the package. Install and authenticate the provider CLIs separately. Run `uv sync`, then `uv run ctlrm --help`. Run checks with `uv run pytest --cov ctlrm --cov-report term-missing`, `uv run ruff check .`, `uv run ruff format --check .`, and `uvx prek run --all-files`.

## Managed sessions and workflows

```sh
ctlrm --root /path/to/worktree session start --provider codex
ctlrm --root /path/to/worktree session status
ctlrm workflow validate --template examples/implement-review.yaml
ctlrm --root /path/to/another-worktree workflow submit --template examples/implement-review.yaml --title "Implement the task" --file task.md --request-id my-task
ctlrm --root /path/to/another-worktree workflow status
```

Use a fresh worktree for each independent job. The [example template](examples/implement-review.yaml) binds implementer and reviewer roles to separate Codex sessions, then waits for a human owner. Profiles explicitly select provider permissions; Claude automatic input requires an unattended profile. See [session operations and recovery limits](docs/managed-sessions.md) and [workflow acknowledgment, approval, retry, and cancellation](docs/workflows.md).

To retire an area, explicitly stop every managed session and verify its owned provider processes have exited, then stop the supervisor. Preserve wanted logs and artifacts outside the area before explicitly removing runtime files or the worktree with normal filesystem/Git tools. Supervisor stop alone leaves agents running. Project jobs can create dedicated worktrees automatically. Automatic worktree removal, recycling, parallel workflows in one area, cron, remote delegation, and embedded editing are deferred.

## Room example

```sh
ctlrm --root /path/to/worktree init --id coordinator --name Coordinator --assign coordinator=coordinate --assign reviewer=review --prompt "Review the change."
ctlrm --root /path/to/worktree join --id reviewer --name Reviewer
ctlrm --root /path/to/worktree send --from coordinator --to reviewer --title Review --body "Please review."
ctlrm --root /path/to/worktree inbox --participant reviewer
```

Restart commands in legacy room signatures are stored only; use managed sessions for executable recovery. Room identity is cooperative rather than authenticated. All participants need appropriate shared filesystem permissions.
