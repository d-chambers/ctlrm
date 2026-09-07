"""Typer command-line interface for filesystem-backed ctlrm rooms."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, TypeVar
from uuid import uuid4

import typer
import yaml
from pydantic import ValidationError

from ctlrm.runtime.manifest import RoleAssignment, RoomManifest
from ctlrm.runtime.messages import MailboxMessage
from ctlrm.runtime.room import RoomRuntime

from ctlrm.managed.cli import session_app, supervisor_app

from ctlrm.managed.workflow_cli import workflow_app
from ctlrm.managed.project_cli import project_app, job_app

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
app.add_typer(session_app, name="session")
app.add_typer(workflow_app, name="workflow")
app.add_typer(project_app, name="project")
app.add_typer(job_app, name="job")
app.add_typer(supervisor_app, name="supervisor")
_Result = TypeVar("_Result")


def _now() -> str:
    """Return the current UTC time in an interoperable representation."""
    return datetime.now(timezone.utc).isoformat()


def _read_text(value: str | None, path: Path | None) -> str:
    """Resolve text supplied inline, from a file, or through standard input."""
    if value is not None:
        return value
    if path is not None:
        return path.read_text(encoding="utf-8")
    if stdin_buffer := getattr(sys.stdin, "buffer", None):
        return stdin_buffer.read().decode("utf-8")
    return sys.stdin.read()


def _run(action: Callable[[], _Result]) -> _Result:
    """Convert expected domain failures into stable command exit codes."""
    try:
        return action()
    except typer.Exit:
        raise
    except FileExistsError as exc:
        typer.echo(f"conflict: {exc.filename or exc}", err=True)
        raise typer.Exit(3) from exc
    except FileNotFoundError as exc:
        typer.echo(f"not found: {exc.filename or exc}", err=True)
        raise typer.Exit(4) from exc
    except (ValidationError, ValueError, yaml.YAMLError) as exc:
        typer.echo(f"invalid: {exc}", err=True)
        raise typer.Exit(2) from exc
    except (OSError, RuntimeError) as exc:
        typer.echo(f"room error: {exc}", err=True)
        raise typer.Exit(5) from exc


def _parse_assignments(values: list[str]) -> list[RoleAssignment]:
    """Parse repeated PARTICIPANT=ROLE assignment options."""
    assignments = []
    for value in values:
        participant, separator, role = value.partition("=")
        if not separator:
            raise ValueError(f"assignment must be PARTICIPANT=ROLE: {value!r}")
        assignments.append(RoleAssignment(participant=participant, role=role))
    return assignments


def _runtime(ctx: typer.Context) -> RoomRuntime:
    """Return the room runtime stored by the root callback."""
    assert isinstance(ctx.obj, RoomRuntime)
    return ctx.obj


@app.callback()
def root(
    ctx: typer.Context,
    root_path: Annotated[
        Path | None,
        typer.Option("--root", help="Project root containing the .ctlrm room."),
    ] = None,
) -> None:
    """Coordinate agents through a project-local shared directory."""
    ctx.obj = RoomRuntime((root_path or Path.cwd()).resolve())


@app.command()
def serve(
    port: Annotated[int, typer.Option(min=1024, max=65535)] = 8766,
    data: Annotated[
        Path | None, typer.Option(help="Override the central user data directory.")
    ] = None,
) -> None:
    """Open a local browser workbench for managed projects and agent terminals."""
    import uvicorn
    from ctlrm.managed.projects import ProjectStore
    from ctlrm.web.app import create_app

    web = create_app(ProjectStore(data), port=port)
    typer.echo(f"Control room: http://127.0.0.1:{port}/#token={web.state.token}")
    uvicorn.run(
        web, host="127.0.0.1", port=port, access_log=False, ws_max_size=65536, ws_max_queue=8
    )


@app.command()
def init(
    ctx: typer.Context,
    participant_id: Annotated[str, typer.Option("--id", help="Room author's participant ID.")],
    name: Annotated[str, typer.Option(help="Display name for the room author.")],
    assign: Annotated[
        list[str],
        typer.Option(help="Role assignment as PARTICIPANT=ROLE; repeat for the full roster."),
    ],
    prompt: Annotated[
        str | None, typer.Option(help="Room prompt; defaults to standard input.")
    ] = None,
    prompt_file: Annotated[
        Path | None,
        typer.Option("--prompt-file", help="Read the room prompt from a UTF-8 file."),
    ] = None,
    kind: Annotated[str, typer.Option(help="Participant kind.")] = "agent",
    provider: Annotated[str | None, typer.Option(help="Agent provider, if applicable.")] = None,
    restart_command: Annotated[
        str | None,
        typer.Option("--restart-command", help="Command to restart this session; stored only."),
    ] = None,
    capability: Annotated[list[str], typer.Option(help="Author capability; may be repeated.")] = [],
) -> None:
    """Create the prompt and role roster, then register their author."""
    if prompt is not None and prompt_file is not None:
        raise typer.BadParameter("use either --prompt or --prompt-file, not both")

    def initialize() -> None:
        room = RoomManifest(
            id=f"room-{uuid4().hex}",
            author=participant_id,
            created_at=_now(),
            assignments=_parse_assignments(assign),
            prompt=_read_text(prompt, prompt_file),
        )
        result = _runtime(ctx).initialize(
            room,
            name=name,
            kind=kind,
            provider=provider,
            restart_command=restart_command,
            capabilities=capability,
            joined_at=_now(),
        )
        typer.echo(result.room_path)
        typer.echo(result.signature_path)
        if not result.created:
            typer.echo("room already initialized; author registration recovered", err=True)

    _run(initialize)


@app.command()
def join(
    ctx: typer.Context,
    participant_id: Annotated[str, typer.Option("--id", help="Assigned participant ID.")],
    name: Annotated[str, typer.Option(help="Participant display name.")],
    kind: Annotated[str, typer.Option(help="Participant kind.")] = "agent",
    provider: Annotated[str | None, typer.Option(help="Agent provider, if applicable.")] = None,
    restart_command: Annotated[
        str | None,
        typer.Option("--restart-command", help="Command to restart this session; stored only."),
    ] = None,
    capability: Annotated[
        list[str], typer.Option(help="Participant capability; may be repeated.")
    ] = [],
) -> None:
    """Accept the role assigned in the room roster and write a signature."""

    def register() -> None:
        path = _runtime(ctx).join_participant(
            participant_id=participant_id,
            name=name,
            kind=kind,
            provider=provider,
            capabilities=capability,
            restart_command=restart_command,
            joined_at=_now(),
        )
        typer.echo(path)

    _run(register)


@app.command()
def send(
    ctx: typer.Context,
    sender: Annotated[str, typer.Option("--from", help="Registered sender ID.")],
    recipient: Annotated[str, typer.Option("--to", help="Registered recipient ID.")],
    title: Annotated[str, typer.Option(help="Short message title.")],
    kind: Annotated[str, typer.Option(help="Message kind.")] = "message",
    body: Annotated[
        str | None, typer.Option(help="Message body; defaults to standard input.")
    ] = None,
    body_file: Annotated[
        Path | None,
        typer.Option("--body-file", help="Read the body from a UTF-8 file."),
    ] = None,
    file: Annotated[list[str], typer.Option(help="Related file path; may be repeated.")] = [],
    message_id: Annotated[str | None, typer.Option("--id", help="Explicit message ID.")] = None,
    execution_id: Annotated[str | None, typer.Option(help="Owning workflow execution ID.")] = None,
) -> None:
    """Deliver an immutable message between registered participants."""
    if body is not None and body_file is not None:
        raise typer.BadParameter("use either --body or --body-file, not both")

    def deliver() -> None:
        identifier = (
            message_id or f"msg-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}"
        )
        message = MailboxMessage(
            id=identifier,
            from_=sender,
            to=recipient,
            kind=kind,
            title=title,
            status="new",
            created_at=_now(),
            files=file,
            body=_read_text(body, body_file),
        )
        runtime = _runtime(ctx)
        if (runtime.root / "area.yaml").exists():
            from ctlrm.managed.area import Area
            from ctlrm.managed.workflows import WorkflowService

            area = Area.load(runtime.project_root)
            if area.data["mode"] == "workflow":
                if not execution_id:
                    raise ValueError("workflow messages require --execution-id from the assignment")
                typer.echo(WorkflowService(area).send_message(message, execution_id))
                return
        if execution_id:
            raise ValueError("execution IDs require a managed workflow")
        typer.echo(runtime.send_message(message))

    _run(deliver)


@app.command()
def inbox(
    ctx: typer.Context,
    participant: Annotated[str, typer.Option(help="Participant whose inbox should be listed.")],
) -> None:
    """List messages in one participant inbox."""

    def show() -> None:
        """List valid items and diagnose malformed neighbors."""
        errors: list[tuple[Path, Exception]] = []
        for message in _runtime(ctx).read_inbox(
            participant, on_error=lambda path, exc: errors.append((path, exc))
        ):
            typer.echo(f"{message.id}\t{message.from_}\t{message.kind}\t{message.title}")
        _report_invalid_files(errors)

    _run(show)


@app.command()
def participants(ctx: typer.Context) -> None:
    """List participant signatures and their assigned roles."""

    def show() -> None:
        """List valid identities and diagnose malformed neighbors."""
        errors: list[tuple[Path, Exception]] = []
        for signature in _runtime(ctx).list_signatures(
            on_error=lambda path, exc: errors.append((path, exc))
        ):
            typer.echo(f"{signature.id}\t{signature.role}\t{signature.kind}\t{signature.name}")
        _report_invalid_files(errors)

    _run(show)


def _report_invalid_files(errors: list[tuple[Path, Exception]]) -> None:
    """Report all invalid files after rendering the valid entries."""
    for path, error in errors:
        typer.echo(f"invalid: {path}: {error}", err=True)
    if errors:
        raise typer.Exit(2)


def main(argv: Sequence[str] | None = None) -> None:
    """Run the ctlrm command-line application."""
    app(args=list(argv) if argv is not None else None, prog_name="ctlrm")


if __name__ == "__main__":
    main()
