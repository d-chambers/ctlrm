"""A detached single-writer supervisor whose lifetime is independent of CLI/TUI."""

import argparse
from pathlib import Path
import subprocess
import sys
import time

from ctlrm.managed.area import Area
from ctlrm.managed.sessions import SessionEngine
from ctlrm.runtime.filesystem import mkdir_shared


def running(area: Area) -> bool:
    """Check the persistent lock instead of trusting a stale PID file."""
    try:
        with area.journal.writer():
            return False
    except RuntimeError:
        return True


def start(area: Area, interval: float = 1) -> None:
    """Start a detached supervisor once and wait briefly for lock acquisition."""
    if not 0.1 <= interval <= 60:
        raise ValueError("supervisor interval must be between 0.1 and 60 seconds")
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
            raise RuntimeError(f"supervisor failed to start; inspect {path}")
        time.sleep(0.05)
    raise RuntimeError(f"supervisor has not acquired its lock; inspect {path}")


def serve(area: Area, interval: float = 1) -> None:
    """Replay committed state and poll requests/processes while holding one lock."""
    if not 0.1 <= interval <= 60:
        raise ValueError("supervisor interval must be between 0.1 and 60 seconds")
    with area.journal.writer():
        area.ensure_room()
        engine = SessionEngine(area)
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
