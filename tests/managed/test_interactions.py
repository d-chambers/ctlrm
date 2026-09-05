"""Conversation input is serialized and cannot silently replay interrupted effects."""

import copy

import pytest

from ctlrm.managed.codex_host import InputLoop


class TestNativeInteractions:
    """Exercise durable acceptance, ownership, completion, and recovery boundaries."""

    @pytest.fixture
    def native(self, managed, monkeypatch):
        """Use the existing supervisor fixture with native transport and synchronous ticks."""
        area, engine, terminal, client = managed
        session = next(iter(engine.state["sessions"].values()))
        session["profile"].update(provider="codex", input_mode="native")
        engine.commit("test-native")
        loop = InputLoop(
            area.root,
            session["id"],
            1,
            session["spec"]["token"],
            {"executable": "codex", "arguments": []},
            session["native_id"],
        )
        original = loop.service.await_result

        def accepted(request):
            """Drive the single writer before the runner observes acceptance."""
            engine.tick()
            return original(request, timeout=0.1)

        monkeypatch.setattr(loop.service, "await_result", accepted)
        return engine, client, session["id"], loop

    def test_delivery_preserves_work(self, native, monkeypatch) -> None:
        """An explicit user message resumes the same thread without replacing task ownership."""
        engine, client, session_id, loop = native
        session = engine.state["sessions"][session_id]
        before = copy.deepcopy(session["work"])
        attempts = []

        def turn(executable, arguments, native_id, prompt):
            """Verify durable claim precedes the external side effect."""
            current = engine.journal.replay()[0]["sessions"][session_id]
            assert current["interactions"][0]["status"] == "running"
            attempts.append((native_id, prompt))
            return native_id

        monkeypatch.setattr("ctlrm.managed.codex_host.turn", turn)
        payload = {"session_id": session_id, "generation": 1, "text": "Explain the current change"}
        client.request("interact", payload, "same-message")
        client.request("interact", payload, "same-message")
        engine.tick()
        assert loop.poll()
        assert not loop.poll()
        current = engine.journal.replay()[0]["sessions"][session_id]
        assert current["work"] == before
        assert current["interactions"][0]["status"] == "completed"
        assert attempts == [(loop.native_id, payload["text"])]

    def test_failed_turn_not_replayed(self, native, monkeypatch) -> None:
        """A provider error may follow side effects and therefore remains uncertain."""
        engine, client, session_id, loop = native

        def fail(*args):
            """Model an interrupted turn after unknown provider effects."""
            raise RuntimeError("provider disconnected")

        monkeypatch.setattr("ctlrm.managed.codex_host.turn", fail)
        client.request("interact", {"session_id": session_id, "generation": 1, "text": "Hello"})
        engine.tick()
        assert loop.poll()
        assert not loop.poll()
        current = engine.journal.replay()[0]["sessions"][session_id]
        assert current["interactions"][0]["status"] == "uncertain"

    def test_stop_retains_uncertainty(self, native) -> None:
        """Stopping a claimed message preserves evidence and rejects a late completion."""
        engine, client, session_id, loop = native
        payload = {"session_id": session_id, "generation": 1}
        client.request("interact", {**payload, "text": "Change something"})
        engine.tick()
        session = engine.state["sessions"][session_id]
        item = session["interactions"][0]
        owner = {**payload, "interaction_id": item["id"], "token": loop.token}
        client.request("interaction-start", owner)
        engine.tick()
        client.request("stop", payload)
        engine.tick()
        request = client.request("interaction-finish", {**owner, "status": "completed"})
        engine.tick()
        assert engine.state["requests"][request]["status"] == "rejected"
        assert engine.state["sessions"][session_id]["interactions"][0]["status"] == "uncertain"

    @pytest.mark.parametrize("generation,text", [(0, "hello"), (1, " "), (1, "x" * 16385)])
    def test_invalid_input(self, native, generation, text) -> None:
        """Old views and invalid messages cannot enqueue work."""
        engine, client, session_id, loop = native
        request = client.request(
            "interact", {"session_id": session_id, "generation": generation, "text": text}
        )
        engine.tick()
        assert engine.state["requests"][request]["status"] == "rejected"
        assert not loop.poll()

    def test_claim_requires_owner(self, native) -> None:
        """A caller cannot claim a turn using an obsolete runner token."""
        engine, client, session_id, loop = native
        client.request("interact", {"session_id": session_id, "generation": 1, "text": "Hello"})
        engine.tick()
        item = engine.state["sessions"][session_id]["interactions"][0]
        request = client.request(
            "interaction-start",
            {
                "session_id": session_id,
                "generation": 1,
                "interaction_id": item["id"],
                "token": "old",
            },
        )
        engine.tick()
        assert engine.state["requests"][request]["status"] == "rejected"
        assert engine.state["sessions"][session_id]["interactions"][0]["status"] == "queued"

    def test_replace_cancels_queued(self, native, monkeypatch) -> None:
        """A queued message for the old native conversation cannot leak into its replacement."""
        engine, client, session_id, loop = native
        session = engine.state["sessions"][session_id]
        monkeypatch.setenv("CODEX_HOME", session["profile"]["context"])
        client.request(
            "interact", {"session_id": session_id, "generation": 1, "text": "Old context"}
        )
        engine.tick()
        client.request("stop", {"session_id": session_id})
        engine.tick()
        request = client.request("replace", {"session_id": session_id, "generation": 1})
        engine.tick()
        assert engine.state["requests"][request]["status"] == "accepted"
        current = engine.state["sessions"][session_id]
        assert current["generation"] == 2
        assert current["interactions"][0]["status"] == "canceled"
        with pytest.raises(ValueError, match="generation"):
            loop.poll()

    def test_queue_limit(self, native) -> None:
        """A busy conversation has a bounded pending queue and rejects excess input visibly."""
        engine, client, session_id, loop = native
        for index in range(9):
            request = client.request(
                "interact", {"session_id": session_id, "generation": 1, "text": f"Message {index}"}
            )
        engine.tick()
        assert engine.state["requests"][request]["status"] == "rejected"
        assert len(engine.state["sessions"][session_id]["interactions"]) == 8

    def test_pending_task_precedes_message(self, native) -> None:
        """A queued conversation cannot claim a turn ahead of an undelivered assignment."""
        engine, client, session_id, loop = native
        payload = {"session_id": session_id, "generation": 1}
        client.request("prompt", {**payload, "text": "Assigned work"})
        client.request("interact", {**payload, "text": "Additional context"})
        engine.tick()
        item = engine.state["sessions"][session_id]["interactions"][0]
        request = client.request(
            "interaction-start", {**payload, "interaction_id": item["id"], "token": loop.token}
        )
        engine.tick()
        assert engine.state["requests"][request]["status"] == "rejected"
        assert engine.state["sessions"][session_id]["interactions"][0]["status"] == "queued"
