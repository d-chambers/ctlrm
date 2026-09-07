"""Cancellation must release the browser attachment process and PTY descriptors."""

import asyncio
import errno
from contextlib import suppress
import os
import sys
import time
from types import SimpleNamespace

import anyio
import pytest

from ctlrm.web import terminal as bridge_module


class TestCanceledAttachment:
    """Exercise ASGI-style cancellation with a real disposable attachment child."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ignore_term", [False, True])
    async def test_releases_resources(self, monkeypatch, ignore_term) -> None:
        """Cancellation during streaming must finish cleanup before the request task exits."""
        processes, descriptors = [], []
        ready = asyncio.Event()
        popen, openpty = bridge_module.subprocess.Popen, bridge_module.pty.openpty

        def start_process(*args, **kwargs):
            """Record only the disposable child owned by this bridge."""
            process = popen(*args, **kwargs)
            processes.append(process)
            return process

        def create_pty():
            """Retain descriptor identities to verify they are closed."""
            pair = openpty()
            descriptors.extend(pair)
            return pair

        class Terminal:
            """Replace tmux only at the command-selection boundary."""

            def __init__(self, area_id):
                """Accept the already validated area's identity."""

            def attach_command(self, *args, **kwargs):
                """Use a real sleeping child with no access to an agent session."""
                code = "import time; time.sleep(60)"
                if ignore_term:
                    code = 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("READY", flush=True); time.sleep(60)'
                return [sys.executable, "-c", code]

        class Workbench:
            """Supply one already validated attachment identity."""

            def session(self, *args):
                """Return the minimum session projection consumed by the bridge."""
                return SimpleNamespace(data={"id": "test"}), {
                    "profile": {"input_mode": "manual"},
                    "spec": {},
                    "identity": {},
                }

        class Socket:
            """Wait indefinitely until the enclosing ASGI request is canceled."""

            async def receive_text(self):
                """Leave input pending at the cancellation boundary."""
                await asyncio.Event().wait()

            async def send_bytes(self, data):
                """Observe readiness when testing a child that refuses graceful termination."""
                if b"READY" in data:
                    ready.set()

        monkeypatch.setattr(bridge_module.subprocess, "Popen", start_process)
        monkeypatch.setattr(bridge_module.pty, "openpty", create_pty)
        monkeypatch.setattr(bridge_module, "TmuxTerminal", Terminal)
        try:
            started = time.monotonic()
            async with anyio.create_task_group() as group:
                group.start_soon(bridge_module.bridge, Socket(), Workbench(), "p", "j", "s", 1)
                with anyio.fail_after(5):
                    while not processes:
                        await anyio.sleep(0.01)
                    if ignore_term:
                        await ready.wait()
                group.cancel_scope.cancel()
            assert time.monotonic() - started < 5, "cleanup waited for the child to exit naturally"
            assert processes[0].poll() is not None, "attachment child survived cancellation"
            for descriptor in descriptors:
                with pytest.raises(OSError):
                    os.fstat(descriptor)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            for descriptor in descriptors:
                with suppress(OSError):
                    os.close(descriptor)


class TestDetachedAttachment:
    """PTY EOF is a normal detach even when it races the child's liveness check."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error_number", [errno.EIO, errno.EBADF], ids=["eio-eof", "unexpected-error"]
    )
    async def test_read_race(self, monkeypatch, error_number) -> None:
        """Only EIO is treated as EOF; other descriptor failures still propagate after cleanup."""
        descriptors = []
        original_openpty, original_read = bridge_module.pty.openpty, os.read

        def openpty():
            """Use real descriptors while controlling the process-poll race."""
            pair = original_openpty()
            descriptors.extend(pair)
            return pair

        def read(descriptor, size):
            """Model the slave closing immediately after a successful poll."""
            if descriptors and descriptor == descriptors[0]:
                raise OSError(error_number, "injected descriptor error")
            return original_read(descriptor, size)

        class Process:
            """Keep poll live until the bridge cleans up its own attachment."""

            stopped = False

            def poll(self):
                """Expose the state before and after cleanup."""
                return 0 if self.stopped else None

            def terminate(self):
                """Record termination without touching any real agent."""
                self.stopped = True

            def wait(self, timeout=None):
                """Finish immediately once the fake attachment has stopped."""
                assert self.stopped
                return 0

        class Socket:
            """Keep input pending while recording the bridge's close status."""

            closed = None

            async def receive_text(self):
                """Let output determine when this attachment ends."""
                await asyncio.Event().wait()

            async def close(self, **kwargs):
                """Record the public WebSocket close code and reason."""
                self.closed = kwargs

        process, socket = Process(), Socket()
        terminal = SimpleNamespace(attach_command=lambda *args, **kwargs: ["unused"])
        workbench = SimpleNamespace(
            session=lambda *args: (
                SimpleNamespace(data={"id": "test"}),
                {"profile": {"input_mode": "manual"}, "spec": {}, "identity": {}},
            )
        )
        monkeypatch.setattr(bridge_module.pty, "openpty", openpty)
        monkeypatch.setattr(bridge_module.os, "read", read)
        monkeypatch.setattr(bridge_module.subprocess, "Popen", lambda *args, **kwargs: process)
        monkeypatch.setattr(bridge_module, "TmuxTerminal", lambda area_id: terminal)
        if error_number == errno.EIO:
            await bridge_module.bridge(socket, workbench, "p", "j", "s", 1)
            assert socket.closed == {"code": 1000, "reason": "terminal detached"}
        else:
            with pytest.raises(OSError) as caught:
                await bridge_module.bridge(socket, workbench, "p", "j", "s", 1)
            assert caught.value.errno == error_number
            assert socket.closed is None
        assert process.stopped
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
