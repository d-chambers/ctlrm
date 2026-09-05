# ctlrm

Control room coordinates CLI agents and human participants through a shared directory. The current CLI supports immutable room prompts, assigned roles, participant registration, and mailbox messages. Background supervision and reusable workflow execution are being implemented according to the [plan](docs/superpowers/plans/2026-09-05-background-agent-workflows.md).

## Development

Use Python 3.11 or newer. Run `uv sync`, then `uv run ctlrm --help`. Run checks with `uv run pytest --cov ctlrm --cov-report term-missing`, `uv run ruff check .`, `uv run ruff format --check .`, and `uvx prek run --all-files`.

## Room example

```sh
ctlrm --root /path/to/worktree init --id coordinator --name Coordinator --assign coordinator=coordinate --assign reviewer=review --prompt "Review the change."
ctlrm --root /path/to/worktree join --id reviewer --name Reviewer
ctlrm --root /path/to/worktree send --from coordinator --to reviewer --title Review --body "Please review."
ctlrm --root /path/to/worktree inbox --participant reviewer
```

Restart commands in standalone room signatures are stored only; they do not yet start a supervisor. Room identity is cooperative rather than authenticated. All participants need appropriate shared filesystem permissions.
