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


class TestTerminalErrors:
    """Library exception types do not escape the adapter's public error contract."""

    def test_libtmux_error_is_translated(self, monkeypatch) -> None:
        """The engine can block one failed launch instead of crashing supervision."""
        import libtmux

        terminal = TmuxTerminal(uuid4().hex)

        def missing(spec):
            """Simulate the library's non-RuntimeError exception hierarchy."""
            raise libtmux.exc.LibTmuxException("tmux unavailable")

        monkeypatch.setattr(terminal, "_session", missing)
        with pytest.raises(RuntimeError, match="tmux operation failed"):
            terminal.ensure({})


class TestInteractiveAttach:
    """The native interactive client must inherit a real controlling terminal."""

    def test_attach_and_detach(self, tmp_path: Path) -> None:
        """Real PTY attachment succeeds and detaching leaves the provider running."""
        import json
        import os
        import pty
        import signal

        token = uuid4().hex
        terminal = TmuxTerminal(token)
        spec = {
            "id": "session-attach",
            "participant": "agent",
            "generation": 1,
            "area_id": token,
            "root": str(tmp_path),
            "token": token,
            "terminal_name": "owned-attach",
            "argv": [sys.executable, "-c", "import time; time.sleep(60)"],
        }
        publish(tmp_path / ".ctlrm/sessions/session-attach/launch-1.json", spec)
        pid = None
        descriptor = None
        reaped = False
        try:
            identity = terminal.ensure(spec)
            data = tmp_path / "attach.json"
            data.write_text(json.dumps({"spec": spec, "identity": identity}))
            pid, descriptor = pty.fork()
            if pid == 0:
                os.environ["TERM"] = "xterm-256color"
                script = "import json,sys; from pathlib import Path; from ctlrm.communication.terminal import TmuxTerminal; d=json.loads(Path(sys.argv[1]).read_text()); TmuxTerminal(d['spec']['area_id']).attach(d['spec'], d['identity'])"
                os.execv(sys.executable, [sys.executable, "-c", script, str(data)])
            deadline = time.monotonic() + 10
            while not terminal.server.clients and time.monotonic() < deadline:
                time.sleep(0.05)
            assert terminal.server.clients
            os.write(descriptor, b"\x02d")
            while time.monotonic() < deadline:
                child, status = os.waitpid(pid, os.WNOHANG)
                if child:
                    reaped = True
                    assert os.waitstatus_to_exitcode(status) == 0
                    break
                time.sleep(0.05)
            assert reaped
            assert terminal.health(spec, identity) == "running"
            terminal.stop(spec, identity)
        finally:
            if pid and not reaped:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            if descriptor is not None:
                os.close(descriptor)
            terminal.server.cmd("kill-server")
