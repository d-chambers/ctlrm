"""Owned terminal management through libtmux, with process identity checks."""

from pathlib import Path
from functools import wraps
import os
import shutil
import subprocess
import shlex
import signal
import time
import sys
from typing import Protocol

import libtmux

from ctlrm.managed.storage import read_record


class StopPending(RuntimeError):
    """An owned process has been signaled but has not exited yet."""


def _translate_errors(method):
    """Keep library-specific exceptions inside the terminal adapter."""

    @wraps(method)
    def call(*args, **kwargs):
        """Translate a failed tmux operation into the service error contract."""
        try:
            return method(*args, **kwargs)
        except libtmux.exc.LibTmuxException as error:
            raise RuntimeError(f"tmux operation failed: {error}") from error

    return call


def process_identity(pid: int) -> str | None:
    """Return an OS start identity, treating dead/zombie processes as absent."""
    try:
        value = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if value[0] == "Z":
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"{boot}:{pid}:{value[19]}"
    except FileNotFoundError:
        return None


def children(pid: int) -> list[int]:
    """Read direct provider children of the stable host process on Linux."""
    try:
        text = Path(f"/proc/{pid}/task/{pid}/children").read_text()
        return [int(value) for value in text.split() if process_identity(int(value))]
    except FileNotFoundError:
        return []


class Terminal(Protocol):
    """Boundary faked by deterministic supervisor tests."""

    def exists(self, spec: dict) -> bool:
        """Return whether the deterministic terminal name is occupied."""
        ...

    def ensure(self, spec: dict) -> dict:
        """Create or reconcile a verified owned terminal."""
        ...

    def adopt(self, spec: dict) -> dict:
        """Verify an existing launch without creating one if it disappeared."""
        ...

    def health(self, spec: dict, identity: dict) -> str:
        """Return running, exited, or missing; mismatched ownership raises."""
        ...

    def wake(self, spec: dict, identity: dict, reference: str) -> None:
        """Deliver a bounded literal mailbox reference to a verified provider."""
        ...

    def stop(self, spec: dict, identity: dict) -> None:
        """Stop only the verified session and its provider processes."""
        ...


