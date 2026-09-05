"""Projects group goals; jobs bind a deliverable to a reusable workflow."""

from typing import Literal

from pydantic import Field, field_validator, model_validator

from ctlrm.runtime.paths import validate_path_component
from ctlrm.runtime.workflows import JobInput, Record, WorkflowTemplate


class Project(Record):
    """A durable improvement initiative for one codebase, independent of worktrees."""

    schema_version: Literal[1] = 1
    id: str
    name: str = Field(min_length=1, max_length=256)
    codebase: str
    git_common_dir: str
    goals: list[str] = Field(min_length=1, max_length=64)
    design: str = Field(default="", max_length=262144)

    @field_validator("id")
    @classmethod
    def safe_id(cls, value: str) -> str:
        """Validate an identifier used in the central directory."""
        return validate_path_component(value, label="project id", max_length=64)

    @model_validator(mode="after")
    def text(self) -> "Project":
        """Reject blank goals and names."""
        if not self.name.strip() or any(not g.strip() or len(g) > 16384 for g in self.goals):
            raise ValueError("project name and goals must be nonblank and bounded")
        return self


class Job(JobInput):
    """An immutable planned deliverable, usually a PR, with fixed workflow inputs."""

    schema_version: Literal[1] = 1
    project_id: str
    acceptance: list[str] = Field(default_factory=list, max_length=64)
    workflow: WorkflowTemplate
    depends_on: list[str] = Field(default_factory=list, max_length=128)
    base: str = "HEAD"

    @field_validator("id", "project_id")
    @classmethod
    def safe_id(cls, value: str) -> str:
        """Keep project and job directory names safe."""
        return validate_path_component(value, label="job/project id", max_length=64)

    @model_validator(mode="after")
    def dependencies(self) -> "Job":
        """Reject self/duplicate dependencies and blank acceptance criteria."""
        for dependency in self.depends_on:
            validate_path_component(dependency, label="dependency id", max_length=64)
        if self.id in self.depends_on or len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("job dependencies must be distinct and cannot include itself")
        if any(not item.strip() or len(item) > 16384 for item in self.acceptance):
            raise ValueError("acceptance criteria must be nonblank and bounded")
        if not self.base or self.base.startswith("-") or len(self.base) > 256:
            raise ValueError("base must be a bounded Git revision")
        return self


class PullRequest(Record):
    """A repository-qualified PR identity attached independently of job execution."""

    number: int = Field(ge=1, strict=True)
    repository: str = Field(min_length=3, max_length=512)
    url: str | None = Field(default=None, max_length=2048)

    @field_validator("repository")
    @classmethod
    def repository_identity(cls, value: str) -> str:
        """Require an explicit owner/repository or host/owner/repository identity."""
        parts = value.strip().split("/")
        if len(parts) not in {2, 3}:
            raise ValueError("repository must be owner/repo or host/owner/repo")
        for part in parts:
            validate_path_component(part, label="repository component", max_length=128)
        return "/".join(parts).casefold()

    @field_validator("url")
    @classmethod
    def web_url(cls, value: str | None) -> str | None:
        """Keep optional PR links usable without interpreting arbitrary schemes."""
        from urllib.parse import urlsplit

        if value is not None:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
            ):
                raise ValueError("PR URL must be an HTTP(S) URL without credentials")
        return value
