"""Capability-protected HTTP routes and separately authenticated terminal sockets."""

import asyncio
import hmac
import json
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from ctlrm.managed import supervisor
from ctlrm.runtime.projects import PullRequest
from ctlrm.runtime.workflows import WorkflowTemplate
from ctlrm.web.service import Workbench

TEMPLATES = Path(__file__).parent / "templates"


class Input(BaseModel):
    """Reject extra browser parameters rather than passing arbitrary operations through."""

    model_config = ConfigDict(extra="forbid")


class ProjectInput(Input):
    """Create an explicitly named, retryable project from a local codebase."""

    project_id: str = Field(min_length=1, max_length=64)
    root: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=256)
    goals: list[str] = Field(min_length=1, max_length=64)
    design: str = Field(default="", max_length=262144)


class JobInput(Input):
    """Snapshot a reusable YAML workflow and the concrete job goal."""

    job_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=256)
    instructions: str = Field(min_length=1, max_length=65536)
    workflow_yaml: str = Field(min_length=1, max_length=1048576)
    depends_on: list[str] = Field(default_factory=list, max_length=128)
    acceptance: list[str] = Field(default_factory=list, max_length=64)
    base: str = Field(default="HEAD", min_length=1, max_length=256)


class SessionInput(Input):
    """Target one observed session generation with an idempotent explicit operation."""

    action: Literal["interact", "stop", "resume", "replace"]
    generation: int = Field(strict=True, ge=1)
    request_id: str = Field(min_length=1, max_length=128)
    text: str | None = Field(default=None, max_length=16384)


class ResponseInput(Input):
    """Carry the exact human assignment and input version shown in the browser."""

    request_id: str = Field(min_length=1, max_length=100)
    run_id: str
    execution_id: str
    participant: str
    input_artifact: str | None = None
    outcome: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=16384)
    specialists: list[str] = Field(default_factory=list, max_length=8)
    specialist_reason: str | None = Field(default=None, max_length=4096)
    pr: PullRequest | None = None


class JobAction(Input):
    """Apply an explicit cancellation or retry to a current job execution."""

    action: Literal["cancel", "retry"]
    request_id: str = Field(min_length=1, max_length=128)
    execution_id: str | None = None
    reason: str | None = Field(default=None, max_length=4096)


async def authorize(request: Request) -> None:
    """Require the capability on every data read and write."""
    expected = f"Bearer {request.app.state.token}"
    if not hmac.compare_digest(
        request.headers.get("authorization", "").encode(), expected.encode()
    ):
        raise HTTPException(401, "open the local URL printed by ctlrm serve")


async def get_workbench(request: Request) -> Workbench:
    """Resolve services from this app instance, never from shared module state."""
    return request.app.state.workbench


WorkbenchDependency = Annotated[Workbench, Depends(get_workbench)]
router = APIRouter(prefix="/api", dependencies=[Depends(authorize)])
terminal_router = APIRouter(prefix="/api")


@router.get("/snapshot")
def snapshot(workbench: WorkbenchDependency) -> dict:
    """Read real projects, assignments, and sessions without starting processes."""
    return workbench.snapshot()


@router.get("/templates")
def templates() -> list[dict]:
    """Offer the packaged reusable workflows as editable YAML before job creation."""
    return [{"name": p.stem, "yaml": p.read_text()} for p in sorted(TEMPLATES.glob("*.yaml"))]


@router.post("/projects")
def project_create(workbench: WorkbenchDependency, body: ProjectInput) -> dict:
    """Create an initiative without implicitly starting agents."""
    values = body.model_dump()
    root = Path(values.pop("root")).expanduser()
    return workbench.store.create(root, **values).model_dump()


@router.post("/projects/{project}/jobs")
def job_create(workbench: WorkbenchDependency, project: str, body: JobInput) -> dict:
    """Validate and snapshot the selected YAML workflow for a planned job."""
    values = body.model_dump()
    template = WorkflowTemplate.parse(values.pop("workflow_yaml"))
    return workbench.store.add_job(project, workflow=template, **values).model_dump()


@router.post("/projects/{project}/jobs/{job}/start")
def start(workbench: WorkbenchDependency, project: str, job: str) -> dict:
    """Provision the job worktree and launch its supervisor only on explicit start."""
    client, request = workbench.store.start_job(project, job)
    supervisor.start(client.area)
    client.await_result(request)
    return {"request_id": request, "status": "accepted"}


@router.put("/projects/{project}/jobs/{job}/pr")
def assign_pr(workbench: WorkbenchDependency, project: str, job: str, body: PullRequest) -> dict:
    """Assign searchable PR metadata without publishing to a hosting service."""
    return workbench.store.assign_pr(project, job, **body.model_dump())


@router.post("/projects/{project}/{action}")
def project_action(
    workbench: WorkbenchDependency, project: str, action: Literal["complete", "archive"]
) -> dict:
    """Retire completed jobs and retain or archive the accepted project code."""
    return getattr(workbench.store, action)(project)


