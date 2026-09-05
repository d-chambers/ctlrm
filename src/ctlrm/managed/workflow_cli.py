"""Workflow template, task, acknowledgment, and human response commands."""

from pathlib import Path
from typing import Annotated

import typer

from ctlrm.managed import supervisor
from ctlrm.managed.cli import _area, _run, _send
from ctlrm.managed.storage import encoded
from ctlrm.managed.workflows import WorkflowService
from ctlrm.runtime.workflows import WorkflowTemplate

workflow_app = typer.Typer(no_args_is_help=True)


@workflow_app.command("validate")
def validate(template: Annotated[Path, typer.Option()]) -> None:
    """Validate a reusable template without creating or launching an area."""
    _run(lambda: typer.echo(encoded(WorkflowTemplate.parse(template.read_text()).model_dump())))


@workflow_app.command("submit")
def submit(
    ctx: typer.Context,
    template: Annotated[Path, typer.Option()],
    title: Annotated[str, typer.Option()],
    file: Annotated[Path, typer.Option()],
    bind: list[str] | None = None,
    request_id: str | None = None,
) -> None:
    """Snapshot a task and start its workflow in the selected worktree."""

    def action() -> None:
        """Validate all input before starting the shared background supervisor."""
        bindings = None
        if bind:
            bindings = {}
            for item in bind:
                role, separator, participant = item.partition("=")
                if not separator or role in bindings:
                    raise ValueError("bindings must be unique ROLE=PARTICIPANT values")
                bindings[role] = participant
        client, request = WorkflowService.submit(
            ctx.obj.project_root,
            WorkflowTemplate.parse(template.read_text()),
            title,
            file.read_text(),
            bindings=bindings,
            request_id=request_id,
        )
        supervisor.start(client.area)
        client.await_result(request)
        typer.echo(f"accepted {request}")

    _run(action)


@workflow_app.command("status")
def status(ctx: typer.Context) -> None:
    """Inspect run progress and session health without triggering launches."""
    _run(lambda: typer.echo(encoded(WorkflowService(_area(ctx)).status()), nl=False))


@workflow_app.command("join")
def join(ctx: typer.Context, participant: Annotated[str, typer.Option()]) -> None:
    """Accept an assigned human role before acknowledging or responding."""
    _run(lambda: WorkflowService(_area(ctx)).join(participant))


@workflow_app.command("acknowledge")
def acknowledge(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Option()],
    execution_id: Annotated[str, typer.Option()],
    participant: Annotated[str, typer.Option()],
    session_id: str | None = None,
    generation: int | None = None,
    input_artifact: str | None = None,
    request_id: str | None = None,
) -> None:
    """Acknowledge an assignment before acting; required for agents and humans."""
    _run(
        lambda: _send(
            ctx,
            "workflow-ack",
            {
                "run_id": run_id,
                "execution_id": execution_id,
                "participant": participant,
                "session_id": session_id,
                "generation": generation,
                "input_artifact": input_artifact,
            },
            request_id,
        )
    )


@workflow_app.command("report")
def report(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Option()],
    execution_id: Annotated[str, typer.Option()],
    participant: Annotated[str, typer.Option()],
    outcome: Annotated[str, typer.Option()],
    summary: Annotated[str, typer.Option()],
    session_id: str | None = None,
    generation: int | None = None,
    input_artifact: str | None = None,
    request_id: str | None = None,
    specialist: list[str] | None = None,
    specialist_reason: str | None = None,
    pr_number: int | None = None,
    pr_repository: str | None = None,
    pr_url: str | None = None,
) -> None:
    """Report an allowed outcome; human responses follow the same validation path."""
    _run(
        lambda: _send(
            ctx,
            "workflow-report",
            {
                "run_id": run_id,
                "execution_id": execution_id,
                "participant": participant,
                "session_id": session_id,
                "generation": generation,
                "input_artifact": input_artifact,
                "outcome": outcome,
                "summary": summary,
                "specialists": specialist,
                "specialist_reason": specialist_reason,
                "pr": {"number": pr_number, "repository": pr_repository, "url": pr_url}
                if any(value is not None for value in (pr_number, pr_repository, pr_url))
                else None,
            },
            request_id,
        )
    )


@workflow_app.command("cancel")
def cancel(ctx: typer.Context, request_id: str | None = None) -> None:
    """Cancel the task and stop its owned agent sessions."""
    _run(lambda: _send(ctx, "workflow-cancel", {}, request_id))


@workflow_app.command("retry")
def retry(
    ctx: typer.Context,
    execution_id: Annotated[str, typer.Option()],
    reason: Annotated[str, typer.Option()],
    request_id: str | None = None,
) -> None:
    """Abandon a visit and refresh its inputs after explicitly stopping its agent."""
    _run(
        lambda: _send(
            ctx, "workflow-retry", {"execution_id": execution_id, "reason": reason}, request_id
        )
    )
