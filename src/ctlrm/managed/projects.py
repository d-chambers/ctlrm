"""Central project planning and job provisioning over the existing workflow supervisor."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sys
import time
from typing import Iterator

from ctlrm.managed.area import git, ignore_runtime, worktree
from ctlrm.managed.storage import Journal, identifier, publish, read_record
from ctlrm.managed.workflows import WorkflowService
from ctlrm.runtime.paths import validate_path_component
from ctlrm.runtime.projects import Job, Project
from ctlrm.runtime.workflows import WorkflowTemplate


def data_directory() -> Path:
    """Use per-user persistent application data, with an explicit test/service override."""
    override = os.environ.get("CTLRM_DATA_HOME")
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "darwin":
        path = Path.home() / "Library/Application Support/ctlrm"
    elif os.name == "nt":
        path = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "ctlrm"
    else:
        base = os.environ.get("XDG_DATA_HOME")
        path = (
            Path(base) if base and Path(base).is_absolute() else Path.home() / ".local/share"
        ) / "ctlrm"
    if not path.is_absolute():
        raise ValueError("ctlrm data directory must be absolute")
    return path.resolve()


class ProjectStore:
    """Manage durable project plans while each job retains its own single writer."""

    def __init__(self, directory: Path | None = None) -> None:
        """Select central storage without creating it during read-only inspection."""
        self.root = (directory or data_directory()).resolve()

    def project_path(self, project_id: str) -> Path:
        """Resolve one safe project ID."""
        validate_path_component(project_id, label="project id", max_length=64)
        return self.root / "projects" / project_id

    def job_path(self, project_id: str, job_id: str) -> Path:
        """Resolve one safe job within its project."""
        validate_path_component(job_id, label="job id", max_length=64)
        return self.project_path(project_id) / "jobs" / job_id

    def project(self, project_id: str) -> Project:
        """Read the authoritative immutable project record."""
        return Project.model_validate(read_record(self.project_path(project_id) / "project.json"))

    def job(self, project_id: str, job_id: str) -> Job:
        """Read the immutable job and workflow snapshot."""
        return Job.model_validate(read_record(self.job_path(project_id, job_id) / "job.json"))

    def journal(self, project_id: str) -> Journal:
        """Use the existing crash-safe journal for project lifecycle decisions."""
        return Journal(
            self.project_path(project_id), storage_root=self.project_path(project_id) / "control"
        )

    @contextmanager
    def locked(self, project_id: str) -> Iterator[Journal]:
        """Wait briefly for competing project changes using a project-specific diagnostic."""
        journal = self.journal(project_id)
        deadline = time.monotonic() + 5
        while True:
            lock = journal.writer(purpose="project")
            try:
                lock.__enter__()
                break
            except RuntimeError as error:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"project {project_id} is busy; retry the operation"
                    ) from error
                time.sleep(0.05)
        try:
            yield journal
        finally:
            lock.__exit__(None, None, None)

    @contextmanager
    def editing(self, project_id: str) -> Iterator[tuple[Journal, dict]]:
        """Serialize project changes and forbid mutation after completion/archive."""
        self.project(project_id)
        with self.locked(project_id) as journal:
            state = journal.replay()[0]
            if state.get("project_status", "active") != "active":
                raise ValueError("project is completed or archived")
            yield journal, state

    def create(
        self,
        root: Path,
        name: str,
        goals: list[str],
        *,
        design: str = "",
        project_id: str | None = None,
    ) -> Project:
        """Create an initiative before any job or agent exists; explicit IDs permit retries."""
        root, _ = worktree(root)
        project = Project(
            id=project_id or identifier("project"),
            name=name,
            goals=goals,
            design=design,
            codebase=str(root),
            git_common_dir=git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"),
        )
        publish(self.project_path(project.id) / "project.json", project.model_dump())
        return project

    def list(self) -> list[dict]:
        """List centrally discoverable projects without launching anything."""
        result = []
        for path in sorted((self.root / "projects").glob("*/project.json")):
            try:
                result.append(self.status(path.parent.name))
            except (ValueError, OSError, KeyError, TypeError) as error:
                result.append(
                    {
                        "project": {"id": path.parent.name},
                        "status": "invalid",
                        "error": str(error),
                        "jobs": [],
                    }
                )
        return result

    def add_job(
        self,
        project_id: str,
        title: str,
        instructions: str,
        workflow: WorkflowTemplate,
        *,
        job_id: str | None = None,
        depends_on: list[str] | None = None,
        acceptance: list[str] | None = None,
        base: str = "HEAD",
    ) -> Job:
        """Append a job whose dependencies already exist, making cycles impossible."""
        job = Job(
            id=job_id or identifier("job"),
            project_id=project_id,
            title=title,
            instructions=instructions,
            workflow=workflow,
            depends_on=depends_on or [],
            acceptance=acceptance or [],
            base=base,
        )
        with self.editing(project_id):
            for dependency in job.depends_on:
                self.job(project_id, dependency)
            publish(self.job_path(project_id, job.id) / "job.json", job.model_dump())
            publish(
                self.job_path(project_id, job.id) / "project-context.json",
                self.project(project_id).model_dump(),
            )
        return job

    def job_status(self, project_id: str, job_id: str) -> dict:
        """Derive task/session progress from the job journal, including removed worktrees."""
        job = self.job(project_id, job_id)
        path = self.job_path(project_id, job_id)
        launch = read_record(path / "launch.json") if (path / "launch.json").exists() else None
        try:
            state = Journal(path, storage_root=path / "runtime").replay()[0]
        except (ValueError, OSError) as error:
            return {
                "job": job.model_dump(),
                "launch": launch,
                "status": "invalid",
                "error": str(error),
                "pr": self.journal(project_id).replay()[0].get("pull_requests", {}).get(job_id),
                "run": None,
                "sessions": {},
                "paused": str(error),
            }
        run = state.get("run")
        return {
            "job": job.model_dump(),
            "launch": launch,
            "status": run["status"] if run else ("starting" if launch else "planned"),
            "pr": self.journal(project_id)
            .replay()[0]
            .get("pull_requests", {})
            .get(job_id, state.get("pr")),
            "run": run,
            "sessions": state["sessions"],
            "paused": state.get("paused"),
        }

    def status(self, project_id: str) -> dict:
        """Return goals and jobs without conflating job completion with project completion."""
        project = self.project(project_id)
        state = self.journal(project_id).replay()[0]
        jobs = []
        for path in sorted((self.project_path(project_id) / "jobs").glob("*/job.json")):
            try:
                jobs.append(self.job_status(project_id, path.parent.name))
            except (ValueError, OSError, KeyError, TypeError) as error:
                jobs.append(
                    {
                        "job": {"id": path.parent.name},
                        "status": "invalid",
                        "error": str(error),
                        "pr": None,
                    }
                )
        return {
            "project": project.model_dump(),
            "status": state.get("project_status", "active"),
            "jobs": jobs,
        }

    def start_job(
        self, project_id: str, job_id: str, *, root: Path | None = None, branch: str | None = None
    ) -> tuple[WorkflowService, str]:
        """Provision or bind one worktree and submit the immutable job idempotently."""
        with self.editing(project_id):
            project, job = self.project(project_id), self.job(project_id, job_id)
            path = self.job_path(project_id, job_id)
            for dependency in job.depends_on:
                if self.job_status(project_id, dependency)["status"] != "completed":
                    raise ValueError(f"job dependency is not completed: {dependency}")
            for profile in job.workflow.profiles.values():
                profile.checked()
            launch_path = path / "launch.json"
            if launch_path.exists():
                launch = read_record(launch_path)
                if (root and str(root.resolve()) != launch["root"]) or (
                    branch and branch != launch["branch"]
                ):
                    raise ValueError("job is already bound to another worktree/branch")
            else:
                target = root.resolve() if root else (self.root / "worktrees" / project_id / job_id)
                name = branch or f"ctlrm/{project_id}/{job_id}"
                base_commit = git(
                    Path(project.codebase), "rev-parse", "--verify", f"{job.base}^{{commit}}"
                )
                launch = {
                    "root": str(target),
                    "branch": name,
                    "base_commit": base_commit,
                    "create": root is None,
                }
                if root:
                    resolved, ref = worktree(root)
                    if (
                        git(resolved, "rev-parse", "--path-format=absolute", "--git-common-dir")
                        != project.git_common_dir
                    ):
                        raise ValueError("job worktree belongs to another codebase")
                    if branch and ref != f"refs/heads/{branch}":
                        raise ValueError("existing worktree branch does not match")
                    launch["branch"] = ref.removeprefix("refs/heads/")
                    if (resolved / ".ctlrm").exists() and any((resolved / ".ctlrm").iterdir()):
                        raise ValueError("worktree already contains another coordination area")
                elif target.exists():
                    raise ValueError("new job worktree path already exists")
                git(Path(project.codebase), "check-ref-format", "--branch", launch["branch"])
                if launch["create"] and git(
                    Path(project.codebase),
                    "for-each-ref",
                    "--format=%(refname)",
                    f"refs/heads/{launch['branch']}",
                ):
                    raise ValueError(
                        "new job branch already exists; choose another branch or bind its worktree"
                    )
                publish(launch_path, launch)
            target = Path(launch["root"])
            if not target.exists():
                if not launch["create"]:
                    raise ValueError("bound worktree is missing")
                target.parent.mkdir(parents=True, exist_ok=True)
                registered = git(
                    Path(project.codebase), "worktree", "list", "--porcelain"
                ).splitlines()
                if f"worktree {target}" in registered:
                    git(Path(project.codebase), "worktree", "remove", "--force", str(target))
                existing_branch = git(
                    Path(project.codebase),
                    "for-each-ref",
                    "--format=%(refname)",
                    f"refs/heads/{launch['branch']}",
                )
                if existing_branch:
                    if (
                        not (path / "runtime/area.yaml").exists()
                        and git(
                            Path(project.codebase), "rev-parse", f"refs/heads/{launch['branch']}"
                        )
                        != launch["base_commit"]
                    ):
                        raise ValueError(
                            "uninitialized job branch differs from its planned base; inspect it before retrying"
                        )
                    git(Path(project.codebase), "worktree", "add", str(target), launch["branch"])
                else:
                    git(
                        Path(project.codebase),
                        "worktree",
                        "add",
                        "-b",
                        launch["branch"],
                        str(target),
                        launch["base_commit"],
                    )
            resolved, ref = worktree(target)
            if (
                ref != f"refs/heads/{launch['branch']}"
                or git(resolved, "rev-parse", "--path-format=absolute", "--git-common-dir")
                != project.git_common_dir
            ):
                raise ValueError("worktree identity changed")
            self._bind(resolved, path)
            publish(path / "project-context.json", project.model_dump())
            return WorkflowService.submit(
                resolved,
                job.workflow,
                job.title,
                job.execution_instructions(),
                request_id=f"start-{job.id}",
                job_id=job.id,
            )

    def _bind(self, root: Path, job: Path) -> None:
        """Publish a locator and scratch link without moving or replacing user files."""
        local = root / ".ctlrm"
        if local.is_symlink():
            raise ValueError("coordination runtime must not be a symlink")
        if local.exists() and any(p.name != "location.json" for p in local.iterdir()):
            raise ValueError("worktree already contains another coordination area")
        locator = local / "location.json"
        expected = {"schema_version": 1, "worktree": str(root), "job_directory": str(job)}
        if locator.exists() and read_record(locator) != expected:
            raise ValueError("worktree is already bound to another job")
        scratch = root / ".scratch"
        if scratch.is_symlink():
            raise ValueError("scratch directory must not be a symlink")
        scratch.mkdir(exist_ok=True)
        link = scratch / "ctlrm"
        if link.is_symlink():
            if link.resolve() != job:
                raise ValueError("scratch link belongs to another job")
        elif link.exists():
            raise ValueError("scratch/ctlrm already exists")
        else:
            link.symlink_to(job, target_is_directory=True)
        ignore_runtime(root)
        ignore_runtime(root, "/.scratch/ctlrm")
        publish(
            local / "location.json",
            {"schema_version": 1, "worktree": str(root), "job_directory": str(job)},
        )

    def assign_pr(
        self, project_id: str, job_id: str, number: int, repository: str, *, url: str | None = None
    ) -> dict:
        """Record a searchable PR assignment; changing it preserves the event history."""
        from ctlrm.runtime.projects import PullRequest

        pr = PullRequest(number=number, repository=repository, url=url).model_dump()
        with self.editing(project_id) as (journal, state):
            self.job(project_id, job_id)
            state.setdefault("pull_requests", {})[job_id] = pr
            journal.commit(state, "pr-assigned", {"job_id": job_id, "pr": pr})
        return pr

    def find_pr(
        self, number: int, *, repository: str | None = None, project_id: str | None = None
    ) -> list[dict]:
        """Search repository-qualified PR identities without guessing among collisions."""
        if number < 1:
            raise ValueError("PR number must be positive")
        projects = [self.status(project_id)] if project_id else self.list()
        return [
            {"project_id": p["project"]["id"], **job}
            for p in projects
            for job in p["jobs"]
            if job["pr"]
            and job["pr"]["number"] == number
            and (repository is None or job["pr"]["repository"] == repository.strip().casefold())
        ]