class TmuxTerminal:
    """One isolated server namespace per coordination area."""

    def __init__(self, area_id: str) -> None:
        """Use a dedicated socket and ignore user tmux configuration."""
        self.socket = f"ctlrm-{area_id}"
        self.server = libtmux.Server(socket_name=self.socket, config_file="/dev/null")

    def _session(self, spec: dict):
        """Find one exact session name without selecting user terminals."""
        return self.server.sessions.get(session_name=spec["terminal_name"], default=None)

    @_translate_errors
    def exists(self, spec: dict) -> bool:
        """Return whether this generation's terminal name is occupied."""
        return self._session(spec) is not None

    def _identity(self, spec: dict, session) -> dict:
        """Verify creation-time environment, host command, root, and process identity."""
        panes = list(session.panes)
        if len(panes) != 1:
            raise ValueError("owned session has an unexpected pane layout")
        pane = panes[0]
        token = session.cmd("show-environment", "CTLRM_LAUNCH_TOKEN").stdout
        if token != [f"CTLRM_LAUNCH_TOKEN={spec['token']}"]:
            raise ValueError("terminal ownership token mismatch")
        host_pid = int(pane.pane_pid)
        expected = [
            sys.executable,
            "-m",
            "ctlrm.managed.host",
            spec["root"],
            spec["id"],
            str(spec["generation"]),
        ]
        try:
            command = Path(f"/proc/{host_pid}/cmdline").read_bytes().split(b"\0")
            actual = [part.decode() for part in command if part]
            cwd = Path(f"/proc/{host_pid}/cwd").resolve(strict=True)
        except FileNotFoundError as error:
            raise ValueError("terminal host disappeared during identity validation") from error
        if actual != expected or cwd != Path(spec["root"]):
            raise ValueError(
                "terminal host command or worktree mismatch; inspect before reconciliation"
            )
        server_pid = int(pane.cmd("display-message", "-p", "#{pid}").stdout[0])
        host_start, server_start = process_identity(host_pid), process_identity(server_pid)
        if not host_start or not server_start:
            raise ValueError("terminal process identity is unavailable")
        return {
            "socket": self.socket,
            "socket_path": pane.cmd("display-message", "-p", "#{socket_path}").stdout[0],
            "server": server_start,
            "host": host_start,
            "pane": pane.pane_id,
            "host_pid": host_pid,
            "session": session.session_id,
        }

    @_translate_errors
    def ensure(self, spec: dict) -> dict:
        """Create a stable host or adopt only an exact creation-time identity match."""
        session = self._session(spec)
        if session is None:
            command = [
                sys.executable,
                "-m",
                "ctlrm.managed.host",
                spec["root"],
                spec["id"],
                str(spec["generation"]),
            ]
            session = self.server.new_session(
                session_name=spec["terminal_name"],
                start_directory=spec["root"],
                window_command="exec " + shlex.join(command),
                environment={"CTLRM_LAUNCH_TOKEN": spec["token"]},
                x=120,
                y=40,
            )
        deadline = time.monotonic() + 2
        while True:
            try:
                identity = self._identity(spec, session)
                break
            except ValueError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        session.set_option("@ctlrm-area", spec["area_id"])
        session.set_option("@ctlrm-session", spec["id"])
        session.set_option("@ctlrm-generation", str(spec["generation"]))
        return identity

    @_translate_errors
    def adopt(self, spec: dict) -> dict:
        """Reconcile an existing launch without starting a process on a race."""
        session = self._session(spec)
        if session is None:
            raise ValueError("launch disappeared before readiness reconciliation")
        return self._identity(spec, session)

    def _verified(self, spec: dict, identity: dict):
        """Resolve a recorded terminal and reject server restart/PID reuse."""
        session = self._session(spec)
        if session is None:
            return None
        if self._identity(spec, session) != identity:
            raise ValueError("terminal identity changed; refusing to adopt or stop it")
        return session

    @_translate_errors
    def health(self, spec: dict, identity: dict) -> str:
        """A live host with no provider child is an exited agent, not a healthy pane."""
        session = self._verified(spec, identity)
        if session is None:
            return "missing"
        if children(identity["host_pid"]):
            return "running"
        exit_path = (
            Path(spec["root"]) / ".ctlrm/sessions" / spec["id"] / f"exit-{spec['generation']}.json"
        )
        if exit_path.exists():
            if read_record(exit_path).get("token") != spec["token"]:
                raise ValueError("provider exit observation has a mismatched launch token")
            return "exited"
        return "starting"

    @_translate_errors
    def wake(self, spec: dict, identity: dict, reference: str) -> None:
        """Type only a bounded ASCII reference, never a user's multiline prompt."""
        if (
            not reference.isascii()
            or len(reference.encode()) > 256
            or "\n" in reference
            or "\r" in reference
        ):
            raise ValueError("terminal references must be a single ASCII line of at most 256 bytes")
        session = self._verified(spec, identity)
        if session is None or self.health(spec, identity) != "running":
            raise ValueError("provider is not running")
        session.panes[0].send_keys(reference, literal=True)

    @_translate_errors
    def stop(self, spec: dict, identity: dict) -> None:
        """Kill only the verified dedicated session, propagating terminal hangup."""
        session = self._verified(spec, identity)
        if session is not None:
            host_pid = identity["host_pid"]
            group = os.getpgid(host_pid)
            if group != host_pid:
                raise ValueError("terminal host does not own its process group")
            os.killpg(group, signal.SIGKILL)
            # Killing the dedicated process group also closes the pane/session.
            deadline = time.monotonic() + 2
            while process_identity(host_pid) == identity["host"] and time.monotonic() < deadline:
                time.sleep(0.02)
        if process_identity(identity["host_pid"]) == identity["host"]:
            raise StopPending("terminal stop is pending; host process still exists")

    @_translate_errors
    def capture(self, spec: dict, identity: dict) -> str:
        """Capture bounded recent output from a verified owned terminal."""
        session = self._verified(spec, identity)
        if session is None:
            return "[terminal is missing]"
        return "\n".join(session.panes[0].capture_pane(start=-200) or [])

    @_translate_errors
    def attach(self, spec: dict, identity: dict) -> None:
        """Attach the interactive client through libtmux to a verified session."""
        session = self._verified(spec, identity)
        if session is None:
            raise ValueError("terminal is missing")
        # libtmux 0.62 captures stdout/stderr, which prevents interactive attachment.
        # Lifecycle and target verification remain library-owned; this is the native UI client.
        binary = self.server.tmux_bin or shutil.which("tmux")
        if not binary:
            raise RuntimeError("tmux executable is missing")
        result = subprocess.run(
            [binary, "-S", identity["socket_path"], "attach-session", "-t", session.session_id]
        )
        if result.returncode:
            raise RuntimeError(f"interactive tmux client exited with status {result.returncode}")
