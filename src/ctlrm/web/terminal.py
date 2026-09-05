"""A browser terminal attaches a disposable PTY client to a verified managed tmux session."""

import asyncio
import fcntl
import json
import os
import pty
import struct
import subprocess
import sys
import termios

from fastapi import WebSocket

from ctlrm.communication.terminal import TmuxTerminal
from ctlrm.web.service import Workbench


async def bridge(
    socket: WebSocket,
    workbench: Workbench,
    project: str,
    job: str,
    session_id: str,
    generation: int,
) -> None:
    """Stream an existing session; closing the browser kills only its attachment client.

    Parameters
    ----------
    socket : WebSocket
        Authenticated browser connection.
    workbench : Workbench
        Authoritative job/session resolver.
    project, job, session_id : str
        Recorded ownership identifiers, never arbitrary terminal targets.
    generation : int
        Generation observed when opening the terminal.
    """
    area, session = await asyncio.to_thread(workbench.session, project, job, session_id, generation)
    terminal = TmuxTerminal(area.data["id"])
    readonly = session["profile"]["input_mode"] == "native"
    argv = await asyncio.to_thread(
        terminal.attach_command, session["spec"], session["identity"], readonly=readonly
    )
    master, slave = pty.openpty()
    process = None
    tasks = []
    try:
        os.set_blocking(master, False)
        environment = {k: v for k, v in os.environ.items() if k not in {"TMUX", "TMUX_PANE"}}
        environment["TERM"] = "xterm-256color"
        # A separate child establishes the controlling terminal; no preexec_fn in a threaded server.
        process = subprocess.Popen(
            [sys.executable, "-m", "ctlrm.web.terminal", *argv],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            env=environment,
        )
        os.close(slave)
        slave = -1

        async def output() -> None:
            """Bound output buffering and stop the view on process or ownership changes."""
            checked = 0.0
            while process.poll() is None:
                if asyncio.get_running_loop().time() - checked > 1:
                    current_area, current = await asyncio.to_thread(
                        workbench.session, project, job, session_id, generation
                    )
                    if current["identity"] != session["identity"]:
                        raise ValueError("terminal ownership changed")
                    await asyncio.to_thread(
                        terminal.attach_command,
                        current["spec"],
                        current["identity"],
                        readonly=readonly,
                    )
                    checked = asyncio.get_running_loop().time()
                try:
                    data = os.read(master, 65536)
                except BlockingIOError:
                    await asyncio.sleep(0.02)
                    continue
                if not data:
                    return
                await asyncio.wait_for(socket.send_bytes(data), 5)

        async def input_frames() -> None:
            """Accept bounded keyboard input and dimensions without interpreting shell commands."""
            while True:
                raw = await socket.receive_text()
                if len(raw.encode()) > 65536:
                    raise ValueError("terminal input frame is too large")
                frame = json.loads(raw)
                if not isinstance(frame, dict):
                    raise ValueError("invalid terminal frame")
                if frame.get("type") == "resize":
                    cols, rows = frame.get("cols"), frame.get("rows")
                    if (
                        type(cols) is not int
                        or type(rows) is not int
                        or not (20 <= cols <= 500 and 5 <= rows <= 200)
                    ):
                        raise ValueError("invalid terminal dimensions")
                    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                elif frame.get("type") == "input" and isinstance(frame.get("data"), str):
                    if readonly:
                        raise ValueError(
                            "native conversations accept messages through the composer"
                        )
                    data = frame["data"].encode()
                    deadline = asyncio.get_running_loop().time() + 5
                    while data:
                        try:
                            written = os.write(master, data)
                            data = data[written:]
                        except BlockingIOError:
                            if asyncio.get_running_loop().time() > deadline:
                                raise ValueError("terminal input is blocked")
                            await asyncio.sleep(0.02)
                else:
                    raise ValueError("invalid terminal frame")

        tasks = [asyncio.create_task(output()), asyncio.create_task(input_frames())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        await socket.close(code=1000, reason="terminal detached")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait)
        os.close(master)
        if slave >= 0:
            os.close(slave)


def main() -> None:
    """Give the native tmux client its controlling PTY, then replace this helper process."""
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execv(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
