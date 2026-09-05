"""Managed CLI contracts used verbatim by agent bootstrap instructions."""

from typer.testing import CliRunner

from ctlrm.__main__ import app
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
