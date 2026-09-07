"""Artifact attribution and stale approvals include relevant untracked content."""

import pytest

from ctlrm.managed.artifacts import capture, verify
from ctlrm.managed.storage import read_record


class TestArtifacts:
    """Approval checks compare content rather than trusting a changed worktree's HEAD."""

    @pytest.mark.parametrize("target", ["source.txt", "new.txt"])
    def test_content_drift(self, area, target) -> None:
        """Both tracked edits and untracked output are part of the delivered version."""
        path = area.root / target
        path.write_text("delivered")
        artifact = capture(area, "run", "execution")
        verify(area, artifact)
        path.write_text("changed after review began")
        with pytest.raises(ValueError, match="artifact changed"):
            verify(area, artifact)

    def test_symlink_is_not_followed(self, area, tmp_path) -> None:
        """A symlink identifies its link text; unrelated target data is never hashed."""
        target = tmp_path.parent / "external.txt"
        target.write_text("external content")
        (area.root / "link").symlink_to(target)
        artifact = capture(area, "run", "execution")
        record = read_record(area.runtime / "artifacts" / f"{artifact}.json")
        assert record["version"]["files"]["link"]["symlink"]
        assert all(not name.startswith(".ctlrm") for name in record["version"]["files"])
        target.write_text("unrelated target changed")
        verify(area, artifact)


class TestGitDirectories:
    """Index gitlinks and replaced directories must not accidentally inspect their parent repo."""

    def test_uninitialized_gitlink(self, area) -> None:
        """An uninitialized submodule records its index SHA even when parent files are dirty."""
        from ctlrm.managed.area import git
        from ctlrm.managed.artifacts import fingerprint

        head = git(area.root, "rev-parse", "HEAD")
        git(area.root, "update-index", "--add", "--cacheinfo", f"160000,{head},lib")
        (area.root / "lib").mkdir()
        (area.root / "new.txt").write_text("untracked output")
        record = fingerprint(area.root)
        assert record["files"]["lib"] == {"index_gitlink": head, "initialized": False}
        assert record["dirty"] and "new.txt" in record["files"]

    def test_file_replaced_by_directory(self, area) -> None:
        """The directory and its non-ignored children identify a real type change."""
        from ctlrm.managed.artifacts import fingerprint

        path = area.root / "source.txt"
        path.unlink()
        path.mkdir()
        (path / "child.txt").write_text("replacement")
        record = fingerprint(area.root)
        assert record["files"]["source.txt"]["directory"]
        assert "source.txt/child.txt" in record["files"]


class TestIndexChanges:
    """Changes staged independently of working-tree bytes are part of the delivered version."""

    def test_staged_mode_drift(self, area) -> None:
        """A staged mode change cannot hide behind already-dirty status and unchanged file bytes."""
        from ctlrm.managed.area import git

        (area.root / "other.txt").write_text("already dirty")
        artifact = capture(area, "run", "execution")
        blob = git(area.root, "rev-parse", "HEAD:source.txt")
        git(area.root, "update-index", "--cacheinfo", f"100755,{blob},source.txt")
        with pytest.raises(ValueError, match="artifact changed"):
            verify(area, artifact)
