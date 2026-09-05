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


class TestStopCompletionRace:
    """The first committed stop prevents later reports from reviving a session."""

    def test_stop_wins_over_late_completion(self, managed) -> None:
        """Completion arriving in the same poll cannot undo accepted stop intent."""
        area, engine, terminal, client = managed
        current = session(engine)
        client.request("prompt", {"session_id": current["id"], "generation": 1, "text": "Work"})
        engine.tick()
        work_id = session(engine)["work"]["id"]
        client.request(
            "acknowledge", {"session_id": current["id"], "generation": 1, "work_id": work_id}
        )
        engine.tick()
        client.request("stop", {"session_id": current["id"]})
        late = client.request(
            "disposition",
            {
                "session_id": current["id"],
                "generation": 1,
                "work_id": work_id,
                "status": "completed",
            },
        )
        engine.tick()
        assert engine.state["requests"][late]["status"] == "rejected"
        assert session(engine)["status"] == "stopped"
        assert not terminal.terminals


class TestRecoveryBoundaries:
    """Recovery authorization belongs to the native conversation that acknowledged it."""

    def test_death_during_wake_retries(self, managed, monkeypatch) -> None:
        """A provider can die after health inspection but before terminal delivery."""
        area, engine, terminal, client = managed
        current = session(engine)

        def fail_wake(spec, identity, reference):
            """Inject death precisely at the terminal delivery boundary."""
            terminal.terminals[spec["terminal_name"]]["health"] = "exited"
            raise ValueError("provider is not running")

        monkeypatch.setattr(terminal, "wake", fail_wake)
        client.request("prompt", {"session_id": current["id"], "generation": 1, "text": "Work"})
        engine.tick()
        assert session(engine)["status"] == "backoff"
        assert session(engine)["work"]["status"] == "pending"

    def test_replacement_requires_own_recovery(self, managed) -> None:
        """A fresh conversation cannot inherit the old conversation's readiness proof."""
        area, engine, terminal, client = managed
        current = session(engine)
        original_native = current["native_id"]
        client.request("stop", {"session_id": current["id"]})
        engine.tick()
        client.request("replace", {"session_id": current["id"]})
        engine.tick()
        replacement = session(engine)
        assert replacement["generation"] == 2
        assert replacement["native_id"] != original_native
        assert not replacement.get("recovery")
        assert not replacement["ready"]


class TestReadyAfterLaunchCrash:
    """Readiness can arrive while a created terminal still awaits its creation commit."""

    def test_queued_readiness_is_accepted(self, area) -> None:
        """Adopt before accepting the generation acknowledgment, without another spawn."""
        terminal = FakeTerminal()
        terminal.crash = True
        with area.journal.writer():
            client = SessionService(area)
            client.request("launch", {"participant": "agent"})
            with pytest.raises(KeyboardInterrupt):
                SessionEngine(area, terminal).tick()
            current = next(iter(area.journal.replay()[0]["sessions"].values()))
            request = client.ready(current["id"], 1, current["native_id"])
            restarted = SessionEngine(area, terminal)
            restarted.tick()
            assert restarted.state["requests"][request]["status"] == "accepted"
            assert session(restarted)["ready"]
            assert terminal.launches == 1


class TestRetirementAndStartup:
    """Slow process boundaries stay retryable and never violate committed intent."""

    def test_stop_pending_retries(self, managed, monkeypatch) -> None:
        """An accepted stop remains stopping until the signaled host is gone."""
        from ctlrm.communication.terminal import StopPending

        area, engine, terminal, client = managed
        current = session(engine)
        original = terminal.stop
        attempts = []

        def stop(spec, identity):
            """Delay the first stop confirmation only."""
            attempts.append(1)
            if len(attempts) == 1:
                raise StopPending("waiting for process exit")
            original(spec, identity)

        monkeypatch.setattr(terminal, "stop", stop)
        client.request("stop", {"session_id": current["id"]})
        engine.tick()
        assert session(engine)["status"] == "stopping"
        engine.tick()
        assert session(engine)["status"] == "stopped"
        assert session(engine)["error"] is None

    def test_slow_start_is_not_death(self, area) -> None:
        """No child yet is distinct from an observed provider exit."""
        terminal = FakeTerminal()
        clock = [100.0]
        with area.journal.writer():
            engine = SessionEngine(area, terminal, clock=lambda: clock[0])
            SessionService(area).request("launch", {"participant": "agent"})
            engine.tick()
            current = session(engine)
            terminal.terminals[current["spec"]["terminal_name"]]["health"] = "starting"
            clock[0] += 5
            engine.tick()
            assert session(engine)["status"] == "starting"
            assert session(engine)["recoveries"] == 0

    def test_failed_restart_validation_keeps_terminal(self, managed, monkeypatch) -> None:
        """A profile/environment validation failure cannot execute a terminal kill."""
        from ctlrm.managed.providers import ProviderProfile

        area, engine, terminal, client = managed
        current = session(engine)
        terminal.terminals[current["spec"]["terminal_name"]]["health"] = "exited"

        def checked(self):
            """Simulate changed environment at the restart validation boundary."""
            raise ValueError("provider context changed")

        monkeypatch.setattr(ProviderProfile, "checked", checked)
        with pytest.raises(ValueError, match="context changed"):
            engine.apply("replace", {"session_id": current["id"]})
        assert terminal.exists(current["spec"])
        assert session(engine)["generation"] == 1

    def test_success_resets_retry_budget(self, managed) -> None:
        """Acknowledged recovery starts a new consecutive-failure budget."""
        area, engine, terminal, client = managed
        current = session(engine)
        current["recoveries"] = 2
        engine.commit("test-previous-retries")
        client.ready(current["id"], current["generation"], current["native_id"])
        engine.tick()
        assert session(engine)["recoveries"] == 0


class TestPendingRetirement:
    """Queued lifecycle operations cannot abandon the previous owned host."""

    def test_stop_retires_old_generation(self, managed) -> None:
        """Stop between restart intent and effects also cleans up the old terminal."""
        area, engine, terminal, client = managed
        current = session(engine)
        terminal.terminals[current["spec"]["terminal_name"]]["health"] = "exited"
        engine.tick()
        engine.tick()
        assert session(engine)["status"] == "planned"
        client.request("stop", {"session_id": current["id"]})
        engine.tick()
        assert session(engine)["status"] == "stopped"
        assert not terminal.terminals
        assert terminal.launches == 1

    def test_second_restart_preserves_retirement(self, managed) -> None:
        """Two queued replacements cannot overwrite the original host's identity."""
        area, engine, terminal, client = managed
        current = session(engine)
        old_name = current["spec"]["terminal_name"]
        terminal.terminals[old_name]["health"] = "exited"
        client.request("replace", {"session_id": current["id"]})
        second = client.request("replace", {"session_id": current["id"]})
        engine.tick()
        assert engine.state["requests"][second]["status"] == "rejected"
        assert old_name not in terminal.terminals
        assert len(terminal.terminals) == 1
        assert session(engine)["generation"] == 2
