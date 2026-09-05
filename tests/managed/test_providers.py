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
                provider="claude",
                executable=sys.executable,
                context=str(tmp_path),
                arguments=["--permission-mode", "dontAsk", "--permission-mode", "default"],
                input_mode="unattended",
            )


class TestPermissionEqualsForm:
    """Valid native CLI inline values are parsed consistently with split values."""

    @pytest.mark.parametrize(
        "provider,argument",
        [("claude", "--permission-mode=dontAsk")],
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


class TestCodexOwnership:
    """Managed sessions cannot mistake a shared daemon's TUI for its provider process."""

    @pytest.mark.parametrize("mode", ["manual", "unattended"])
    def test_shared_daemon_transport_rejected(self, tmp_path, mode) -> None:
        """Codex ownership requires the terminal-owned native turn transport."""
        with pytest.raises(ValueError, match="requires input_mode native"):
            ProviderProfile(
                provider="codex", executable=sys.executable, context=str(tmp_path), input_mode=mode
            )


class TestCodexExecArguments:
    """Obsolete interactive permission flags fail before immutable profile publication."""

    @pytest.mark.parametrize("argument", ["--ask-for-approval=never", "-a"])
    def test_interactive_approval_flag(self, tmp_path, argument) -> None:
        """Use Codex exec's configuration interface without silently changing permission policy."""
        with pytest.raises(ValueError, match="does not accept"):
            ProviderProfile(
                provider="codex",
                executable="codex",
                context=str(tmp_path),
                input_mode="native",
                arguments=[argument],
            )
