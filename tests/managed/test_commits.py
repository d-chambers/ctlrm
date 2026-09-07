"""Commit previews retain bounded, attributable text without invoking external diff tools."""

import json
import subprocess
import sys
import time

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
        text = read_commit(area.runtime, area.data["id"], entry["id"])
        assert text["truncated"] and "Display truncated" in text["text"]
        binary = record(area.root, b"\0binary\0data", "Binary commit")
        entry = retain_commit(area, binary)
        assert "Binary files" in read_commit(area.runtime, area.data["id"], entry["id"])["text"]
        with pytest.raises(ValueError, match="identity"):
            read_commit(area.runtime, "foreign-area", entry["id"])
        with pytest.raises(ValueError, match="full Git"):
            retain_commit(area, "HEAD:change.txt")
        path = area.runtime / "commits" / f"{entry['id']}.json"
        path.write_text(json.dumps({**text, "text": "altered"}))
        with pytest.raises(ValueError, match="identity"):
            read_commit(area.runtime, area.data["id"], entry["id"])

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
        assert read_artifact(area.runtime, area.data["id"], artifact)["head_commit"]["sha"] == sha
        assert not marker.exists()

    def test_timeout(self, area, monkeypatch) -> None:
        """A stalled preview is reaped and explicitly unavailable without blocking a handoff."""
        original = subprocess.Popen
        processes = []

        def stalled(*args, **kwargs):
            """Model Git stalled before producing its first byte."""
            process = original([sys.executable, "-c", "import time; time.sleep(5)"], **kwargs)
            processes.append(process)
            return process

        monkeypatch.setattr("ctlrm.managed.commits.subprocess.Popen", stalled)
        monkeypatch.setattr("ctlrm.managed.commits.PREVIEW_SECONDS", 0.05)
        started = time.monotonic()
        entry = retain_commit(area, "a" * 40)
        assert time.monotonic() - started < 3
        assert len(processes) == 1 and processes[0].poll() is not None
        document = read_commit(area.runtime, area.data["id"], entry["id"])
        assert document["truncated"] and "preview unavailable" in document["text"]

    def test_stops_large_output(self, area, monkeypatch) -> None:
        """The preview stops its producer at the display cap instead of spooling the whole patch."""
        original = subprocess.Popen
        processes = []
        marker = area.root / "finished-output"
        code = (
            "import pathlib,sys,time; sys.stdout.buffer.write(b'x' * (2*1024*1024)); "
            "sys.stdout.flush(); pathlib.Path(sys.argv[1]).write_text('finished'); time.sleep(5)"
        )

        def large(*args, **kwargs):
            """Generate more data than a pipe can buffer, followed by a completion marker."""
            process = original([sys.executable, "-c", code, str(marker)], **kwargs)
            processes.append(process)
            return process

        monkeypatch.setattr("ctlrm.managed.commits.subprocess.Popen", large)
        entry = retain_commit(area, "a" * 40)
        assert len(processes) == 1 and processes[0].poll() is not None
        assert not marker.exists()
        document = read_commit(area.runtime, area.data["id"], entry["id"])
        assert document["truncated"] and "Display truncated" in document["text"]
        assert len(document["text"].encode()) < 129 * 1024

    def test_missing_base(self, area) -> None:
        """Pruned pre-rewrite history cannot prevent an otherwise valid artifact handoff."""
        original = git(area.root, "rev-parse", "HEAD")
        removed = record(area.root, b"old", "Old branch")
        git(area.root, "reset", "--hard", original)
        head = record(area.root, b"replacement", "Replacement")
        git(area.root, "reflog", "expire", "--expire=now", "--all")
        git(area.root, "gc", "--prune=now")
        with pytest.raises(ValueError):
            git(area.root, "cat-file", "-e", removed)
        reference = capture(area, "run", "execution", base_commit=removed)
        artifact = read_artifact(area.runtime, area.data["id"], reference)
        assert artifact["head_commit"]["sha"] == head
        assert artifact["head_commit"]["title"] == "Replacement"
        assert artifact["commits"] == [] and "history is unavailable" in artifact["commits_note"]

    def test_exhausted_budget(self, area, monkeypatch) -> None:
        """One range budget also applies to HEAD and history discovery, with no new subprocesses."""
        monkeypatch.setattr("ctlrm.managed.commits.PREVIEW_SECONDS", 0)

        def unexpected(*args, **kwargs):
            """Fail if exhausted preview work starts another command."""
            pytest.fail("preview subprocess started after the shared deadline")

        monkeypatch.setattr("ctlrm.managed.commits.subprocess.Popen", unexpected)
        result = retain_commits(area, "a" * 40, "b" * 40)
        assert result["head_commit"]["sha"] == "b" * 40
        assert result["commits"] == [] and "history is unavailable" in result["commits_note"]

    def test_shared_range_budget(self, area, monkeypatch) -> None:
        """Successive previews cannot reset the range deadline and keep a handoff waiting."""
        from types import SimpleNamespace
        import ctlrm.managed.commits as commits

        clock = [0.0]
        calls = []
        head, older, oldest = "a" * 40, "b" * 40, "c" * 40

        def text(root, *arguments, deadline):
            """Model bounded external commands that consume the same monotonic budget."""
            calls.append(arguments)
            cost = 2 if arguments[0] == "show" else 1
            if clock[0] + cost >= deadline:
                clock[0] = deadline
                raise ValueError("preview budget exhausted")
            clock[0] += cost
            if arguments[0] == "show":
                return "commit metadata\n\n    Subject\n", False
            if "--not" in arguments:
                return "", False
            return f"{head}\n{older}\n{oldest}\n", False

        monkeypatch.setattr(commits, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr(commits, "_git_text", text)
        result = retain_commits(area, "d" * 40, head)
        assert clock[0] == commits.PREVIEW_SECONDS
        assert len(calls) == 4
        assert [item["sha"] for item in result["commits"]] == [head, older]
        assert result["commits"][1]["truncated"]
        assert "time budget reached" in result["commits_note"]
