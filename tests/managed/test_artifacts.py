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
        record = read_record(area.room.root / "artifacts" / f"{artifact}.json")
        assert record["version"]["files"]["link"]["symlink"]
        assert all(not name.startswith(".ctlrm") for name in record["version"]["files"])
        target.write_text("unrelated target changed")
        verify(area, artifact)
