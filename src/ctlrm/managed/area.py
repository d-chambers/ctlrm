"""Immutable coordination identity for one Git worktree and branch."""

import copy
import fcntl
import os
from pathlib import Path
import subprocess

from ctlrm.managed.storage import Journal, identifier, now, publish, read_record
from ctlrm.runtime.location import runtime_path
from ctlrm.runtime.manifest import RoleAssignment, RoomManifest
from ctlrm.runtime.room import RoomRuntime


def git(root: Path, *args: str) -> str:
    """Run a bounded Git query without a shell."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=15
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError("Git query timed out; retry after resolving the slow operation") from error
    if result.returncode:
        raise ValueError(result.stderr.strip() or "not a Git worktree")
    return result.stdout.strip()


def worktree(path: Path) -> tuple[Path, str]:
    """Resolve subdirectories and linked worktrees to their root and branch ref."""
    root = Path(git(path.resolve(), "rev-parse", "--show-toplevel")).resolve()
    branch = git(root, "symbolic-ref", "--quiet", "HEAD")
    if not branch.startswith("refs/heads/"):
        raise ValueError("managed areas require a checked-out branch")
    return root, branch


def ignore_runtime(root: Path, pattern: str = "/.ctlrm/") -> None:
    """Preserve the Git-resolved local exclude file and idempotently add the runtime."""
    result = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "-q", pattern.lstrip("/")],
        capture_output=True,
        timeout=15,
    )
    if result.returncode == 0:
        return
    exclude = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        content = stream.read()
        if pattern not in content.splitlines():
            stream.write(("\n" if content and not content.endswith("\n") else "") + pattern + "\n")
            stream.flush()
            os.fsync(stream.fileno())


class Area:
    """One immutable area definition; live state resides only in committed events."""

    def __init__(self, root: Path, data: dict) -> None:
        """Bind a persisted area to its current physical root."""
        self.root = root
        self.data = data
        self.journal = Journal(root)
        self.room = RoomRuntime(root)

    @classmethod
    def load(cls, path: Path) -> "Area":
        """Load an area through Git, retaining branch drift for status inspection."""
        root = Path(git(path, "rev-parse", "--show-toplevel")).resolve()
        if (root / ".ctlrm").is_symlink():
            raise ValueError("coordination runtime must not be a symlink")
        data = read_record(runtime_path(root) / "area.yaml")
        if data.get("schema_version") != 1:
            raise ValueError("unsupported coordination area schema")
        return cls(root, data)

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        mode: str,
        roles: dict,
        profiles: dict,
        prompt: str,
        definition: dict | None = None,
        adopt: bool = False,
    ) -> "Area":
        """Snapshot one instance, refusing unrelated work or implicit legacy adoption."""
        root, branch = worktree(path)
        if (root / ".ctlrm").is_symlink():
            raise ValueError("coordination runtime must not be a symlink")
        runtime = runtime_path(root)
        if runtime != root / ".ctlrm":
            job = read_record(runtime.parent / "job.json")
            if (
                mode != "workflow"
                or not definition
                or definition.get("task", {}).get("id") != job["id"]
            ):
                raise ValueError("central job area requires its planned workflow")
        journal = Journal(root)
        spec = {
            "mode": mode,
            "roles": roles,
            "profiles": profiles,
            "prompt": prompt,
            "definition": definition,
        }
        spec = copy.deepcopy(spec)
        coordinator = {
            "name": "Control room",
            "kind": "human",
            "provider": None,
            "capabilities": ["coordinate"],
            "restart_command": None,
        }
        area_path = runtime_path(root) / "area.yaml"

        def existing() -> "Area | None":
            """Retry compatible initialization without acquiring the daemon's lifetime lock."""
            if not area_path.exists():
                return None
            area = cls.load(root)
            area.validate()
            if any(
                (
                    area.data.get("requested_roles", area.data["roles"])
                    if key == "roles"
                    else area.data.get(key)
                )
                != value
                for key, value in spec.items()
            ):
                raise ValueError("this worktree already coordinates another instance")
            area.ensure_room()
            return area

        reused = existing()
        if reused is not None:
            return reused
        with journal.writer(purpose="initialize"):
            reused = existing()
            if reused is not None:
                return reused
            children = [p for p in runtime_path(root).iterdir() if p.name != "supervisor"]
            if children and not adopt:
                raise ValueError("populated legacy area: use a fresh worktree or explicit --adopt")
            room_id = identifier("room")
            if children:
                legacy = RoomRuntime(root).read_room()
                expected = {
                    "coordinator": "coordinator",
                    **{key: val["role"] for key, val in roles.items()},
                }
                if (
                    legacy.author != "coordinator"
                    or {a.participant: a.role for a in legacy.assignments} != expected
                ):
                    raise ValueError(
                        "legacy roster must match the managed coordinator and role bindings"
                    )
                if legacy.prompt != prompt:
                    raise ValueError("legacy prompt must match the requested instance")
                legacy_runtime = RoomRuntime(root)
                if legacy_runtime.signature_path("coordinator").exists():
                    signature = legacy_runtime.read_signature("coordinator")
                    coordinator = signature.model_dump(include=set(coordinator))
                for participant, role in spec["roles"].items():
                    if not legacy_runtime.signature_path(participant).exists():
                        continue
                    signature = legacy_runtime.read_signature(participant)
                    provider = (
                        profiles[role["profile"]]["provider"] if role["kind"] == "agent" else None
                    )
                    if (signature.name, signature.kind, signature.provider) != (
                        role["name"],
                        role["kind"],
                        provider,
                    ):
                        raise ValueError(f"legacy participant identity conflicts: {participant}")
                    role["capabilities"] = signature.capabilities
                    role["restart_command"] = signature.restart_command
                room_id = legacy.id
            data = {
                "schema_version": 1,
                "id": identifier("area"),
                "root": str(root),
                "branch": branch,
                "room_id": room_id,
                "created_at": now(),
                "coordinator": coordinator,
                "requested_roles": roles,
                **spec,
            }
            ignore_runtime(root)
            publish(area_path, data)
            area = cls(root, data)
            area.ensure_room()
            return area

    def validate(self) -> None:
        """Pause mutations if the root moved, disappeared, or changed branch."""
        root, branch = worktree(self.root)
        if str(root) != self.data["root"] or branch != self.data["branch"]:
            raise ValueError(
                "coordination root or branch changed; restore the recorded worktree/branch"
            )

    def ensure_room(self) -> None:
        """Recover initialization from the complete immutable area definition."""
        if self.data["mode"] == "workflow":
            publish(self.room.root / "definition.yaml", self.data["definition"])
            from ctlrm.runtime.room import _write_exclusive_atomic

            task = self.data["definition"]["task"]
            content = f"# {task['title']}\n\n{task['instructions']}\n"
            path = self.room.root / "task.md"
            try:
                _write_exclusive_atomic(path, content)
            except FileExistsError:
                if path.read_text() != content:
                    raise ValueError("conflicting immutable task snapshot")
        manifest = RoomManifest(
            id=self.data["room_id"],
            author="coordinator",
            created_at=self.data["created_at"],
            prompt=self.data["prompt"],
            assignments=[RoleAssignment(participant="coordinator", role="coordinator")]
            + [
                RoleAssignment(participant=key, role=value["role"])
                for key, value in self.data["roles"].items()
            ],
        )
        if self.room.room_path.exists():
            existing = self.room.read_room()
            if {a.participant: a.role for a in existing.assignments} == {
                a.participant: a.role for a in manifest.assignments
            }:
                manifest.assignments = existing.assignments
        self.room.initialize(
            manifest,
            **self.data.get(
                "coordinator",
                {
                    "name": "Control room",
                    "kind": "human",
                    "provider": None,
                    "capabilities": ["coordinate"],
                },
            ),
            joined_at=self.data["created_at"],
        )