@router.get("/projects/{project}/activity")
def activity(workbench: WorkbenchDependency, project: str) -> list[dict]:
    """Read the latest accepted events for this project and its jobs."""
    return workbench.activity(project)


@router.get("/projects/{project}/jobs/{job}/artifacts/{reference}")
def artifact(workbench: WorkbenchDependency, project: str, job: str, reference: str) -> dict:
    """Read and verify an immutable execution artifact manifest."""
    return workbench.artifact(project, job, reference)


@router.get("/projects/{project}/jobs/{job}/executions/{execution}/artifacts")
def execution_artifacts(
    workbench: WorkbenchDependency, project: str, job: str, execution: str
) -> dict:
    """List commits and messages attributed to an exact task execution."""
    return workbench.execution_artifacts(project, job, execution)


@router.get(
    "/projects/{project}/jobs/{job}/executions/{execution}/artifacts/{kind}/{item}",
)
def execution_text(
    workbench: WorkbenchDependency, project: str, job: str, execution: str, kind: str, item: str
) -> dict:
    """Read the retained text of a commit, mailbox message, or outcome report."""
    return workbench.execution_text(project, job, execution, kind, item)


@router.get("/projects/{project}/jobs/{job}/files")
def files(
    workbench: WorkbenchDependency, project: str, job: str, artifact: str | None = None
) -> dict:
    """List code from the worktree or a selected artifact version."""
    return workbench.files(project, job, artifact)


@router.get("/projects/{project}/jobs/{job}/file")
def file(
    workbench: WorkbenchDependency, project: str, job: str, name: str, artifact: str | None = None
) -> dict:
    """Read bounded code with path and selected-version checks."""
    return workbench.file(project, job, name, artifact)


@router.post("/projects/{project}/jobs/{job}/sessions/{session_id}")
def session_action(
    workbench: WorkbenchDependency, project: str, job: str, session_id: str, body: SessionInput
) -> dict:
    """Send an explicit generation-bound session operation to the single writer."""
    client = workbench.client(project, job, validate_worktree=body.action != "stop")
    payload = {"session_id": session_id, "generation": body.generation}
    if body.action == "interact":
        payload["text"] = body.text
    request = client.request(body.action, payload, body.request_id)
    supervisor.start(client.area)
    client.await_result(request)
    return {"request_id": request, "status": "accepted"}


@router.post("/projects/{project}/jobs/{job}/respond")
def respond(workbench: WorkbenchDependency, project: str, job: str, body: ResponseInput) -> dict:
    """Join the assigned human role and submit an exact-version acknowledgment/report."""
    client = workbench.client(project, job)
    role = client.area.data["roles"].get(body.participant)
    if not role or role["kind"] != "human":
        raise ValueError("browser responses require an assigned human participant")
    client.join(body.participant)
    supervisor.start(client.area)
    payload = body.model_dump(exclude={"request_id"})
    ack = {k: payload[k] for k in ("run_id", "execution_id", "participant", "input_artifact")}
    request = client.request("workflow-ack", ack, body.request_id + "-ack")
    client.await_result(request)
    request = client.request("workflow-report", payload, body.request_id + "-report")
    client.await_result(request)
    return {"request_id": request, "status": "accepted"}


@router.post("/projects/{project}/jobs/{job}/action")
def job_action(workbench: WorkbenchDependency, project: str, job: str, body: JobAction) -> dict:
    """Cancel or retry through the existing workflow ownership checks."""
    client = workbench.client(project, job, validate_worktree=body.action != "cancel")
    payload = (
        {}
        if body.action == "cancel"
        else {"execution_id": body.execution_id, "reason": body.reason}
    )
    request = client.request("workflow-" + body.action, payload, body.request_id)
    supervisor.start(client.area)
    client.await_result(request)
    return {"request_id": request, "status": "accepted"}


@terminal_router.websocket("/terminal/{project}/{job}/{session_id}/{generation}")
async def terminal(
    socket: WebSocket, project: str, job: str, session_id: str, generation: int
) -> None:
    """Attach a view to an existing owned terminal after origin and capability checks."""
    if (
        socket.headers.get("host") not in socket.app.state.hosts
        or socket.headers.get("origin") not in socket.app.state.origins
    ):
        await socket.close(code=1008)
        return
    await socket.accept()
    try:
        frame = await asyncio.wait_for(socket.receive_text(), 5)
        if len(frame) > 512:
            raise ValueError("invalid terminal authentication")
        supplied = json.loads(frame)
        if not isinstance(supplied, dict) or not isinstance(supplied.get("token"), str):
            raise ValueError("invalid terminal authentication")
        if not hmac.compare_digest(supplied["token"].encode(), socket.app.state.token.encode()):
            raise ValueError("invalid terminal capability")
        from ctlrm.web.terminal import bridge

        await bridge(socket, socket.app.state.workbench, project, job, session_id, generation)
    except (ValueError, OSError, RuntimeError, asyncio.TimeoutError) as error:
        await socket.close(code=1008, reason=str(error).encode()[:120].decode(errors="ignore"))
    except WebSocketDisconnect:
        pass
