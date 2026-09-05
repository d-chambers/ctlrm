"""Read projections and bounded file access for the local browser workbench."""

import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess

from ctlrm.managed.area import Area
from ctlrm.managed.artifacts import read_artifact
from ctlrm.managed.projects import ProjectStore
from ctlrm.managed.storage import Journal, read_record
from ctlrm.managed.workflows import WorkflowService

MAX_TEXT = 1024 * 1024


def read_file(root: Path, name: str) -> bytes:
    """Read a bounded regular file without following any directory or file symlinks.

    Parameters
    ----------
    root : Path
        Trusted directory selected from the project records.
    name : str
        Relative POSIX file name.
    """
    path = PurePosixPath(name)
    if not name or path.is_absolute() or any(p in {"", ".", ".."} for p in name.split("/")):
        raise ValueError("file name must be a safe relative path")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TEXT:
                raise ValueError("viewer supports regular text files up to 1 MiB")
            data = stream.read(MAX_TEXT + 1)
            if len(data) > MAX_TEXT:
                raise ValueError("file grew beyond the viewer limit")
            return data
    finally:
        os.close(directory)


def text_content(data: bytes) -> str:
    """Reject binary data instead of rendering misleading replacement characters."""
    if b"\x00" in data:
        raise ValueError("binary file; use the worktree or retained archive")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("file is not UTF-8 text") from error


