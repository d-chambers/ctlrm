from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TmuxTargetRef:
    session: str
    window: str
    pane: str

    @property
    def selector(self) -> str:
        return f"{self.session}:{self.window}.{self.pane}"


@dataclass(frozen=True)
class TmuxCommand:
    program: str
    args: list[str]

    @classmethod
    def send_keys(cls, target: TmuxTargetRef, text: str) -> TmuxCommand:
        return cls("tmux", ["send-keys", "-t", target.selector, text, "Enter"])

    @classmethod
    def capture_pane(cls, target: TmuxTargetRef) -> TmuxCommand:
        return cls("tmux", ["capture-pane", "-p", "-t", target.selector])


class CommandRunner(Protocol):
    def run(self, command: TmuxCommand) -> str: ...


class SubprocessCommandRunner:
    def run(self, command: TmuxCommand) -> str:
        result = subprocess.run(
            [command.program, *command.args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout
