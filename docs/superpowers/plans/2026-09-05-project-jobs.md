# Project, job, and task backend

The agreed hierarchy is codebase → project (goals and optional design) → job (usually a PR, one worktree/branch, a workflow snapshot) → fixed workflow task → execution (one assignment to a participant and optional managed session). Review loops create new executions; native session recovery retains the execution. GUI work is excluded until a separate design discussion.

The central OS-specific user data directory owns durable project/job records. A job worktree exposes these through `.scratch/ctlrm`. Existing worktree-local `.ctlrm` areas remain compatible. Archival preserves records and actual retained changes, verifies the ZIP, and does not silently delete worktrees or credentials.

## Implementation sequence

1. Align task definition, job input, and execution models; retain historical imports and journal keys; deliver task-specific instructions.
2. Introduce projects, planned jobs, dependency validation, central storage resolution, worktree provisioning, and project/job CLI operations. Snapshot inputs before starting jobs; retain one supervisor per job.
3. Add bounded optional specialist tasks and a ship workflow with check/review/fix loops, CI and review-thread handling, and merge-ready completion. Provide a minimal single-agent template.
4. Implement project completion and verified ZIP archival with retained code changes; document and exercise the complete backend.

Each step runs applicable tests/lint and self-review, receives a fresh internal review and a Claude CLI review, addresses actionable findings, and merges locally into main. Reviews remain under ignored `.scratch/`. No remote or PR is configured; no GUI implementation is included.
