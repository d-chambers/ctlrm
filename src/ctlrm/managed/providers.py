"""Authoritative provider profiles and agent-acknowledged native recovery."""

import os
from pathlib import Path
import re
import shutil
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProviderProfile(BaseModel):
    """An immutable, user-selected executable and recovery policy."""

    model_config = ConfigDict(extra="forbid")
    provider: Literal["claude", "codex", "command"]
    executable: str
    arguments: list[str] = Field(default_factory=list)
    required_environment: list[str] = Field(default_factory=list)
    context: str
    input_mode: Literal["manual", "unattended"] = "manual"
    registration_timeout: float = Field(default=180, ge=1, le=3600)
    recovery_limit: int = Field(default=3, ge=0, le=20)
    backoff: float = Field(default=2, ge=0, le=300)
    launch_arguments: list[str] = Field(default_factory=list)
    resume_arguments: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_flags(self) -> "ProviderProfile":
        """Keep native identity and persistence flags under adapter control."""
        reserved = {
            "--resume",
            "--session-id",
            "--fork-session",
            "--no-session-persistence",
            "--ephemeral",
        }
        if any(value.split("=", 1)[0] in reserved for value in self.arguments):
            raise ValueError(
                "profile arguments cannot override native session identity or persistence"
            )
        if any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in self.required_environment
        ):
            raise ValueError("invalid required environment name")
        if self.input_mode == "unattended" and self.provider != "command":
            names = (
                {"--permission-mode"} if self.provider == "claude" else {"--ask-for-approval", "-a"}
            )
            if sum(value.split("=", 1)[0] in names for value in self.arguments) != 1:
                raise ValueError("automatic input requires exactly one explicit permission policy")
            pairs = {}
            for index, value in enumerate(self.arguments):
                key, separator, inline = value.partition("=")
                if key in names:
                    pairs[key] = (
                        inline
                        if separator
                        else (
                            self.arguments[index + 1] if index + 1 < len(self.arguments) else None
                        )
                    )
            if self.provider == "claude" and pairs.get("--permission-mode") != "dontAsk":
                raise ValueError("automatic Claude input requires --permission-mode dontAsk")
            if (
                self.provider == "codex"
                and pairs.get("--ask-for-approval", pairs.get("-a")) != "never"
            ):
                raise ValueError("automatic Codex input requires --ask-for-approval never")
        if self.provider == "command" and not self.resume_arguments:
            raise ValueError("command profiles require an explicit native resume argument template")
        return self

    def checked(self) -> "ProviderProfile":
        """Resolve the executable and validate current environment/context availability."""
        executable = shutil.which(self.executable)
        if not executable:
            raise ValueError(f"provider executable is missing: {self.executable}")
        missing = [name for name in self.required_environment if not os.environ.get(name)]
        if missing:
            raise ValueError(f"required environment missing: {', '.join(missing)}")
        context = Path(self.context).expanduser().resolve()
        if not context.is_dir():
            raise ValueError(f"provider context directory is missing: {context}")
        if self.provider in {"claude", "codex"}:
            name = "CODEX_HOME" if self.provider == "codex" else "CLAUDE_CONFIG_DIR"
            default = Path.home() / (".codex" if self.provider == "codex" else ".claude")
            active_context = Path(os.environ.get(name, str(default))).expanduser().resolve()
            if active_context != context:
                raise ValueError(
                    f"provider context changed: expected {context}, current {active_context}"
                )
        return self.model_copy(
            update={"executable": str(Path(executable).absolute()), "context": str(context)}
        )

    def native_id(self) -> str | None:
        """Allocate Claude/custom IDs; Codex acknowledges its own native thread ID."""
        return None if self.provider == "codex" else str(uuid4())

    def command(self, native_id: str | None, bootstrap: str, *, resume: bool) -> list[str]:
        """Compose argv exclusively from the profile and validated native identity."""
        if native_id is not None:
            UUID(native_id)
        if resume and not native_id:
            raise ValueError("native recovery requires an acknowledged session ID")
        if self.provider == "claude":
            assert native_id is not None
            return [
                self.executable,
                *self.arguments,
                "--resume" if resume else "--session-id",
                native_id,
                bootstrap,
            ]
        if self.provider == "codex":
            return [
                self.executable,
                *(["resume", native_id] if resume else []),
                *self.arguments,
                bootstrap,
            ]
        values = {"native_id": native_id or "", "bootstrap": bootstrap}
        template = self.resume_arguments if resume else self.launch_arguments
        return [self.executable, *self.arguments, *[arg.format_map(values) for arg in template]]


def builtin(provider: str) -> ProviderProfile:
    """Build a default profile without persisting credential values."""
    if provider not in {"claude", "codex"}:
        raise ValueError("use a profile file for custom providers")
    context = (
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
        if provider == "codex"
        else os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
    )
    return ProviderProfile(provider=provider, executable=provider, context=context).checked()
