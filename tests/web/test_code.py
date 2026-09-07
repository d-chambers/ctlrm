"""Code browsing preserves inventory, content bounds, and accepted archive versions."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from ctlrm.web.service import MAX_TEXT, read_file
from .test_app import respond

BASE = "/api/projects/project/jobs/job"


@pytest.fixture
def started(planned):
    """Start the existing human workflow and expose its disposable worktree."""
    client, store = planned
    assert client.post(BASE + "/start", json={}).status_code == 200
    root = Path(store.job_status("project", "job")["launch"]["root"])
    return client, store, root


class TestCurrentCode:
    """The HTTP viewer reads current code only through a validated Git inventory."""

    def test_inventory_and_nested_text(self, started) -> None:
        """Tracked and untracked text is readable; ignored files and runtime state stay hidden."""
        client, _, root = started
        (root / "nested").mkdir()
        (root / "nested/new.txt").write_text("Résumé\nsecond line\n", encoding="utf-8")
        (root / ".gitignore").write_text("ignored.txt\n")
        (root / "ignored.txt").write_text("private")
        response = client.get(BASE + "/files")
        assert response.status_code == 200, response.text
        assert response.json()["files"] == [".gitignore", "nested/new.txt", "source.txt"]
        assert response.json()["version"] == "current worktree"
        response = client.get(BASE + "/file", params={"name": "nested/new.txt"})
        assert response.status_code == 200, response.text
        assert response.json()["text"] == "Résumé\nsecond line\n"
        response = client.get(BASE + "/file", params={"name": "ignored.txt"})
        assert response.status_code == 409
        assert "not in the selected code inventory" in response.json()["error"]

    @pytest.mark.parametrize(
        ("content", "error"),
        [
            (b"text\x00binary", "binary file"),
            (b"\xff", "not UTF-8"),
            (b"x" * (MAX_TEXT + 1), "1 MiB"),
        ],
        ids=["binary", "invalid-utf8", "oversized"],
    )
    def test_unsupported_content(self, started, content, error) -> None:
        """Unsupported bytes produce a useful domain error, never replacement text or a traceback."""
        client, _, root = started
        (root / "source.txt").write_bytes(content)
        response = client.get(BASE + "/file", params={"name": "source.txt"})
        assert response.status_code == 409
        assert error in response.json()["error"]
        assert "text" not in response.json()

    def test_deleted_tracked_file(self, started) -> None:
        """A still-indexed file disappearing is reported as missing, not empty content."""
        client, _, root = started
        (root / "source.txt").unlink()
        response = client.get(BASE + "/file", params={"name": "source.txt"})
        assert response.status_code == 404
        assert "source.txt" in response.json()["error"]

    def test_unstarted_job(self, planned) -> None:
        """Code inspection cannot implicitly provision a worktree or launch an agent."""
        client, store = planned
        response = client.get(BASE + "/files")
        assert response.status_code == 409
        assert "start the job first" in response.json()["error"]
        assert not (store.root / "worktrees").exists()

    def test_branch_drift(self, started) -> None:
        """Code from a different checked-out branch cannot be presented as this job's work."""
        client, _, root = started
        subprocess.run(
            ["git", "-C", str(root), "checkout", "-b", "unrelated"], check=True, capture_output=True
        )
        response = client.get(BASE + "/files")
        assert response.status_code == 409
        assert "branch changed" in response.json()["error"]

    @pytest.mark.parametrize("failure", ["timeout", "git-error", "too-many-files"])
    def test_inventory_failure(self, started, monkeypatch, failure) -> None:
        """A failed or excessive Git inventory is never presented as a successful partial list."""
        client, _, _ = started
        original = subprocess.run

        def run(argv, *args, **kwargs):
            """Inject only the external inventory boundary, preserving all other real Git queries."""
            if argv[:1] == ["git"] and "ls-files" in argv:
                if failure == "timeout":
                    raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
                if failure == "git-error":
                    return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"")
                names = b"\0".join(str(i).encode() for i in range(10001))
                return subprocess.CompletedProcess(argv, 0, stdout=names, stderr=b"")
            return original(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", run)
        response = client.get(BASE + "/files")
        assert response.status_code == 409
        expected = {
            "timeout": "timed out",
            "git-error": "inventory unavailable",
            "too-many-files": "exceeds 10000",
        }
        assert expected[failure] in response.json()["error"]
        assert "files" not in response.json()


class TestRetainedCode:
    """Archival retains readable bytes without letting a damaged blob impersonate that version."""

    def test_removed_worktree(self, started) -> None:
        """Current and artifact-specific views survive removal of the original worktree."""
        client, store, root = started
        writer = client.get("/api/snapshot").json()["actions"][0]
        assert respond(client, writer, "done", "implement").status_code == 200
        review = client.get("/api/snapshot").json()["actions"][0]
        reference = review["active"]["input"]["artifact"]
        assert respond(client, review, "approved", "approval").status_code == 200
        assert client.post("/api/projects/project/archive", json={}).status_code == 200
        subprocess.run(["git", "-C", str(root), "worktree", "remove", str(root)], check=True)
        assert not root.exists()
        for params, version in [({}, "retained"), ({"artifact": reference}, reference)]:
            listing = client.get(BASE + "/files", params=params)
            assert listing.status_code == 200, listing.text
            assert listing.json() == {"files": ["source.txt"], "version": version}
            response = client.get(BASE + "/file", params={**params, "name": "source.txt"})
            assert response.status_code == 200, response.text
            assert response.json() == {
                "name": "source.txt",
                "text": "initial\n",
                "version": version,
            }
        blobs = store.job_path("project", "job") / "retained/blobs"
        blob = next(p for p in blobs.iterdir() if p.read_bytes() == b"initial\n")
        blob.write_bytes(b"tampered\n")
        response = client.get(BASE + "/file", params={"name": "source.txt", "artifact": reference})
        assert response.status_code == 409
        assert "original bytes are unavailable" in response.json()["error"]


class TestBoundedRead:
    """Regular-file checks hold even at the size limit and when the file changes during a read."""

    def test_exact_limit(self, tmp_path) -> None:
        """A file exactly at the documented size limit remains readable."""
        path = tmp_path / "code.txt"
        path.write_bytes(b"x" * MAX_TEXT)
        assert read_file(tmp_path, path.name) == b"x" * MAX_TEXT

    def test_fifo(self, tmp_path) -> None:
        """A FIFO is rejected immediately, even without a writer on the other end."""
        os.mkfifo(tmp_path / "pipe")
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; "
                "from ctlrm.web.service import read_file; "
                "read_file(Path(sys.argv[1]), 'pipe')",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 1
        assert "ValueError: viewer supports regular text files" in result.stderr

    def test_growth_after_stat(self, tmp_path, monkeypatch) -> None:
        """A small file growing after fstat cannot bypass the read-size limit."""
        path = tmp_path / "code.txt"
        path.write_bytes(b"small")
        inode = path.stat().st_ino
        original = os.fstat

        def fstat(descriptor):
            """Grow the actual file after obtaining the stale size observation."""
            info = original(descriptor)
            if info.st_ino == inode:
                with path.open("ab") as stream:
                    stream.write(b"x" * MAX_TEXT)
            return info

        monkeypatch.setattr(os, "fstat", fstat)
        with pytest.raises(ValueError, match="grew beyond"):
            read_file(tmp_path, path.name)
