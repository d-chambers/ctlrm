"""Real libtmux tests on isolated servers and disposable worktrees."""

from pathlib import Path
import sys
import time
from uuid import uuid4

import pytest

from ctlrm.communication.terminal import TmuxTerminal
from ctlrm.managed.storage import publish


class TestOwnedTerminal:
    """Real terminals enforce identity and distinguish provider exit from a live host."""

    def test_create_adopt_capture_and_stop(self, tmp_path: Path) -> None:
        """A deterministic generation has one host and literal, bounded wake-ups."""
        token = uuid4().hex
        terminal = TmuxTerminal(token)
        spec = {
            "id": "session-test",
            "participant": "agent",
            "generation": 1,
            "area_id": token,
            "root": str(tmp_path),
            "token": token,
            "terminal_name": "owned-test",
            "argv": [
                sys.executable,
                "-u",
                "-c",
                "import sys; print('READY', flush=True); [print('GOT:'+line, flush=True) for line in sys.stdin]",
            ],
        }
        publish(tmp_path / ".ctlrm/sessions/session-test/launch-1.json", spec)
        try:
            identity = terminal.ensure(spec)
            assert terminal.ensure(spec) == identity
            deadline = time.monotonic() + 5
            while terminal.health(spec, identity) != "running" and time.monotonic() < deadline:
                time.sleep(0.02)
            terminal.wake(spec, identity, "Read literal-$HOME-`text`-semi;colon")
            deadline = time.monotonic() + 5
            while (
                "GOT:Read literal-$HOME-`text`-semi;colon" not in terminal.capture(spec, identity)
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            assert "GOT:Read literal-$HOME-`text`-semi;colon" in terminal.capture(spec, identity)
            with pytest.raises(ValueError, match="single ASCII"):
                terminal.wake(spec, identity, "first\nsecond")
            with pytest.raises(ValueError, match="identity changed"):
                terminal.stop(spec, {**identity, "server": "reused-pid"})
            assert terminal.health(spec, identity) == "running"
            terminal.stop(spec, identity)
            assert terminal.health(spec, identity) == "missing"
        finally:
            terminal.server.cmd("kill-server")

    def test_live_host_dead_provider(self, tmp_path: Path) -> None:
        """A provider that exits leaves an inspectable host, not a false healthy session."""
        token = uuid4().hex
        terminal = TmuxTerminal(token)
        spec = {
            "id": "session-exit",
            "participant": "agent",
            "generation": 1,
            "area_id": token,
            "root": str(tmp_path),
            "token": token,
            "terminal_name": "owned-exit",
            "argv": [sys.executable, "-c", "print('finished')"],
        }
        publish(tmp_path / ".ctlrm/sessions/session-exit/launch-1.json", spec)
        try:
            identity = terminal.ensure(spec)
            deadline = time.monotonic() + 5
            while (
                "provider exited" not in terminal.capture(spec, identity)
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            assert terminal.exists(spec)
            assert terminal.health(spec, identity) == "exited"
            terminal.stop(spec, identity)
        finally:
            terminal.server.cmd("kill-server")
