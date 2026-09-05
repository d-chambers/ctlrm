"""Immutable coordination identity for one Git worktree and branch."""

import fcntl
import os
from pathlib import Path
import subprocess

from ctlrm.managed.storage import Journal, identifier, now, publish, read_record
from ctlrm.runtime.manifest import RoleAssignment, RoomManifest
from ctlrm.runtime.room import RoomRuntime


def git(root: Path, *args: str) -> str:
    """Run a bounded Git query without a shell."""
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=15
    )
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


def ignore_runtime(root: Path) -> None:
    """Preserve the Git-resolved local exclude file and idempotently add the runtime."""
    result = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "-q", ".ctlrm/"], capture_output=True, timeout=15
    )
    if result.returncode == 0:
        return
    exclude = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        content = stream.read()
        if "/.ctlrm/" not in content.splitlines():
            stream.write(("\n" if content and not content.endswith("\n") else "") + "/.ctlrm/\n")
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
        data = read_record(root / ".ctlrm/area.yaml")
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
        journal = Journal(root)
        spec = {
            "mode": mode,
            "roles": roles,
            "profiles": profiles,
            "prompt": prompt,
            "definition": definition,
        }
        with journal.writer():
            area_path = root / ".ctlrm/area.yaml"
            if area_path.exists():
                area = cls.load(root)
                area.validate()
                if any(area.data.get(key) != value for key, value in spec.items()):
                    raise ValueError("this worktree already coordinates another instance")
                area.ensure_room()
                return area
            children = [p for p in (root / ".ctlrm").iterdir() if p.name != "supervisor"]
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
                room_id = legacy.id
            data = {
                "schema_version": 1,
                "id": identifier("area"),
                "root": str(root),
                "branch": branch,
                "room_id": room_id,
                "created_at": now(),
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
        self.room.initialize(
            manifest,
            name="Control room",
            kind="human",
            provider=None,
            capabilities=["coordinate"],
            joined_at=self.data["created_at"],
        )
