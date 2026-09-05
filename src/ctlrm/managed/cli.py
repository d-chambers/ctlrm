"""Typer commands for managed standalone sessions and their supervisor."""

import os
from pathlib import Path
from typing import Annotated

import typer

from ctlrm.managed.area import Area
from ctlrm.managed.providers import ProviderProfile, builtin
from ctlrm.managed.sessions import SessionService
from ctlrm.managed.storage import encoded
from ctlrm.managed import supervisor
from ctlrm.runtime.documents import load_yaml
from ctlrm.runtime.participants import validate_participant_id

session_app = typer.Typer(no_args_is_help=True)
supervisor_app = typer.Typer(no_args_is_help=True)


def _run(action):
    """Use the retained CLI's consistent domain-error exit codes."""
    from ctlrm.__main__ import _run as run

    return run(action)


def _area(ctx: typer.Context) -> Area:
    """Resolve managed state from the root CLI's selected project directory."""
    return Area.load(ctx.obj.project_root)


def _send(ctx: typer.Context, kind: str, payload: dict, request_id: str | None) -> None:
    """Publish an operation and ensure execution continues in the background."""
    area = _area(ctx)
    result = SessionService(area).request(kind, payload, request_id)
    if not os.environ.get("CTLRM_SESSION"):
        supervisor.start(area)
    SessionService(area).await_result(result)
    typer.echo(f"accepted {result}")


@session_app.command("start")
def session_start(
    ctx: typer.Context,
    provider: str = "claude",
    profile: Path | None = None,
    participant: str = "agent",
    role: str = "worker",
    instructions: str = "Work on user prompts.",
    adopt: bool = False,
    request_id: str | None = None,
) -> None:
    """Create a standalone coordination area and launch one background agent."""

    def action() -> None:
        """Validate before creating immutable state or requesting a process."""
        validate_participant_id(participant)
        if participant == "coordinator":
            raise ValueError("coordinator is reserved")
        config = (
            ProviderProfile.model_validate(load_yaml(profile.read_text())).checked()
            if profile
            else builtin(provider)
        )
        roles = {
            participant: {
                "name": participant,
                "role": role,
                "kind": "agent",
                "profile": "agent",
                "instructions": instructions,
            }
        }
        area = Area.create(
            ctx.obj.project_root,
            mode="standalone",
            roles=roles,
            profiles={"agent": config.model_dump()},
            prompt=instructions,
            adopt=adopt,
        )
        request = SessionService(area).request("launch", {"participant": participant}, request_id)
        supervisor.start(area)
        typer.echo(request)

    _run(action)


@session_app.command("status")
def session_status(ctx: typer.Context) -> None:
    """Show durable session state, work IDs, recovery revisions, and diagnostics."""

    def action() -> None:
        """Render machine-readable state without launching anything."""
        area = _area(ctx)
        value = SessionService(area).status()
        value["supervisor_running"] = supervisor.running(area)
        typer.echo(encoded(value), nl=False)

    _run(action)


@session_app.command("ready")
def ready(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Option()],
    generation: Annotated[int, typer.Option()],
    native_id: Annotated[str, typer.Option()],
) -> None:
    """Accept the assigned role and acknowledge executable native recovery instructions."""

    def action() -> None:
        """Confirm the supervisor accepted this generation's recovery acknowledgment."""
        client = SessionService(_area(ctx))
        request = client.ready(session_id, generation, native_id)
        client.await_result(request)
        typer.echo(f"accepted {request}")

    _run(action)


@session_app.command("prompt")
def prompt(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Option()],
    generation: Annotated[int, typer.Option()],
    text: str | None = None,
    file: Path | None = None,
    request_id: str | None = None,
) -> None:
    """Queue a durable prompt; a pending prompt must finish before another is accepted."""

    def action() -> None:
        """Use exactly one text source, preserving multiline content in the mailbox."""
        if (text is None) == (file is None):
            raise ValueError("provide exactly one of --text or --file")
        _send(
            ctx,
            "prompt",
            {
                "session_id": session_id,
                "generation": generation,
                "text": text if text is not None else file.read_text(),
            },
            request_id,
        )

    _run(action)


