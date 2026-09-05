"""Project planning, job launch, and PR lookup without a graphical client."""

from pathlib import Path
from typing import Annotated

import typer

from ctlrm.managed import supervisor
from ctlrm.managed.cli import _run
from ctlrm.managed.projects import ProjectStore
from ctlrm.managed.storage import encoded
from ctlrm.runtime.workflows import WorkflowTemplate

project_app = typer.Typer(no_args_is_help=True)
job_app = typer.Typer(no_args_is_help=True)


@project_app.command("create")
def create(
    ctx: typer.Context,
    name: Annotated[str, typer.Option()],
    goal: Annotated[list[str], typer.Option()],
    project_id: str | None = None,
    design: Path | None = None,
) -> None:
    """Create a goal-based project for the selected codebase."""
    _run(
        lambda: typer.echo(
            encoded(
                ProjectStore()
                .create(
                    ctx.obj.project_root,
                    name,
                    goal,
                    project_id=project_id,
                    design=design.read_text() if design else "",
                )
                .model_dump()
            )
        )
    )


@project_app.command("list")
def list_projects() -> None:
    """List project goals and job progress from central storage."""
    _run(lambda: typer.echo(encoded({"projects": ProjectStore().list()})))


@project_app.command("status")
def project_status(project: Annotated[str, typer.Option()]) -> None:
    """Inspect a project without starting a supervisor."""
    _run(lambda: typer.echo(encoded(ProjectStore().status(project))))


@job_app.command("create")
def create_job(
    project: Annotated[str, typer.Option()],
    title: Annotated[str, typer.Option()],
    file: Annotated[Path, typer.Option()],
    template: Annotated[Path, typer.Option()],
    job_id: str | None = None,
    depends_on: list[str] | None = None,
    acceptance: list[str] | None = None,
    base: str = "HEAD",
) -> None:
    """Plan a deliverable and snapshot its workflow without launching agents."""
    _run(
        lambda: typer.echo(
            encoded(
                ProjectStore()
                .add_job(
                    project,
                    title,
                    file.read_text(),
                    WorkflowTemplate.parse(template.read_text()),
                    job_id=job_id,
                    depends_on=depends_on,
                    acceptance=acceptance,
                    base=base,
                )
                .model_dump()
            )
        )
    )


@job_app.command("start")
def start_job(
    project: Annotated[str, typer.Option()],
    job: Annotated[str, typer.Option()],
    worktree: Path | None = None,
    branch: str | None = None,
) -> None:
    """Create a dedicated worktree (or bind the supplied one) and launch the job."""

    def action() -> None:
        """Commit job submission before starting the existing supervisor."""
        client, request = ProjectStore().start_job(project, job, root=worktree, branch=branch)
        supervisor.start(client.area)
        client.await_result(request)
        typer.echo(encoded({"request": request, "root": str(client.area.root)}))

    _run(action)


@job_app.command("status")
def job_status(
    project: Annotated[str, typer.Option()], job: Annotated[str, typer.Option()]
) -> None:
    """Inspect tasks, executions, sessions, and the assigned PR."""
    _run(lambda: typer.echo(encoded(ProjectStore().job_status(project, job))))


@job_app.command("assign-pr")
def assign_pr(
    project: Annotated[str, typer.Option()],
    job: Annotated[str, typer.Option()],
    number: Annotated[int, typer.Option(min=1)],
    repository: Annotated[str, typer.Option()],
    url: str | None = None,
) -> None:
    """Assign or correct a repository-qualified PR number for a job."""
    _run(
        lambda: typer.echo(
            encoded(ProjectStore().assign_pr(project, job, number, repository, url=url))
        )
    )


@job_app.command("find")
def find_job(
    pr: Annotated[int, typer.Option(min=1)],
    repository: str | None = None,
    project: str | None = None,
) -> None:
    """Find every matching job; PR numbers may repeat across repositories."""
    _run(
        lambda: typer.echo(
            encoded({"jobs": ProjectStore().find_pr(pr, repository=repository, project_id=project)})
        )
    )