class Workbench:
    """Reuse the central store and supervisor without introducing another state authority."""

    def __init__(self, store: ProjectStore) -> None:
        """Bind a browser service to a user-selected central data directory."""
        self.store = store

    def runtime(self, project: str, job: str) -> Path:
        """Locate a validated job's durable runtime, even after removing its worktree."""
        self.store.job(project, job)
        return self.store.job_path(project, job) / "runtime"

    def client(self, project: str, job: str, *, validate_worktree: bool = True) -> WorkflowService:
        """Resolve one job; explicit stop/cancel and inspection can tolerate branch drift."""
        self.store.project(project)
        state = self.store.journal(project).replay()[0]
        if state.get("project_status", "active") != "active":
            raise ValueError("project is read-only")
        runtime = self.runtime(project, job)
        path = self.store.job_path(project, job) / "launch.json"
        if not path.exists():
            raise ValueError("start the job first")
        area = Area.load(Path(read_record(path)["root"]))
        if area.room.root.resolve() != runtime.resolve():
            raise ValueError("worktree no longer belongs to this job")
        if validate_worktree:
            area.validate()
        return WorkflowService(area)

    def session(
        self, project: str, job: str, session_id: str, generation: int
    ) -> tuple[Area, dict]:
        """Resolve a current session for explicit interaction or a terminal attachment."""
        client = self.client(project, job, validate_worktree=False)
        state = client.area.journal.replay()[0]
        session = state["sessions"].get(session_id)
        if not session or session["generation"] != generation:
            raise ValueError("session generation changed; reopen its terminal")
        if not session.get("identity"):
            raise ValueError("session has no owned terminal yet")
        return client.area, session

    def snapshot(self) -> dict:
        """Expose project progress and participants without publishing launch credentials."""
        projects = self.store.list()
        participants = []
        actions = []
        for project in projects:
            pid = project["project"]["id"]
            for item in project["jobs"]:
                job = item["job"]
                jid = job["id"]
                workflow = job.get("workflow", {})
                run = item.get("run") or {}
                active = next(
                    (e for e in run.get("executions", []) if e["id"] == run.get("active")), None
                )
                clean = {}
                for sid, session in item.get("sessions", {}).items():
                    clean[sid] = {
                        k: session.get(k)
                        for k in (
                            "id",
                            "participant",
                            "generation",
                            "native_id",
                            "status",
                            "ready",
                            "recovering",
                            "error",
                            "work",
                            "interactions",
                            "recovery",
                        )
                    }
                    clean[sid]["input_mode"] = session["profile"]["input_mode"]
                    clean[sid]["attached_available"] = (
                        bool(session.get("identity"))
                        and session["status"] != "stopped"
                        and project["status"] == "active"
                    )
                item["sessions"] = clean
                for role, definition in workflow.get("roles", {}).items():
                    session = next((s for s in clean.values() if s["participant"] == role), None)
                    participant = {
                        "id": f"{pid}/{jid}/{role}",
                        "project": pid,
                        "job": jid,
                        "name": role,
                        "role": role,
                        "kind": definition["kind"],
                        "provider": workflow.get("profiles", {})
                        .get(definition.get("profile"), {})
                        .get("provider"),
                        "session": session,
                        "active": active if active and active["participant"] == role else None,
                    }
                    participants.append(participant)
                    if (
                        participant["active"]
                        and definition["kind"] == "human"
                        and run.get("status") == "running"
                        and active.get("message")
                        and project["status"] == "active"
                    ):
                        actions.append(
                            {
                                **participant,
                                "run_id": run["id"],
                                "task": workflow["tasks"][active["task"]],
                            }
                        )
        return {
            "projects": projects,
            "participants": participants,
            "actions": actions,
            "data_directory": str(self.store.root),
        }

    def activity(self, project: str) -> list[dict]:
        """Show bounded accepted history with project and job attribution."""
        status = self.store.status(project)
        roots = [(None, self.store.journal(project).root)]
        roots.extend(
            (j["job"]["id"], self.runtime(project, j["job"]["id"])) for j in status["jobs"]
        )
        events = []
        for job, root in roots:
            # Replay first so corrupt history never appears as accepted activity.
            Journal(root, storage_root=root).replay()
            for path in sorted((root / "events").glob("*.json"))[-60:]:
                record = read_record(path)
                events.append(
                    {"job": job, **{k: record[k] for k in ("sequence", "at", "kind", "detail")}}
                )
        return sorted(events, key=lambda event: event["at"], reverse=True)[:100]

    def artifact(self, project: str, job: str, reference: str) -> dict:
        """Verify a content-addressed manifest before displaying its version metadata."""
        root = self.runtime(project, job)
        area = read_record(root / "area.yaml")
        return read_artifact(root, area["id"], reference)

    def files(self, project: str, job: str, artifact: str | None = None) -> dict:
        """List current code or a manifest inventory, preserving the selected version."""
        status = self.store.job_status(project, job)
        retained = self.store.job_path(project, job) / "retained"
        if artifact:
            version = self.artifact(project, job, artifact)["version"]
            return {"files": sorted(version["files"]), "version": artifact}
        if (retained / "manifest.json").exists():
            manifest = read_record(retained / "manifest.json")
            return {"files": sorted(manifest["version"]["files"]), "version": "retained"}
        client = self.client(project, job)
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(client.area.root),
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                ],
                capture_output=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError("code inventory query timed out") from error
        if result.returncode:
            raise ValueError(
                result.stderr.decode(errors="replace").strip() or "code inventory unavailable"
            )
        names = result.stdout.decode("utf-8").split("\0")
        names = sorted({n for n in names if n and not n.startswith((".ctlrm/", ".scratch/ctlrm/"))})
        if len(names) > 10000:
            raise ValueError("code inventory exceeds 10000 files")
        return {"files": names, "version": "current worktree", "branch": status["launch"]["branch"]}

    def file(self, project: str, job: str, name: str, artifact: str | None = None) -> dict:
        """Read only selected code; never mislabel changed bytes as an earlier artifact."""
        listing = self.files(project, job, artifact)
        if name not in listing["files"]:
            raise ValueError("file is not in the selected code inventory")
        retained = self.store.job_path(project, job) / "retained"
        info = None
        if artifact:
            info = self.artifact(project, job, artifact)["version"]["files"][name]
        elif listing["version"] == "retained":
            info = read_record(retained / "manifest.json")["version"]["files"][name]
        if info and (info.get("symlink") or "sha256" not in info):
            raise ValueError("this entry is a link, deleted file, or nested repository")
        if info and (retained / "blobs" / info["sha256"]).exists():
            data = read_file(retained, f"blobs/{info['sha256']}")
        else:
            launch = self.store.job_status(project, job).get("launch")
            if not launch:
                raise ValueError("job has no worktree")
            data = read_file(Path(launch["root"]), name)
        if info and hashlib.sha256(data).hexdigest() != info["sha256"]:
            raise ValueError(
                "file has changed since this artifact; its original bytes are unavailable"
            )
        return {"name": name, "text": text_content(data), "version": listing["version"]}
