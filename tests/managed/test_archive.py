"""Completed projects retain actual code and archive verified records without deleting work."""

import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

import pytest

from ctlrm.managed.archive import verify_archive
from ctlrm.managed import supervisor
from test_projects import finish
from test_projects import human_template as human_template
from test_projects import store as store


class TestProjectArchive:
    """Archive lifecycle preserves code, PR search, and retired process ownership."""

    def test_complete_and_archive(self, store, human_template, tmp_path) -> None:
        """The ZIP contains history and uncommitted outputs, and does not delete the worktree."""
        store.add_job("auth", "A", "x", human_template, job_id="a")
        client, _ = store.start_job("auth", "a")
        output = client.area.root / "new.txt"
        output.write_text("uncommitted result\n")
        store.assign_pr("auth", "a", 42, "owner/repo")
        finish(client)
        result = store.archive("auth")
        assert client.area.root.is_dir()
        assert store.status("auth")["status"] == "archived"
        assert store.find_pr(42)[0]["job"]["id"] == "a"
        assert client.area.journal.replay()[0]["retired"]
        assert not supervisor.running(client.area)
        with pytest.raises(ValueError, match="retired"):
            supervisor.start(client.area)
        manifest = verify_archive(Path(result["path"]))
        assert manifest["project_id"] == "auth"
        with zipfile.ZipFile(result["path"]) as archive:
            retained = json.loads(archive.read("jobs/a/retained/manifest.json"))
            content_hash = retained["version"]["files"]["new.txt"]["sha256"]
            assert archive.read("jobs/a/retained/blobs/" + content_hash) == b"uncommitted result\n"
            assert "jobs/a/retained/code.bundle" in archive.namelist()
            bundle = tmp_path / "code.bundle"
            bundle.write_bytes(archive.read("jobs/a/retained/code.bundle"))
        restored = tmp_path / "restored"
        subprocess.run(["git", "init", str(restored)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(restored), "fetch", str(bundle), "HEAD"],
            check=True,
            capture_output=True,
        )
        fetched = subprocess.run(
            ["git", "-C", str(restored), "rev-parse", "FETCH_HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert fetched == retained["version"]["head"]
        assert store.archive("auth") == result
        with pytest.raises(ValueError, match="completed or archived"):
            store.add_job("auth", "B", "x", human_template, job_id="b")
        subprocess.run(
            [
                "git",
                "-C",
                str(client.area.root),
                "worktree",
                "remove",
                "--force",
                str(client.area.root),
            ],
            check=True,
            capture_output=True,
        )
        assert store.archive("auth") == result

    def test_unfinished_project(self, store, human_template) -> None:
        """Planning is not completion, and archive cannot bypass unfinished jobs."""
        store.add_job("auth", "A", "x", human_template, job_id="a")
        with pytest.raises(ValueError, match="complete every job"):
            store.archive("auth")
        assert store.status("auth")["status"] == "active"

    def test_symlink_targets_excluded(self, store, human_template, tmp_path) -> None:
        """Archival preserves link text without copying unrelated external content."""
        store.add_job("auth", "A", "x", human_template, job_id="a")
        client, _ = store.start_job("auth", "a")
        secret = tmp_path / "external-private.txt"
        secret.write_text("DO-NOT-ARCHIVE-TARGET")
        (client.area.root / "external-link").symlink_to(secret)
        finish(client)
        store.complete("auth")
        (store.job_path("auth", "a") / "external-link").symlink_to(secret)
        result = store.archive("auth")
        with zipfile.ZipFile(result["path"]) as archive:
            assert archive.read("jobs/a/external-link") == str(secret).encode()
            assert all(
                b"DO-NOT-ARCHIVE-TARGET" not in archive.read(name) for name in archive.namelist()
            )

    def test_tampered_archive(self, tmp_path) -> None:
        """A ZIP with valid CRC but wrong recorded content fails verification."""
        path = tmp_path / "bad.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("file", "changed")
            archive.writestr(
                "archive-manifest.json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "project_id": "p",
                        "files": {"file": hashlib.sha256(b"original").hexdigest()},
                    }
                ),
            )
        with pytest.raises(ValueError, match="checksum mismatch"):
            verify_archive(path)

    def test_changed_completion_requires_restore(self, store, human_template) -> None:
        """Archive cannot substitute code edited after the accepted final outcome."""
        store.add_job("auth", "A", "x", human_template, job_id="a")
        client, _ = store.start_job("auth", "a")
        finish(client)
        output = client.area.root / "later.txt"
        output.write_text("not part of completed job")
        with pytest.raises(ValueError, match="changed after completion"):
            store.archive("auth")
        assert store.status("auth")["status"] == "active"
        output.unlink()
        assert store.archive("auth")["files"] > 0

    def test_recover_published_archive(self, store, human_template, monkeypatch) -> None:
        """A crash after ZIP publication does not force overwriting or losing the valid archive."""
        from ctlrm.managed.storage import Journal

        store.add_job("auth", "A", "x", human_template, job_id="a")
        client, _ = store.start_job("auth", "a")
        finish(client)
        original = Journal.commit

        def crash(self, state, kind, detail=None):
            """Simulate power loss at the final project lifecycle commit."""
            if kind == "project-archived":
                raise RuntimeError("simulated crash")
            return original(self, state, kind, detail)

        with monkeypatch.context() as patch:
            patch.setattr(Journal, "commit", crash)
            with pytest.raises(RuntimeError, match="simulated crash"):
                store.archive("auth")
        assert store.status("auth")["status"] == "completed"
        assert (store.root / "archives/auth.zip").exists()
        assert store.archive("auth")["files"] > 0
        assert store.status("auth")["status"] == "archived"

    def test_corrupt_retained_code(self, store, human_template) -> None:
        """Code retention is reverified before a ZIP can certify project completion."""
        store.add_job("auth", "A", "x", human_template, job_id="a")
        client, _ = store.start_job("auth", "a")
        finish(client)
        store.complete("auth")
        (store.job_path("auth", "a") / "retained/code.bundle").write_bytes(b"broken")
        with pytest.raises(ValueError, match="checksum mismatch"):
            store.archive("auth")
        assert not (store.root / "archives/auth.zip").exists()

    def test_retirement_waits_for_process_exit(self, repository, profile) -> None:
        """A pending process stop cannot be mistaken for a safely archived runtime."""
        from ctlrm.communication.terminal import StopPending
        from ctlrm.managed.workflows import WorkflowEngine, WorkflowService
        from ctlrm.runtime.workflows import WorkflowTemplate
        from conftest import FakeTerminal
        from test_workflow_engine import complete

        class PendingTerminal(FakeTerminal):
            """Hold one owned process alive until the test releases it."""

            pending = True

            def stop(self, spec, identity):
                """Model the asynchronous termination boundary."""
                if self.pending:
                    raise StopPending("still stopping")
                super().stop(spec, identity)

        template = WorkflowTemplate.model_validate(
            {
                "name": "single",
                "entry": "work",
                "profiles": {"agent": profile.model_dump()},
                "roles": {"worker": {"kind": "agent", "profile": "agent"}},
                "tasks": {
                    "work": {
                        "role": "worker",
                        "verify_input": False,
                        "transitions": {"done": "terminal:completed"},
                    }
                },
            }
        )
        client, _ = WorkflowService.submit(repository, template, "A", "x")
        terminal = PendingTerminal()
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            complete(engine, client, "done")
            client.request("workflow-retire", {})
            engine.tick()
            assert engine.state["retiring"]
            assert not engine.state.get("retired")
            engine = WorkflowEngine(client.area, terminal)
            terminal.pending = False
            engine.tick()
            engine.tick()
            assert engine.state["retired"] and engine.state["shutdown"]
            assert not terminal.terminals
        with pytest.raises(ValueError, match="retired"):
            client.request("launch", {"participant": "worker"})
        before = client.area.journal.replay()[1]
        with pytest.raises(ValueError, match="retired"):
            supervisor.serve(client.area)
        assert client.area.journal.replay()[1] == before
