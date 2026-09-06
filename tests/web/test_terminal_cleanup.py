"""Cancellation must release the browser attachment process and PTY descriptors."""

import asyncio
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