@session_app.command("acknowledge")
def acknowledge(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Option()],
    generation: Annotated[int, typer.Option()],
    work_id: Annotated[str, typer.Option()],
    request_id: str | None = None,
) -> None:
    """Acknowledge the existing work ID before acting on its contents."""
    _run(
        lambda: _send(
            ctx,
            "acknowledge",
            {"session_id": session_id, "generation": generation, "work_id": work_id},
            request_id,
        )
    )


@session_app.command("disposition")
def disposition(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Option()],
    generation: Annotated[int, typer.Option()],
    work_id: Annotated[str, typer.Option()],
    status: Annotated[str, typer.Option()],
    request_id: str | None = None,
) -> None:
    """Report completed, not_started, in_progress, or uncertain work status."""
    _run(
        lambda: _send(
            ctx,
            "disposition",
            {
                "session_id": session_id,
                "generation": generation,
                "work_id": work_id,
                "status": status,
            },
            request_id,
        )
    )


@session_app.command("stop")
def stop(
    ctx: typer.Context, session_id: Annotated[str, typer.Option()], request_id: str | None = None
) -> None:
    """Explicitly stop a session and disable automatic recovery."""
    _run(lambda: _send(ctx, "stop", {"session_id": session_id}, request_id))


@session_app.command("resume")
def resume(
    ctx: typer.Context, session_id: Annotated[str, typer.Option()], request_id: str | None = None
) -> None:
    """Resume the acknowledged native conversation in a new generation."""
    _run(lambda: _send(ctx, "resume", {"session_id": session_id}, request_id))


@session_app.command("replace")
def replace(
    ctx: typer.Context, session_id: Annotated[str, typer.Option()], request_id: str | None = None
) -> None:
    """Create a fresh native conversation while retaining role and work evidence."""
    _run(lambda: _send(ctx, "replace", {"session_id": session_id}, request_id))


@session_app.command("attach")
def attach(ctx: typer.Context, session_id: Annotated[str, typer.Option()]) -> None:
    """Attach directly; manually typed prompts are outside durable dispatch guarantees."""

    def action() -> None:
        """Resolve and verify the selected terminal before interactive attachment."""
        from ctlrm.communication.terminal import TmuxTerminal

        area = _area(ctx)
        state = area.journal.replay()[0]
        session = state["sessions"].get(session_id)
        if not session or not session.get("identity"):
            raise ValueError("session has no terminal yet")
        TmuxTerminal(area.data["id"]).attach(session["spec"], session["identity"])

    _run(action)


@supervisor_app.command("start")
def supervisor_start(ctx: typer.Context, foreground: bool = False, interval: float = 1) -> None:
    """Start one supervisor; closing this CLI or TUI will not stop background execution."""
    _run(lambda: (supervisor.serve if foreground else supervisor.start)(_area(ctx), interval))


@supervisor_app.command("stop")
def supervisor_stop(ctx: typer.Context, request_id: str | None = None) -> None:
    """Stop supervision while leaving agent terminals intact."""
    _run(lambda: typer.echo(SessionService(_area(ctx)).request("supervisor-stop", {}, request_id)))


@supervisor_app.command("status")
def supervisor_status(ctx: typer.Context) -> None:
    """Inspect the supervisor's persistent lock."""
    _run(lambda: typer.echo("running" if supervisor.running(_area(ctx)) else "stopped"))


@supervisor_app.command("reconcile")
def reconcile(ctx: typer.Context, request_id: str | None = None) -> None:
    """Unpause only after restoring the immutable worktree and branch identity."""
    _run(lambda: _send(ctx, "reconcile-area", {}, request_id))
