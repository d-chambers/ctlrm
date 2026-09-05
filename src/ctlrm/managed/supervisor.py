"""A detached single-writer supervisor whose lifetime is independent of CLI/TUI."""

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import time

from ctlrm.managed.area import Area
from ctlrm.managed.sessions import SessionEngine
from ctlrm.managed.storage import read_record
from ctlrm.runtime.filesystem import mkdir_shared


def running(area: Area) -> bool:
    """Check the persistent lock instead of trusting a stale PID file."""
    path = area.room.root / "supervisor/lock"
    try:
        info = path.stat()
        owner = read_record(path)
        if owner.get("purpose") != "supervisor":
            return False
        for line in Path("/proc/locks").read_text().splitlines():
            fields = line.split()
            if len(fields) < 6 or fields[1] != "FLOCK" or fields[4] != str(owner.get("pid")):
                continue
            major, minor, inode = fields[5].split(":")
            if (int(major, 16), int(minor, 16), int(inode)) == (
                os.major(info.st_dev),
                os.minor(info.st_dev),
                info.st_ino,
            ):
                return True
    except (FileNotFoundError, ValueError):
        return False
    return False


@contextmanager
def _writer(area: Area):
    """Allow startup races with a brief initializer or another starting daemon."""
    deadline = time.monotonic() + 5
    while True:
        lock = area.journal.writer()
        try:
            lock.__enter__()
            break
        except RuntimeError:
            if running(area) or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def start(area: Area, interval: float = 1) -> None:
    """Start a detached supervisor once and wait briefly for lock acquisition."""
    if not 0.1 <= interval <= 60:
        raise ValueError("supervisor interval must be between 0.1 and 60 seconds")
    if area.journal.replay()[0].get("retired"):
        raise ValueError("job is retired; its runtime is read-only")
    if running(area):
        return
    mkdir_shared(area.room.root / "supervisor/logs")
    path = area.room.root / "supervisor/logs/supervisor.log"
    with path.open("ab") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ctlrm.managed.supervisor",
                str(area.root),
                "--interval",
                str(interval),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if running(area):
            return
        if process.poll() is not None:
            if process.returncode == 0 and area.journal.replay()[0].get("retired"):
                return
            raise RuntimeError(f"supervisor failed to start; inspect {path}")
        time.sleep(0.05)
    raise RuntimeError(f"supervisor has not acquired its lock; inspect {path}")


def serve(area: Area, interval: float = 1) -> None:
    """Replay committed state and poll requests/processes while holding one lock."""
    if not 0.1 <= interval <= 60:
        raise ValueError("supervisor interval must be between 0.1 and 60 seconds")
    with _writer(area):
        if area.journal.replay()[0].get("retired"):
            raise ValueError("job is retired; its runtime is read-only")
        area.ensure_room()
        from ctlrm.managed.workflows import WorkflowEngine

        engine = WorkflowEngine(area) if area.data["mode"] == "workflow" else SessionEngine(area)
        engine.state["shutdown"] = False
        engine.commit("supervisor-started")
        previous_errors = []
        while True:
            errors = engine.tick()
            if errors != previous_errors:
                for error in errors:
                    print(error, file=sys.stderr, flush=True)
                previous_errors = errors
            if engine.state["shutdown"]:
                return
            time.sleep(interval)


def main() -> None:
    """Run foreground for service managers or as the detached client child."""
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--interval", type=float, default=1)
    args = parser.parse_args()
    serve(Area.load(args.root), args.interval)


if __name__ == "__main__":
    main()
