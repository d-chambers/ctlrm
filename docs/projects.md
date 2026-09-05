# Projects and jobs

A project is a goal or related goals for a codebase, with an optional design document. A job is a deliverable, usually one PR, with an immutable workflow snapshot and a dedicated worktree/branch. Workflow tasks are fixed generic assignments; each visit produces a task execution. Agent recovery continues that execution. Human tasks require no agent session.

Create a project and plan jobs without launching agents:

```sh
ctlrm --root /path/to/codebase project create --project-id auth --name 'Improve authentication' --goal 'Rotate tokens safely' --design design.md
ctlrm job create --project auth --job-id core --title 'Token rotation' --file goal.md --template examples/implement-review.yaml --acceptance 'Existing tokens remain valid during rollout'
ctlrm job create --project auth --job-id clients --title 'Migrate callers' --file clients.md --template examples/implement-review.yaml --depends-on core
ctlrm project status --project auth
ctlrm job start --project auth --job core
```

`job start` creates a worktree under the central data directory, or binds an explicit `--worktree PATH` from the same codebase. It refuses existing unrelated coordination state. Repeating the same start resumes initialization rather than creating another job. `--branch` selects a branch name for a new worktree. A planned job's `--base` selects its starting Git revision; it is resolved and frozen when launch is first attempted. Dependencies require an existing job and block start until its workflow completes. Completion does not imply that a PR was merged; use an appropriate base revision for stacked jobs or merge prerequisites before starting dependent jobs.

The central data directory is `$XDG_DATA_HOME/ctlrm` (otherwise `~/.local/share/ctlrm`) on Linux, `~/Library/Application Support/ctlrm` on macOS, and `%LOCALAPPDATA%/ctlrm` on Windows. `CTLRM_DATA_HOME` overrides this with an absolute path. These are storage conventions; managed process supervision still requires the existing Linux/tmux environment.

Project records live under `projects/PROJECT/`; each `jobs/JOB/` owns its job definition and runtime. The worktree contains a small `.ctlrm/location.json` locator and `.scratch/ctlrm` symlink to its central job directory. Standalone sessions and direct workflow submissions store their runtime locally under `.ctlrm`; project jobs use central storage. Status inspection reads central history even after a worktree disappears. New managed native profiles receive `--add-dir` for their own job directory, preserving the configured tool permission policy and excluding other project/job directories. The link and locator must remain intact while the job runs.

Assign a PR after creating a job, then search by number:

```sh
ctlrm job assign-pr --project auth --job core --repository owner/repo --number 42 --url https://github.com/owner/repo/pull/42
ctlrm job find --pr 42
ctlrm job find --pr 42 --repository owner/repo
```

Repository identities accept `owner/repo` or `host/owner/repo`. Bare-number searches return every match, avoiding ambiguity across repositories. Reassigning a PR preserves the previous assignment in project event history. Assignment records identity; it does not query a hosting service or assert that the PR is merged. Project and job status commands never launch processes.
