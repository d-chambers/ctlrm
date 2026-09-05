"""Detached process acceptance for supervisor and terminal lifetime separation."""

import time

from ctlrm.communication.terminal import TmuxTerminal
from ctlrm.managed.sessions import SessionService
from ctlrm.managed.supervisor import running, start


def wait_for(predicate, timeout: float = 10) -> None:
    """Wait for an observable process boundary with a bounded deadline."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("supervisor did not reach the expected state")


class TestDetachedSupervisor:
    """Stopping and restarting supervision preserves a live provider exactly once."""

    def test_terminal_survives_supervisor_restart(self, area) -> None:
        """Exercise the real daemon entrypoint, persistent lock, and owned libtmux server."""
        client = SessionService(area)
        terminal = TmuxTerminal(area.data["id"])
        client.request("launch", {"participant": "agent"}, "start-agent")
        try:
            start(area, 0.1)
            start(area, 0.1)
            wait_for(
                lambda: any(
                    s.get("identity") for s in area.journal.replay()[0]["sessions"].values()
                )
            )
            session = next(iter(area.journal.replay()[0]["sessions"].values()))
            client.await_result(client.ready(session["id"], 1, session["native_id"]))
            client.await_result(client.request("supervisor-stop", {}))
            wait_for(lambda: not running(area))
            assert terminal.health(session["spec"], session["identity"]) == "running"
            start(area, 0.1)
            restored = next(iter(area.journal.replay()[0]["sessions"].values()))
            assert restored["identity"] == session["identity"]
            assert restored["generation"] == 1
            client.await_result(client.request("stop", {"session_id": session["id"]}))
            wait_for(
                lambda: area.journal.replay()[0]["sessions"][session["id"]]["status"] == "stopped"
            )
            assert terminal.health(session["spec"], session["identity"]) == "missing"
        finally:
            if running(area):
                client.request("supervisor-stop", {})
                wait_for(lambda: not running(area))
            terminal.server.cmd("kill-server")
