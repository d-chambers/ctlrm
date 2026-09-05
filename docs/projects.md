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

## Completion and archival

```sh
ctlrm project complete --project auth
ctlrm project archive --project auth --output /path/to/auth.zip
ctlrm project verify-archive --file /path/to/auth.zip
```

Complete a project after every job workflow has completed. Completion retires each job: it stops owned agent processes, shuts down its supervisor, and prevents new managed work. It then retains the final accepted code version. If the worktree changed after the final outcome, completion refuses it; restore that version before retrying. A partially completed operation can be retried. A completed project cannot add/start jobs or change PR assignments.

Archival completes the project if needed, writes and verifies a ZIP, then records its checksum in project history. The default path is `archives/PROJECT.zip` under the central data directory. An existing unrelated ZIP is never overwritten. Retrying after interrupted publication recovers the matching verified ZIP. If a previously recorded ZIP has been deleted, `project archive` rebuilds it from retained records at the original path or a new `--output` path, recording the superseded archive identity. An existing damaged ZIP must be restored or moved aside explicitly before rebuilding. Archived project/job metadata remains centrally searchable, including PR numbers. Worktrees and source records remain in place; removing them is a separate explicit action after checking the archive.

The ZIP contains project goals/design, immutable job/workflow snapshots, managed session recovery records, mailbox messages, review records, artifact manifests, runtime logs, and retained code. Each job's `retained/code.bundle` contains the Git history reachable from its final HEAD; `retained/index.patch` preserves staged changes. `retained/manifest.json` records the final file inventory, deletion state, modes, link text hashes, and content-addressed `retained/blobs/` data for tracked and non-ignored untracked files. To recover a checkout, fetch `HEAD` from the bundle into a new repository, apply the index patch, and restore the manifest's working-file content and modes from its blobs. Verify the archive before using its files; verification does not extract or execute anything.

Provider credentials and external native conversation stores are excluded. Gitlinks identify nested repositories but do not bundle their separate object databases. Symlinks are stored as link text without following their targets. Archival applies three independent limits: 512 MiB of uncompressed content, 100,000 files, and an 8 MiB manifest. Long file names can reach the manifest limit before the file-count limit. These are checked when creating the ZIP after project completion; a size failure leaves the project completed and retired, with retained source records intact. Retained code stores both Git history and working-file blobs, which count toward the content limit. Individual code artifacts retain their existing 64 MiB/10,000-file limits. A limit or checksum failure leaves source records and worktrees intact.

The [native acceptance record](evidence/project-archive-2026-09-05.json) exercises a single-agent job, a dependent two-task job with a fresh native reviewer conversation, owned-process retirement, and verified retention of both uncommitted outputs in the project ZIP.
