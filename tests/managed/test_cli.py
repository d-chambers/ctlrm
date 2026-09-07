"""Managed CLI contracts used verbatim by agent bootstrap instructions."""

import pytest
from typer.testing import CliRunner

from ctlrm.__main__ import app
from ctlrm.managed.area import Area
from ctlrm.managed.sessions import SessionService


class TestAgentCommands:
    """Bootstrap flags must reach the same service operations tested in the engine."""

    def test_ready_flags(self, area, monkeypatch) -> None:
        """Required values are named options, matching the generated bootstrap command."""
        calls = []

        def ready(self, session_id, generation, native_id):
            """Record the parsed command without requiring an external provider."""
            calls.append((session_id, generation, native_id))
            return "request-test"

        monkeypatch.setattr(SessionService, "ready", ready)
        monkeypatch.setattr(SessionService, "await_result", lambda *args: {"status": "accepted"})
        result = CliRunner().invoke(
            app,
            [
                "--root",
                str(area.root),
                "session",
                "ready",
                "--session-id",
                "session-test",
                "--generation",
                "2",
                "--native-id",
                "native-test",
            ],
        )
        assert result.exit_code == 0, result.output
        assert calls == [("session-test", 2, "native-test")]
        assert "accepted request-test" in result.output

    def test_status_never_launches(self, area) -> None:
        """Inspection of an initialized area creates no sessions or synthetic tasks."""
        result = CliRunner().invoke(app, ["--root", str(area.root), "session", "status"])
        assert result.exit_code == 0, result.output
        assert '"sessions":{}' in result.output
        assert area.journal.replay()[1] == 0


class TestStandaloneStart:
    """Starting a standalone session validates inputs before preserving one durable launch."""

    def test_retry_custom_profile(self, repository, profile, monkeypatch) -> None:
        """Retrying the CLI with the same request ID preserves the area and launch intent."""
        path = repository / "profile.json"
        path.write_text(profile.model_dump_json())
        starts = []
        monkeypatch.setattr(
            "ctlrm.managed.cli.supervisor.start", lambda area: starts.append(area.data["id"])
        )
        args = [
            "--root",
            str(repository),
            "session",
            "start",
            "--profile",
            str(path),
            "--participant",
            "writer",
            "--role",
            "implement",
            "--instructions",
            "Keep this goal",
            "--request-id",
            "start-one",
        ]
        runner = CliRunner()
        for _ in range(2):
            result = runner.invoke(app, args)
            assert result.exit_code == 0, result.output
            assert result.stdout.strip() == "start-one"
        area = Area.load(repository)
        assert starts == [area.data["id"], area.data["id"]]
        assert area.data["mode"] == "standalone" and area.data["definition"] is None
        assert area.data["prompt"] == "Keep this goal"
        assert area.data["roles"]["writer"]["role"] == "implement"
        assert area.data["profiles"]["agent"] == profile.checked().model_dump()
        state, sequence, _ = area.journal.replay()
        pending, errors = area.journal.pending(state)
        assert sequence == 0 and state["sessions"] == {} and not errors
        assert len(pending) == 1
        assert pending[0]["id"] == "start-one"
        assert pending[0]["kind"] == "launch"
        assert pending[0]["payload"] == {"participant": "writer"}

    @pytest.mark.parametrize("failure", ["reserved-role", "invalid-profile", "missing-executable"])
    def test_invalid_start(self, repository, profile, monkeypatch, failure) -> None:
        """Rejected starts cannot leave immutable area state or launch a supervisor."""
        path = repository / "profile.json"
        path.write_text(profile.model_dump_json())
        participant = "agent"
        if failure == "reserved-role":
            participant = "coordinator"
        elif failure == "invalid-profile":
            path.write_text('{"provider": "command"}')
        else:
            path.write_text(
                profile.model_copy(
                    update={"executable": str(repository / "missing")}
                ).model_dump_json()
            )
        starts = []
        monkeypatch.setattr("ctlrm.managed.cli.supervisor.start", lambda area: starts.append(area))
        result = CliRunner().invoke(
            app,
            [
                "--root",
                str(repository),
                "session",
                "start",
                "--profile",
                str(path),
                "--participant",
                participant,
            ],
        )
        assert result.exit_code == 2, result.output
        assert "invalid:" in result.stderr
        assert not starts
        assert not (repository / ".ctlrm").exists()


