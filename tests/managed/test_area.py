"""Main/linked worktree isolation and immutable coordination identity."""

from pathlib import Path
import subprocess

import pytest

from ctlrm.managed.area import Area, git, worktree


class TestArea:
    """One branch/worktree owns one coordination instance."""

    def test_subdirectory_and_branch_drift(self, area) -> None:
        """Subdirectories resolve to the area; a changed branch blocks mutations."""
        sub = area.root / "sub"
        sub.mkdir()
        assert Area.load(sub).root == area.root
        git(area.root, "switch", "-c", "changed")
        with pytest.raises(ValueError, match="branch changed"):
            area.validate()

    def test_independent_linked_worktree(self, area, tmp_path_factory) -> None:
        """Linked worktrees share Git metadata, never runtime state or terminal IDs."""
        linked = tmp_path_factory.mktemp("linked") / "tree"
        git(area.root, "worktree", "add", "-b", "other", str(linked))
        spec = {key: area.data[key] for key in ("mode", "roles", "profiles", "prompt")}
        other = Area.create(linked, **spec)
        assert other.data["id"] != area.data["id"]
        assert other.journal.root != area.journal.root
        assert (linked / ".git").is_file()
        assert (
            subprocess.run(
                ["git", "-C", str(linked), "check-ignore", "-q", ".ctlrm/"], capture_output=True
            ).returncode
            == 0
        )

    def test_second_instance_rejected(self, area) -> None:
        """An existing area cannot silently acquire a different task or roster."""
        with pytest.raises(ValueError, match="another instance"):
            Area.create(
                area.root,
                mode="standalone",
                roles=area.data["roles"],
                profiles=area.data["profiles"],
                prompt="Different",
            )

    def test_populated_area_rejected(self, repository: Path) -> None:
        """A populated coordination directory is never overwritten."""
        runtime = repository / ".ctlrm"
        runtime.mkdir()
        (runtime / "existing.md").write_text("preserve")
        with pytest.raises(ValueError, match="populated"):
            Area.create(repository, mode="standalone", roles={}, profiles={}, prompt="Task")
        assert (runtime / "existing.md").read_text() == "preserve"

    def test_detached_rejected(self, repository: Path) -> None:
        """Native managed sessions require an explicit branch identity."""
        git(repository, "checkout", "--detach")
        with pytest.raises(ValueError):
            worktree(repository)


class TestRuntimeSymlinks:
    """Coordination data cannot be redirected into another worktree's storage."""

    def test_external_runtime_rejected(self, repository: Path, tmp_path_factory) -> None:
        """Reject before creating a lock or writing any redirected state."""
        elsewhere = tmp_path_factory.mktemp("external-runtime")
        (repository / ".ctlrm").symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(ValueError, match="symlink"):
            Area.create(repository, mode="standalone", roles={}, profiles={}, prompt="Task")
        assert list(elsewhere.iterdir()) == []


class TestInitializationRetry:
    """A running supervisor must not prevent idempotent client launch retries."""

    def test_retry_while_supervisor_holds_lock(self, area) -> None:
        """Reuse immutable identity without taking the lifetime writer lock."""
        with area.journal.writer():
            reused = Area.create(
                area.root,
                mode=area.data["mode"],
                roles=area.data["roles"],
                profiles=area.data["profiles"],
                prompt=area.data["prompt"],
            )
        assert reused.data["id"] == area.data["id"]
