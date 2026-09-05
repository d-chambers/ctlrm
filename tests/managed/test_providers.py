"""Provider profile authority and native recovery validation."""

from pathlib import Path
import sys

import pytest

from ctlrm.managed.providers import ProviderProfile


class TestProviderProfiles:
    """An agent cannot override configured executable, arguments, or native identity."""

    def test_missing_environment(self, tmp_path: Path, monkeypatch) -> None:
        """Missing runtime values are named without serializing credential values."""
        monkeypatch.delenv("CTLRM_TEST_REQUIRED", raising=False)
        profile = ProviderProfile(
            provider="claude",
            executable=sys.executable,
            context=str(tmp_path),
            required_environment=["CTLRM_TEST_REQUIRED"],
        )
        with pytest.raises(ValueError, match="CTLRM_TEST_REQUIRED"):
            profile.checked()

    def test_missing_executable(self, tmp_path: Path) -> None:
        """Every profile is validated before launch intent is accepted."""
        profile = ProviderProfile(
            provider="claude", executable="ctlrm-no-such-executable", context=str(tmp_path)
        )
        with pytest.raises(ValueError, match="executable is missing"):
            profile.checked()

    @pytest.mark.parametrize("flag", ["--resume", "--session-id=other", "--ephemeral"])
    def test_reserved_identity_flags(self, tmp_path: Path, flag: str) -> None:
        """Configured extra arguments cannot silently replace controller-owned identity."""
        with pytest.raises(ValueError, match="identity or persistence"):
            ProviderProfile(
                provider="claude",
                executable=sys.executable,
                context=str(tmp_path),
                arguments=[flag],
            )

    def test_resume_keeps_profile_flags(self, tmp_path: Path) -> None:
        """Minimal native ID acknowledgment retains the authoritative launch configuration."""
        profile = ProviderProfile(
            provider="claude",
            executable=sys.executable,
            context=str(tmp_path),
            arguments=["--permission-mode", "plan"],
        )
        native = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        assert profile.command(native, "mailbox", resume=True) == [
            sys.executable,
            "--permission-mode",
            "plan",
            "--resume",
            native,
            "mailbox",
        ]


class TestAutomaticInputPolicy:
    """Automatic terminal delivery requires a single non-interactive approval policy."""

    def test_interactive_policy_rejected(self, tmp_path: Path) -> None:
        """Profiles with native approval dialogs cannot receive automatic Enter keys."""
        with pytest.raises(ValueError, match="dontAsk"):
            ProviderProfile(
                provider="claude",
                executable=sys.executable,
                context=str(tmp_path),
                arguments=["--permission-mode", "acceptEdits"],
                input_mode="unattended",
            )

    def test_conflicting_alias_rejected(self, tmp_path: Path) -> None:
        """A later short option must not undo a policy validated through a long option."""
        with pytest.raises(ValueError, match="exactly one"):
            ProviderProfile(
                provider="codex",
                executable=sys.executable,
                context=str(tmp_path),
                arguments=["--ask-for-approval", "never", "-a", "on-request"],
                input_mode="unattended",
            )


class TestPermissionEqualsForm:
    """Valid native CLI inline values are parsed consistently with split values."""

    @pytest.mark.parametrize(
        "provider,argument",
        [("claude", "--permission-mode=dontAsk"), ("codex", "--ask-for-approval=never")],
    )
    def test_equals_form(self, tmp_path: Path, provider: str, argument: str) -> None:
        """The policy guard accepts native argument syntax without weakening its checks."""
        profile = ProviderProfile(
            provider=provider,
            executable=sys.executable,
            context=str(tmp_path),
            arguments=[argument],
            input_mode="unattended",
        )
        assert profile.input_mode == "unattended"