class TestPromptCommands:
    """The CLI publishes and acknowledges real work through the existing single-writer engine."""

    @pytest.fixture
    def cli(self, managed, monkeypatch):
        """Drive the real request queue synchronously while retaining the fake provider boundary."""
        area, engine, terminal, client = managed
        monkeypatch.delenv("CTLRM_SESSION", raising=False)
        original = SessionService.await_result

        def await_result(service, request_id, timeout=30):
            """Commit requests with the fixture's writer before observing the durable result."""
            engine.tick()
            return original(service, request_id, timeout=0.2)

        monkeypatch.setattr(SessionService, "await_result", await_result)
        monkeypatch.setattr("ctlrm.managed.cli.supervisor.start", lambda area: engine.tick())
        current = next(iter(engine.state["sessions"].values()))
        prefix = ["--root", str(area.root), "session"]
        owner = ["--session-id", current["id"], "--generation", "1"]
        return CliRunner(), prefix, owner, area, engine, terminal

    @pytest.mark.parametrize("source", ["text", "file"])
    def test_work_lifecycle(self, cli, tmp_path, source) -> None:
        """Multiline prompts remain one work item on retry and complete only after acknowledgment."""
        runner, prefix, owner, area, engine, terminal = cli
        body = "First line\n    indented résumé\nLast line\n"
        path = tmp_path / "prompt.txt"
        path.write_text(body, encoding="utf-8")
        text_args = ["--text", body] if source == "text" else ["--file", str(path)]
        args = [*prefix, "prompt", *owner, *text_args, "--request-id", "prompt-one"]
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        sid = owner[1]
        work = engine.state["sessions"][sid]["work"]
        assert work["status"] == "pending"
        assert work["message"]["body"].partition("\n\n")[2] == body
        assert work["id"] in work["message"]["body"].splitlines()[0]
        delivered = area.room.read_inbox("agent")
        wakes = list(terminal.wakes)
        assert runner.invoke(app, args).exit_code == 0
        assert engine.state["sessions"][sid]["work"] == work
        assert area.room.read_inbox("agent") == delivered and terminal.wakes == wakes
        for command, extra, status in [
            ("acknowledge", [], "acknowledged"),
            ("disposition", ["--status", "completed"], "completed"),
        ]:
            result = runner.invoke(app, [*prefix, command, *owner, "--work-id", work["id"], *extra])
            assert result.exit_code == 0, result.output
            assert engine.state["sessions"][sid]["work"]["status"] == status

    @pytest.mark.parametrize("source", ["neither", "both", "missing-file"])
    def test_invalid_source(self, cli, tmp_path, source) -> None:
        """Ambiguous or missing prompt content is rejected before a request is published."""
        runner, prefix, owner, area, engine, _ = cli
        path = tmp_path / "missing.txt"
        text_args = {
            "neither": [],
            "both": ["--text", "inline", "--file", str(path)],
            "missing-file": ["--file", str(path)],
        }[source]
        before = area.journal.replay()
        result = runner.invoke(app, [*prefix, "prompt", *owner, *text_args])
        assert result.exit_code == (4 if source == "missing-file" else 2), result.output
        assert ("not found:" if source == "missing-file" else "exactly one") in result.stderr
        assert area.journal.replay() == before
        assert area.journal.pending(engine.state) == ([], [])

    def test_stale_generation(self, cli) -> None:
        """The CLI reports a committed rejection when its observed generation is stale."""
        runner, prefix, owner, area, engine, terminal = cli
        owner[-1] = "2"
        result = runner.invoke(
            app,
            [
                *prefix,
                "prompt",
                *owner,
                "--text",
                "Do not dispatch",
                "--request-id",
                "stale",
            ],
        )
        assert result.exit_code == 2
        assert "stale session generation" in result.stderr
        request = engine.state["requests"]["stale"]
        assert request["status"] == "rejected"
        assert engine.state["sessions"][owner[1]]["work"] is None
        assert terminal.wakes == []
        assert not any(m.body == "Do not dispatch" for m in area.room.read_inbox("agent"))
