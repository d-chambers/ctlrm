"""Small tmux command objects and command runner implementations."""

from __future__ import annotations

import libtmux

from ctlrm.communication.terminal import _translate_errors
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TmuxTargetRef:
    """Reference to a tmux pane."""

    session: str
    window: str
    pane: str

    @property
    def selector(self) -> str:
        """Return the tmux selector string for this target."""
        if self.pane.startswith("%"):
            return self.pane
        return f"{self.session}:{self.window}.{self.pane}"


@dataclass(frozen=True)
class TmuxCommand:
    """A tmux command with executable and argument list."""

    program: str
    args: list[str]

    @classmethod
    def send_keys(cls, target: TmuxTargetRef, text: str) -> TmuxCommand:
        """Build a command that sends text and Enter to a pane."""
        return cls("tmux", ["send-keys", "-t", target.selector, text, "Enter"])

    @classmethod
    def kill_pane(cls, target: TmuxTargetRef) -> TmuxCommand:
        """Stop exactly the pane created by a failed launch."""
        return cls("tmux", ["kill-pane", "-t", target.selector])

    @classmethod
    def capture_pane(cls, target: TmuxTargetRef) -> TmuxCommand:
        """Build a command that captures pane text."""
        return cls("tmux", ["capture-pane", "-p", "-t", target.selector])

    @classmethod
    def new_session(cls, session: str, workdir: str) -> TmuxCommand:
        """Build a command that creates or attaches a detached session."""
        return cls("tmux", ["new-session", "-d", "-A", "-s", session, "-c", workdir])

    @classmethod
    def split_window(cls, session: str, workdir: str, command: list[str]) -> TmuxCommand:
        """Build a command that opens a pane and prints its pane id."""
        return cls(
            "tmux",
            [
                "split-window",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                session,
                "-c",
                workdir,
                *command,
            ],
        )


class CommandRunner(Protocol):
    """Protocol for executing tmux commands."""

    def run(self, command: TmuxCommand) -> str:
        """Execute a tmux command and return stdout."""
        ...


class SubprocessCommandRunner:
    """Execute the prototype GUI terminal commands through libtmux."""

    @_translate_errors
    def run(self, command: TmuxCommand) -> str:
        """Execute a tmux command and return stdout."""
        if command.program != "tmux":
            raise ValueError("the terminal runner accepts only tmux operations")
        result = libtmux.Server().cmd(*command.args)
        if result.returncode:
            raise RuntimeError("; ".join(result.stderr))
        return "\n".join(result.stdout)
