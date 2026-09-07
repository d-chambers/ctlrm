"""Provider hosting verifies committed launch intent and retains real process exit evidence."""

import json
from pathlib import Path
import sys

import pytest

from ctlrm.managed import host
from ctlrm.managed.storage import publish, read_record


@pytest.fixture
def launch(area, monkeypatch):
    """Record one disposable provider command without a supervisor or model process."""
    monkeypatch.setenv("CTLRM_LAUNCH_TOKEN", "owned-test-launch")
    directory = area.runtime / "sessions" / "session-test"

    def prepare(code: str, *, argv: list[str] | None = None) -> Path:
        """Publish the same immutable launch contract consumed by the real host."""
        publish(
            directory / "launch-1.json",
            {
                "token": "owned-test-launch",
                "argv": argv or [sys.executable, "-c", code],
            },
        )
        return directory

    return prepare


class TestProviderRun:
    """The testable provider boundary preserves environment, working directory, and exit codes."""

    @pytest.mark.parametrize("returncode", [0, 7])
    def test_exit_evidence(self, area, launch, returncode) -> None:
        """A real child receives only the recorded session context and its exit is durable."""
        directory = launch(
            "import json,os; from pathlib import Path; "
            "Path('observed.json').write_text(json.dumps({"
            "'cwd':os.getcwd(), 'root':os.environ['CTLRM_ROOT'], "
            "'session':os.environ['CTLRM_SESSION'], 'generation':os.environ['CTLRM_GENERATION']})); "
            f"raise SystemExit({returncode})"
        )
        before = Path.cwd()
        assert host.run_provider(area.root, "session-test", 1) == returncode
        assert Path.cwd() == before
        assert json.loads((area.root / "observed.json").read_text()) == {
            "cwd": str(area.root),
            "root": str(area.root),
            "session": "session-test",
            "generation": "1",
        }
        assert read_record(directory / "exit-1.json") == {
            "token": "owned-test-launch",
            "returncode": returncode,
        }

    @pytest.mark.parametrize("token", [None, "", "wrong"])
    def test_wrong_token(self, area, launch, monkeypatch, token) -> None:
        """Missing or incorrect authority cannot run a child or publish an exit record."""
        directory = launch("from pathlib import Path; Path('unexpected').touch()")
        if token is None:
            monkeypatch.delenv("CTLRM_LAUNCH_TOKEN")
        else:
            monkeypatch.setenv("CTLRM_LAUNCH_TOKEN", token)
        with pytest.raises(ValueError, match="launch token"):
            host.run_provider(area.root, "session-test", 1)
        assert not (area.root / "unexpected").exists()
        assert not (directory / "exit-1.json").exists()

    def test_spawn_error(self, area, launch) -> None:
        """An executable error cannot be mislabeled as a successful provider exit."""
        directory = launch("", argv=[str(area.root / "missing-executable")])
        with pytest.raises(FileNotFoundError):
            host.run_provider(area.root, "session-test", 1)
        assert not (directory / "exit-1.json").exists()

    @pytest.mark.parametrize(
        "session_id,generation",
        [("../other", 1), ("session-test", 0), ("session-test", -1), ("session-test", True)],
    )
    def test_invalid_identity(self, area, session_id, generation) -> None:
        """Malformed command-line identity is rejected before any launch-file lookup."""
        with pytest.raises(ValueError):
            host.run_provider(area.root, session_id, generation)


class TestInspectionHost:
    """The entry point retains an inspectable terminal after the child has exited."""

    def test_entrypoint(self, area, launch, monkeypatch, capsys) -> None:
        """Entry-point parsing calls the real provider boundary before entering the wait loop."""
        directory = launch("raise SystemExit(7)")
        waited = []

        def inspect():
            """Replace only the indefinite inspection wait."""
            waited.append(True)

        monkeypatch.chdir(area.root)
        monkeypatch.setattr(sys, "argv", ["host", str(area.root), "session-test", "1"])
        monkeypatch.setattr(host, "wait_for_inspection", inspect)
        host.main()
        assert read_record(directory / "exit-1.json")["returncode"] == 7
        assert waited == [True]
        assert "status 7; host remains for inspection" in capsys.readouterr().out

    def test_wait_interval(self, monkeypatch) -> None:
        """Inspection waits at a bounded frequency without relaunching the provider."""
        intervals = []

        def sleep(seconds):
            """End the otherwise indefinite loop at its first real waiting boundary."""
            intervals.append(seconds)
            raise InterruptedError("end inspection")

        monkeypatch.setattr(host.time, "sleep", sleep)
        with pytest.raises(InterruptedError, match="end inspection"):
            host.wait_for_inspection()
        assert intervals == [60]
