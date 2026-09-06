"""Authenticated loopback HTTP and WebSocket routes for the local workbench."""

import asyncio
import hmac
import json
from pathlib import Path
import secrets
import yaml
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from ctlrm.managed import supervisor
from ctlrm.managed.projects import ProjectStore
from ctlrm.runtime.projects import PullRequest
from ctlrm.runtime.workflows import WorkflowTemplate
from ctlrm.web.service import Workbench

STATIC = Path(__file__).parent / "static"
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


def create_app(
    store: ProjectStore | None = None, *, token: str | None = None, port: int = 8766
) -> FastAPI:
    """Create a local capability-protected application.

    Parameters
    ----------
    store : ProjectStore, optional
        Central project store, injectable for isolated tests.
    token : str, optional
        Browser capability; generated randomly when omitted.
    port : int
        Exact loopback port allowed in Host and Origin headers.
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.workbench = Workbench(store or ProjectStore())
    app.state.token = token or secrets.token_urlsafe(32)
    app.state.port = port
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    workbench = app.state.workbench

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        """Reject rebinding, cross-origin requests, and oversized request bodies."""
        if request.headers.get("host") not in hosts:
            return JSONResponse({"error": "invalid local host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in origins:
            return JSONResponse({"error": "cross-origin requests are not allowed"}, status_code=403)
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > 2 * 1024 * 1024:
            return JSONResponse({"error": "request body is too large"}, status_code=413)
        # All writes use bounded JSON; reject unbounded chunked request bodies.
        if request.method in {"POST", "PUT", "PATCH"} and "content-length" not in request.headers:
            return JSONResponse({"error": "content-length is required"}, status_code=411)
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            }
        )
        return response

    async def authorize(request: Request) -> None:
        """Require the capability on every data read and write."""
        expected = f"Bearer {app.state.token}"
        if not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), expected.encode()
        ):
            raise HTTPException(401, "open the local URL printed by ctlrm serve")

    def domain_error(request: Request, error: Exception) -> JSONResponse:
        """Report domain rejections without exposing a server traceback."""
        status = 404 if isinstance(error, FileNotFoundError) else 409
        return JSONResponse({"error": str(error)}, status_code=status)

    for error in (ValueError, OSError, RuntimeError, KeyError, yaml.YAMLError):
        app.add_exception_handler(error, domain_error)

    auth = [Depends(authorize)]

    @app.get("/api/snapshot", dependencies=auth)
    def snapshot() -> dict:
        """Read real projects, assignments, and sessions without starting processes."""
        return workbench.snapshot()

    @app.get("/api/templates", dependencies=auth)
    def templates() -> list[dict]:
        """Offer the packaged reusable workflows as editable YAML before job creation."""
        return [{"name": p.stem, "yaml": p.read_text()} for p in sorted(TEMPLATES.glob("*.yaml"))]

    @app.post("/api/projects", dependencies=auth)
    def project_create(body: ProjectInput) -> dict:
        """Create an initiative without implicitly starting agents."""
        values = body.model_dump()
        root = Path(values.pop("root")).expanduser()
        return workbench.store.create(root, **values).model_dump()

    @app.post("/api/projects/{project}/jobs", dependencies=auth)
    def job_create(project: str, body: JobInput) -> dict:
        """Validate and snapshot the selected YAML workflow for a planned job."""
        values = body.model_dump()
        template = WorkflowTemplate.parse(values.pop("workflow_yaml"))
        return workbench.store.add_job(project, workflow=template, **values).model_dump()

    @app.post("/api/projects/{project}/jobs/{job}/start", dependencies=auth)
    def start(project: str, job: str) -> dict:
        """Provision the job worktree and launch its supervisor only on explicit start."""
        client, request = workbench.store.start_job(project, job)
        supervisor.start(client.area)
        client.await_result(request)
        return {"request_id": request, "status": "accepted"}

    @app.put("/api/projects/{project}/jobs/{job}/pr", dependencies=auth)
    def assign_pr(project: str, job: str, body: PullRequest) -> dict:
        """Assign searchable PR metadata without publishing to a hosting service."""
        return workbench.store.assign_pr(project, job, **body.model_dump())

    @app.post("/api/projects/{project}/{action}", dependencies=auth)
    def project_action(project: str, action: Literal["complete", "archive"]) -> dict:
        """Retire completed jobs and retain or archive the accepted project code."""
        return getattr(workbench.store, action)(project)

    @app.get("/api/projects/{project}/activity", dependencies=auth)
    def activity(project: str) -> list[dict]:
        """Read the latest accepted events for this project and its jobs."""
        return workbench.activity(project)

    @app.get("/api/projects/{project}/jobs/{job}/artifacts/{reference}", dependencies=auth)
    def artifact(project: str, job: str, reference: str) -> dict:
        """Read and verify an immutable execution artifact manifest."""
        return workbench.artifact(project, job, reference)

    @app.get(
        "/api/projects/{project}/jobs/{job}/executions/{execution}/artifacts", dependencies=auth
    )
    def execution_artifacts(project: str, job: str, execution: str) -> dict:
        """List commits and messages attributed to an exact task execution."""
        return workbench.execution_artifacts(project, job, execution)

    @app.get(
        "/api/projects/{project}/jobs/{job}/executions/{execution}/artifacts/{kind}/{item}",
        dependencies=auth,
    )
    def execution_text(project: str, job: str, execution: str, kind: str, item: str) -> dict:
        """Read the retained text of a commit, mailbox message, or outcome report."""
        return workbench.execution_text(project, job, execution, kind, item)

    @app.get("/api/projects/{project}/jobs/{job}/files", dependencies=auth)
    def files(project: str, job: str, artifact: str | None = None) -> dict:
        """List code from the worktree or a selected artifact version."""
        return workbench.files(project, job, artifact)

    @app.get("/api/projects/{project}/jobs/{job}/file", dependencies=auth)
    def file(project: str, job: str, name: str, artifact: str | None = None) -> dict:
        """Read bounded code with path and selected-version checks."""
        return workbench.file(project, job, name, artifact)

    @app.post("/api/projects/{project}/jobs/{job}/sessions/{session_id}", dependencies=auth)
    def session_action(project: str, job: str, session_id: str, body: SessionInput) -> dict:
        """Send an explicit generation-bound session operation to the single writer."""
        client = workbench.client(project, job, validate_worktree=body.action != "stop")
        payload = {"session_id": session_id, "generation": body.generation}
        if body.action == "interact":
            payload["text"] = body.text
        request = client.request(body.action, payload, body.request_id)
        supervisor.start(client.area)
        client.await_result(request)
        return {"request_id": request, "status": "accepted"}

    @app.post("/api/projects/{project}/jobs/{job}/respond", dependencies=auth)
    def respond(project: str, job: str, body: ResponseInput) -> dict:
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

    @app.post("/api/projects/{project}/jobs/{job}/action", dependencies=auth)
    def job_action(project: str, job: str, body: JobAction) -> dict:
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

    @app.websocket("/api/terminal/{project}/{job}/{session_id}/{generation}")
    async def terminal(socket: WebSocket, project: str, job: str, session_id: str, generation: int):
        """Attach a view to an existing owned terminal after origin and capability checks."""
        if socket.headers.get("host") not in hosts or socket.headers.get("origin") not in origins:
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
            if not hmac.compare_digest(supplied["token"].encode(), app.state.token.encode()):
                raise ValueError("invalid terminal capability")
            from ctlrm.web.terminal import bridge

            await bridge(socket, workbench, project, job, session_id, generation)
        except (ValueError, OSError, RuntimeError, asyncio.TimeoutError) as error:
            await socket.close(code=1008, reason=str(error).encode()[:120].decode(errors="ignore"))
        except WebSocketDisconnect:
            pass

    @app.get("/")
    def index() -> FileResponse:
        """Serve the packaged workbench shell; it contains no access capability."""
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
