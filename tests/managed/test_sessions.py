"""Session readiness, idempotent delivery, and recovery failure boundaries."""

import pytest

from ctlrm.managed.sessions import SessionEngine, SessionService
from conftest import FakeTerminal


def session(engine) -> dict:
    """Select the fixture's single current session snapshot."""
    return next(iter(engine.state["sessions"].values()))


class TestStandalone:
    """Standalone sessions require no task or workflow graph."""

    def test_prompt_ack_and_completion(self, managed) -> None:
        """Large multiline content remains durable while terminal hints stay bounded."""
        area, engine, terminal, client = managed
        current = session(engine)
        payload = {"session_id": current["id"], "generation": 1, "text": "line\n" * 1000}
        client.request("prompt", payload, "prompt-one")
        engine.tick()
        work = session(engine)["work"]
        assert "line\n" * 1000 in work["message"]["body"]
        assert len(terminal.wakes[-1]) <= 256 and "\n" not in terminal.wakes[-1]
        client.request("prompt", payload, "prompt-one")
        client.request(
            "acknowledge", {"session_id": current["id"], "generation": 1, "work_id": work["id"]}
        )
        engine.tick()
        assert session(engine)["work"]["status"] == "acknowledged"
        client.request(
            "disposition",
            {
                "session_id": current["id"],
                "generation": 1,
                "work_id": work["id"],
                "status": "completed",
            },
        )
        engine.tick()
        assert session(engine)["work"]["status"] == "completed"
        assert "definition" in area.data and area.data["definition"] is None

    def test_dead_provider_reconciles_work(self, managed) -> None:
        """A live terminal shell cannot hide death or silently repeat acknowledged effects."""
        area, engine, terminal, client = managed
        current = session(engine)
        client.request(
            "prompt", {"session_id": current["id"], "generation": 1, "text": "Change code"}
        )
        engine.tick()
        work = session(engine)["work"]
        client.request(
            "acknowledge", {"session_id": current["id"], "generation": 1, "work_id": work["id"]}
        )
        engine.tick()
        terminal.terminals[current["spec"]["terminal_name"]]["health"] = "exited"
        engine.tick()
        engine.tick()
        engine.tick()
        recovered = session(engine)
        assert recovered["generation"] == 2 and recovered["native_id"] == current["native_id"]
        client.ready(current["id"], 2, current["native_id"])
        engine.tick()
        assert session(engine)["status"] == "reconciling"
        stale = client.request(
            "disposition",
            {
                "session_id": current["id"],
                "generation": 1,
                "work_id": work["id"],
                "status": "completed",
            },
        )
        engine.tick()
        assert engine.state["requests"][stale]["status"] == "rejected"
        client.request(
            "disposition",
            {
                "session_id": current["id"],
                "generation": 2,
                "work_id": work["id"],
                "status": "uncertain",
            },
        )
        engine.tick()
        assert session(engine)["status"] == "blocked"
        assert session(engine)["work"]["status"] == "acknowledged"

    def test_explicit_stop_does_not_restart(self, managed) -> None:
        """Stopping disables recovery even across supervisor reconstruction."""
        area, engine, terminal, client = managed
        client.request("stop", {"session_id": session(engine)["id"]})
        engine.tick()
        restarted = SessionEngine(area, terminal)
        restarted.tick()
        assert session(restarted)["status"] == "stopped"
        assert terminal.launches == 1 and not terminal.terminals

    def test_crash_after_create_does_not_duplicate(self, area) -> None:
        """Reconcile the committed deterministic intent after create-before-commit death."""
        terminal = FakeTerminal()
        terminal.crash = True
        with area.journal.writer():
            client = SessionService(area)
            client.request("launch", {"participant": "agent"})
            with pytest.raises(KeyboardInterrupt):
                SessionEngine(area, terminal).tick()
            restarted = SessionEngine(area, terminal)
            restarted.tick()
            assert terminal.launches == 1
            assert session(restarted)["status"] == "starting"

    def test_readiness_timeout(self, area) -> None:
        """Unacknowledged bootstrap or trust prompts become visible blocked sessions."""
        terminal = FakeTerminal()
        clock = [100.0]
        with area.journal.writer():
            engine = SessionEngine(area, terminal, clock=lambda: clock[0])
            SessionService(area).request("launch", {"participant": "agent"})
            engine.tick()
            clock[0] += 181
            engine.tick()
            assert session(engine)["status"] == "blocked"
            assert "readiness timeout" in session(engine)["error"]

    def test_supervisor_restart_keeps_provider(self, managed) -> None:
        """Reopening a UI or supervisor never constitutes a new launch request."""
        area, engine, terminal, client = managed
        restarted = SessionEngine(area, terminal)
        restarted.tick()
        assert terminal.launches == 1
        assert session(restarted)["status"] == "ready"


class TestPermissionBoundaries:
    """Interactive approval dialogs must never receive automated terminal input."""

    def test_manual_profile_only_publishes(self, managed) -> None:
        """Manual profiles keep durable prompts available without pressing Enter."""
        area, engine, terminal, client = managed
        current = session(engine)
        current["profile"]["input_mode"] = "manual"
        engine.commit("test-manual-profile")
        client.request("prompt", {"session_id": current["id"], "generation": 1, "text": "Test"})
        engine.tick()
        assert session(engine)["work"]["status"] == "pending"
        assert not terminal.wakes
        assert len(area.room.read_inbox("agent")) == 2
