"""Disposable Git worktrees and fake provider boundaries for managed runtime tests."""

from pathlib import Path
import subprocess
import sys

import pytest

from ctlrm.managed.area import Area
from ctlrm.managed.providers import ProviderProfile
from ctlrm.managed.sessions import SessionEngine, SessionService


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """Create a committed local repository without touching user Git configuration."""

    def run(*args: str) -> None:
        """Run setup commands with per-invocation author identity."""
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.test",
                *args,
            ],
            check=True,
            capture_output=True,
        )

    run("init", "-b", "main")
    (tmp_path / "source.txt").write_text("initial\n")
    run("add", "source.txt")
    run("commit", "-m", "Initial")
    return tmp_path


@pytest.fixture
def profile(repository: Path) -> ProviderProfile:
    """Use a real local executable with an explicit test native-resume contract."""
    return ProviderProfile(
        provider="command",
        input_mode="unattended",
        executable=sys.executable,
        context=str(repository),
        launch_arguments=["-c", "import time; time.sleep(3600)"],
        resume_arguments=["-c", "import time; time.sleep(3600)", "{native_id}"],
        backoff=0,
    )


@pytest.fixture
def area(repository: Path, profile: ProviderProfile) -> Area:
    """Create one standalone area with an immutable agent role/profile."""
    return Area.create(
        repository,
        mode="standalone",
        roles={
            "agent": {
                "name": "Agent",
                "role": "worker",
                "kind": "agent",
                "profile": "test",
                "instructions": "Do work",
            }
        },
        profiles={"test": profile.model_dump()},
        prompt="Standalone",
    )


class FakeTerminal:
    """Model process liveness independently from terminal existence."""

    def __init__(self) -> None:
        """Start with no terminals or side effects."""
        self.terminals = {}
        self.launches = 0
        self.wakes = []
        self.crash = False

    def exists(self, spec: dict) -> bool:
        """Query the deterministic name's occupancy."""
        return spec["terminal_name"] in self.terminals

    def ensure(self, spec: dict) -> dict:
        """Create once and allow crash injection immediately after external creation."""
        name = spec["terminal_name"]
        if name not in self.terminals:
            self.launches += 1
            self.terminals[name] = {"token": spec["token"], "health": "running"}
        if self.terminals[name]["token"] != spec["token"]:
            raise ValueError("ownership mismatch")
        if self.crash:
            self.crash = False
            raise KeyboardInterrupt("simulated supervisor crash")
        return {"name": name, "token": spec["token"]}

    def health(self, spec: dict, identity: dict) -> str:
        """A shell can remain after its provider exits."""
        value = self.terminals.get(identity["name"])
        if value is None:
            return "missing"
        if value["token"] != identity["token"]:
            raise ValueError("ownership mismatch")
        return value["health"]

    def wake(self, spec: dict, identity: dict, reference: str) -> None:
        """Record bounded references instead of executing provider effects."""
        self.wakes.append(reference)

    def stop(self, spec: dict, identity: dict) -> None:
        """Refuse unrelated terminals and stop the exact generation."""
        self.health(spec, identity)
        self.terminals.pop(identity["name"], None)


@pytest.fixture
def managed(area: Area):
    """Hold one supervisor writer for deterministic service operations."""
    terminal = FakeTerminal()
    with area.journal.writer():
        engine = SessionEngine(area, terminal)
        client = SessionService(area)
        client.request("launch", {"participant": "agent"}, "launch-request")
        engine.tick()
        session = next(iter(engine.state["sessions"].values()))
        client.ready(session["id"], 1, session["native_id"])
        engine.tick()
        yield area, engine, terminal, client
