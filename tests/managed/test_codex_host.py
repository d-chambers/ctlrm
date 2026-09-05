"""Codex native turn transport validates thread identity without a shared daemon."""

import json
from pathlib import Path
import sys

import pytest

from ctlrm.managed.codex_host import turn
from ctlrm.managed.providers import ProviderProfile


class TestCodexTurns:
    """The provider, rather than the controller, reports the native thread identity."""

    @pytest.fixture
    def executable(self, tmp_path: Path) -> str:
        """Create a deterministic JSONL-speaking provider executable."""
        path = tmp_path / "codex"
        path.write_text(
            f"#!{sys.executable}\nimport json,sys\nprint(json.dumps({{'type':'thread.started','thread_id':'00000000-0000-4000-8000-000000000001'}}))\n"
        )
        path.chmod(0o755)
        return str(path)

    def test_same_native_id(self, executable) -> None:
        """Every resumed turn must retain the actual initially observed thread UUID."""
        native = turn(executable, [], None, "bootstrap")
        assert turn(executable, [], native, "mailbox") == native
        with pytest.raises(ValueError, match="different native thread"):
            turn(executable, [], "00000000-0000-4000-8000-000000000002", "mailbox")

    def test_profile_keeps_permissions(self, tmp_path, executable) -> None:
        """Native delivery adds transport arguments without changing user permission policy."""
        profile = ProviderProfile(
            provider="codex", executable=executable, context=str(tmp_path), input_mode="native"
        )
        argv = profile.command(None, "bootstrap", resume=False)
        assert argv[1:3] == ["-m", "ctlrm.managed.codex_host"]
        assert json.loads(argv[-1])["arguments"] == []
        assert json.loads(argv[-1])["native_id"] is None


class TestRetryPendingInput:
    """A native turn ending without accepted acknowledgment cannot consume its assignment."""

    def test_rejected_ack_retries(self, managed, monkeypatch) -> None:
        """Pause and reconcile retain the same retryable hint until the work is acknowledged."""
        import ctlrm.managed.codex_host as host

        area, engine, terminal, client = managed
        current = next(iter(engine.state["sessions"].values()))
        current["profile"].update(provider="codex", input_mode="native")
        engine.commit("test-native-transport")
        client.request("prompt", {"session_id": current["id"], "generation": 1, "text": "Work"})
        engine.tick()
        attempts = []

        def turn(executable, arguments, native_id, prompt):
            """Model a completed native turn whose acknowledgment was rejected."""
            attempts.append(prompt)
            return native_id

        monkeypatch.setattr(host, "turn", turn)
        clock = [100.0]
        loop = host.InputLoop(
            area.root,
            current["id"],
            1,
            current["spec"]["token"],
            {"executable": "codex", "arguments": []},
            current["native_id"],
            clock=lambda: clock[0],
        )
        assert loop.poll()
        engine.state["paused"] = "branch drift"
        engine.commit("test-pause")
        clock[0] += 16
        assert not loop.poll()
        engine.state["paused"] = None
        engine.commit("test-reconcile")
        assert loop.poll()
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        current = engine.state["sessions"][current["id"]]
        client.request(
            "acknowledge",
            {"session_id": current["id"], "generation": 1, "work_id": current["work"]["id"]},
        )
        engine.tick()
        clock[0] += 16
        assert not loop.poll()
