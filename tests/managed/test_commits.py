"""Commit previews retain bounded, attributable text without invoking external diff tools."""

import json
import subprocess

import pytest

from ctlrm.managed.artifacts import capture, read_artifact
from ctlrm.managed.commits import read_commit, retain_commit, retain_commits
from ctlrm.managed.area import git


def record(root, content, title):
    """Record one disposable change with an explicit test identity."""
    (root / "change.txt").write_bytes(content)
    git(root, "add", "change.txt")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", title)
    return git(root, "rev-parse", "HEAD")


class TestRetainedCommits:
    """Preview limits are explicit and retained identities detect tampering."""

    def test_large_binary_and_identity(self, area) -> None:
        """Large patches are visibly truncated; binary commits remain readable as metadata."""
        sha = record(area.root, b"x" * (160 * 1024), "Large commit")
        entry = retain_commit(area, sha)
        text = read_commit(area.room.root, area.data["id"], entry["id"])
        assert text["truncated"] and "Display truncated" in text["text"]
        binary = record(area.root, b"\0binary\0data", "Binary commit")
        entry = retain_commit(area, binary)
        assert "Binary files" in read_commit(area.room.root, area.data["id"], entry["id"])["text"]
        with pytest.raises(ValueError, match="identity"):
            read_commit(area.room.root, "foreign-area", entry["id"])
        with pytest.raises(ValueError, match="full Git"):
            retain_commit(area, "HEAD:change.txt")
        path = area.room.root / "commits" / f"{entry['id']}.json"
        path.write_text(json.dumps({**text, "text": "altered"}))
        with pytest.raises(ValueError, match="identity"):
            read_commit(area.room.root, area.data["id"], entry["id"])

    def test_range_and_rewrite(self, area, monkeypatch) -> None:
        """Bound long ranges and label rewritten history instead of attributing unrelated commits."""
        base = git(area.root, "rev-parse", "HEAD")
        first = record(area.root, b"one", "One")
        head = record(area.root, b"two", "Two")
        monkeypatch.setattr("ctlrm.managed.commits.MAX_COMMITS", 1)
        result = retain_commits(area, base, head)
        assert [item["sha"] for item in result["commits"]] == [head]
        assert "newest 1" in result["commits_note"]
        git(area.root, "reset", "--hard", base)
        replacement = record(area.root, b"replacement", "Replacement")
        result = retain_commits(area, first, replacement)
        assert result["commits"] == [] and "rewritten" in result["commits_note"]
        assert result["head_commit"]["sha"] == replacement

    def test_external_diff_disabled(self, area) -> None:
        """Git preview never runs an external diff command from repository configuration."""
        sha = record(area.root, b"text", "Code change")
        marker = area.root / "external-ran"
        git(area.root, "config", "diff.external", f"touch {marker}")
        artifact = capture(area, "run", "execution", base_commit=sha)
        assert read_artifact(area.room.root, area.data["id"], artifact)["head_commit"]["sha"] == sha
        assert not marker.exists()

    def test_timeout(self, area, monkeypatch) -> None:
        """A timed out Git formatting process is reported as a domain error."""

        def timeout(*args, **kwargs):
            """Simulate a stalled Git subprocess."""
            raise subprocess.TimeoutExpired("git", 15)

        monkeypatch.setattr("ctlrm.managed.commits.subprocess.run", timeout)
        with pytest.raises(ValueError, match="could not be read"):
            retain_commit(area, "a" * 40)
