"""Construct the loopback workbench and enforce its shared HTTP boundary."""

from collections.abc import Awaitable, Callable
from pathlib import Path
import secrets
import yaml

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ctlrm.managed.projects import ProjectStore
from ctlrm.web.api import router, terminal_router
from ctlrm.web.service import Workbench

STATIC = Path(__file__).parent / "static"


async def boundary(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Reject rebinding, cross-origin requests, and oversized request bodies."""
    if request.headers.get("host") not in request.app.state.hosts:
        return JSONResponse({"error": "invalid local host"}, status_code=403)
    origin = request.headers.get("origin")
    if origin and origin not in request.app.state.origins:
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


def domain_error(request: Request, error: Exception) -> JSONResponse:
    """Report domain rejections without exposing a server traceback."""
    status = 404 if isinstance(error, FileNotFoundError) else 409
    return JSONResponse({"error": str(error)}, status_code=status)


def index() -> FileResponse:
    """Serve the packaged workbench shell; it contains no access capability."""
    return FileResponse(STATIC / "index.html")


def create_app(
    store: ProjectStore | None = None, *, token: str | None = None, port: int = 8766
) -> FastAPI:
    """Bind one project store and capability to the exact loopback port."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.workbench = Workbench(store or ProjectStore())
    app.state.token = token or secrets.token_urlsafe(32)
    app.state.port = port
    app.state.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    app.state.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    app.middleware("http")(boundary)
    for error in (ValueError, OSError, RuntimeError, KeyError, yaml.YAMLError):
        app.add_exception_handler(error, domain_error)
    app.include_router(router)
    app.include_router(terminal_router)
    app.add_api_route("/", index, methods=["GET"])
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
