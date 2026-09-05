"""Real browser PTY traffic cannot stop or replace its managed agent by detaching."""

from pathlib import Path
import sys
import time

from ctlrm.communication.terminal import TmuxTerminal
from ctlrm.managed.area import Area
from ctlrm.managed.sessions import SessionService
from ctlrm.managed.workflows import WorkflowEngine
from ctlrm.runtime.workflows import WorkflowTemplate


class TestTerminalBridge:
    """Exercise authentication, real keyboard/output traffic, generation checks, and cleanup."""

    def test_attach_input_detach(self, browser, repository) -> None:
        """The browser controls an existing tmux pane while its disposable client owns no agent."""
        client, store = browser
        store.create(repository, "Terminal test", ["Exercise real PTY"], project_id="terminal")
        template = WorkflowTemplate.model_validate(
            {
                "name": "echo",
                "entry": "work",
                "profiles": {
                    "echo": {
                        "provider": "command",
                        "executable": sys.executable,
                        "context": str(repository),
                        "input_mode": "manual",
                        "launch_arguments": [
                            "-u",
                            "-c",
                            "import sys; print('READY'); [print('GOT:'+line, flush=True) for line in sys.stdin]",
                        ],
                        "resume_arguments": ["-c", "import time; time.sleep(60)", "{native_id}"],
                    }
                },
                "roles": {"agent": {"kind": "agent", "profile": "echo"}},
                "tasks": {
                    "work": {
                        "role": "agent",
                        "verify_input": False,
                        "transitions": {"done": "terminal:completed"},
                    }
                },
            }
        )
        store.add_job("terminal", "Echo", "Accept terminal input", template, job_id="echo")
        assert client.post("/api/projects/terminal/jobs/echo/start", json={}).status_code == 200
        area = Area.load(Path(store.job_status("terminal", "echo")["launch"]["root"]))
        terminal = TmuxTerminal(area.data["id"])
        try:
            session = next(iter(area.journal.replay()[0]["sessions"].values()))
            service = SessionService(area)
            request = service.ready(session["id"], 1, session["native_id"])
            service.await_result(request)
            session = area.journal.replay()[0]["sessions"][session["id"]]
            deadline = time.monotonic() + 5
            while (
                "READY" not in terminal.capture(session["spec"], session["identity"])
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            path = f"/api/terminal/terminal/echo/{session['id']}/1"
            headers = {"Origin": "http://127.0.0.1:8766", "Host": "127.0.0.1:8766"}
            with client.websocket_connect(path, headers=headers) as socket:
                socket.send_json({"token": "test-capability"})
                socket.send_json({"type": "resize", "cols": 100, "rows": 28})
                socket.send_json({"type": "input", "data": "browser-marker\r"})
                output = b""
                for _ in range(20):
                    output += socket.receive_bytes()
                    if b"GOT:browser-marker" in output:
                        break
                assert b"GOT:browser-marker" in output
            assert terminal.health(session["spec"], session["identity"]) == "running"
            assert "GOT:browser-marker" in terminal.capture(session["spec"], session["identity"])
            snapshot = client.get("/api/snapshot").json()
            public = snapshot["participants"][0]["session"]
            assert "spec" not in public and "profile" not in public
            stale = client.post(
                f"/api/projects/terminal/jobs/echo/sessions/{session['id']}",
                json={
                    "action": "stop",
                    "generation": 2,
                    "request_id": "stale-stop",
                },
            )
            assert stale.status_code == 409
            assert terminal.health(session["spec"], session["identity"]) == "running"
            with client.websocket_connect(path, headers=headers) as socket:
                socket.send_json({"token": "test-capability"})
                socket.send_json({"type": "resize", "cols": 0, "rows": 5000})
                for _ in range(20):
                    frame = socket.receive()
                    if frame["type"] == "websocket.close":
                        assert frame["code"] == 1008
                        break
                else:
                    raise AssertionError("invalid dimensions did not close the view")
            assert terminal.health(session["spec"], session["identity"]) == "running"
        finally:
            with area.journal.writer():
                service.request("workflow-cancel", {})
                WorkflowEngine(area).tick()
            terminal.server.cmd("kill-server")
