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
        from ctlrm.managed.sessions import message
        from ctlrm.runtime.messages import MailboxMessage

        with pytest.raises(ValueError, match="retired"):
            client.area.room.send_message(
                MailboxMessage.model_validate(message("owner", "message", "late message"))
            )
        with pytest.raises(ValueError, match="retired"):
            client.area.room.init_participant("late")
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
                (self.root / "events/.000000000002.json.crash").write_text("unpublished event")
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

    @pytest.mark.parametrize("drift", [False, True])
    def test_retirement_waits_for_process_exit(self, repository, profile, drift) -> None:
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
            if drift:
                subprocess.run(
                    ["git", "-C", str(repository), "switch", "-c", "drift"],
                    check=True,
                    capture_output=True,
                )
                engine.tick()
                assert engine.state["paused"]
                subprocess.run(
                    ["git", "-C", str(repository), "switch", "main"],
                    check=True,
                    capture_output=True,
                )
                request = client.request("reconcile-area", {})
                engine.tick()
                assert engine.state["requests"][request]["status"] == "accepted"
                assert not engine.state["paused"]
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

    def test_text_converter_cannot_change_index(self, store, human_template, tmp_path) -> None:
        """Retained patches restore raw staged bytes despite repository diff configuration."""
        store.add_job("auth", "A", "x", human_template, job_id="a")
        client, _ = store.start_job("auth", "a")
        root = client.area.root

        def git(*args):
            """Run Git against the disposable job's worktree."""
            return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

        (root / ".gitattributes").write_text("source.txt diff=redact\n")
        git("config", "diff.redact.textconv", "sed s/secret/redacted/g")
        (root / "source.txt").write_text("staged secret\n")
        git("add", ".gitattributes", "source.txt")
        (root / "source.txt").write_text("unstaged secret\n")
        finish(client)
        store.complete("auth")
        retained = store.job_path("auth", "a") / "retained"
        restored = tmp_path / "restored"
        subprocess.run(["git", "init", str(restored)], check=True, capture_output=True)
        for args in [
            ("fetch", str(retained / "code.bundle"), "HEAD"),
            ("reset", "--hard", "FETCH_HEAD"),
            ("apply", "--index", str(retained / "index.patch")),
        ]:
            subprocess.run(["git", "-C", str(restored), *args], check=True, capture_output=True)
        staged = subprocess.run(
            ["git", "-C", str(restored), "show", ":source.txt"], check=True, capture_output=True
        ).stdout
        assert staged == b"staged secret\n"
        manifest = json.loads((retained / "manifest.json").read_text())
        checksum = manifest["version"]["files"]["source.txt"]["sha256"]
        assert (retained / "blobs" / checksum).read_bytes() == b"unstaged secret\n"

    def test_final_artifact_identity(self, store, human_template) -> None:
        """An edited artifact document cannot redefine the accepted final version."""
        from ctlrm.managed.artifacts import fingerprint

        store.add_job("auth", "A", "x", human_template, job_id="a")
        client, _ = store.start_job("auth", "a")
        finish(client)
        last = store.job_status("auth", "a")["run"]["executions"][-1]
        artifact = client.area.room.root / "artifacts" / f"{last['artifact']}.json"
        record = json.loads(artifact.read_text())
        (client.area.root / "source.txt").write_text("unaccepted later version\n")
        record["version"] = fingerprint(client.area.root)
        artifact.write_text(json.dumps(record))
        with pytest.raises(ValueError, match="invalid artifact identity"):
            store.archive("auth")
        assert store.status("auth")["status"] == "active"

    def test_incomplete_retained_inventory(self, store, human_template) -> None:
        """Removing a checksum entry cannot hide missing retained history."""
        store.add_job("auth", "A", "x", human_template, job_id="a")
        client, _ = store.start_job("auth", "a")
        finish(client)
        store.complete("auth")
        retained = store.job_path("auth", "a") / "retained"
        manifest = json.loads((retained / "manifest.json").read_text())
        del manifest["files"]["code.bundle"]
        (retained / "code.bundle").unlink()
        (retained / "manifest.json").write_text(json.dumps(manifest))
        with pytest.raises(ValueError, match="inventory"):
            store.archive("auth")

    def test_fifo_rejected_without_waiting(self, tmp_path) -> None:
        """Unsupported named pipes fail promptly rather than blocking before file validation."""
        import os
        import sys

        project = tmp_path / "records"
        project.mkdir()
        os.mkfifo(project / "pipe")
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; from ctlrm.managed.archive import archive; archive(Path(sys.argv[1]), Path(sys.argv[2]))",
                str(project),
                str(tmp_path / "archive.zip"),
            ],
            capture_output=True,
            timeout=5,
        )
        assert result.returncode != 0
        assert b"unsupported or oversized archive file" in result.stderr
        assert not (tmp_path / "archive.zip").exists()

    def test_rebuild_deleted_archive(self, store, human_template, tmp_path) -> None:
        """Missing ZIP recovery preserves the superseded identity in project history."""
        store.add_job("auth", "A", "x", human_template, job_id="a")
        client, _ = store.start_job("auth", "a")
        finish(client)
        original = store.archive("auth")
        Path(original["path"]).unlink()
        subprocess.run(
            [
                "git",
                "-C",
                store.project("auth").codebase,
                "worktree",
                "remove",
                "--force",
                str(client.area.root),
            ],
            check=True,
            capture_output=True,
        )
        assert not client.area.root.exists()
        rebuilt = store.archive("auth", tmp_path / "rebuilt.zip")
        assert rebuilt["path"] != original["path"]
        assert rebuilt["sha256"] != original["sha256"]
        assert verify_archive(Path(rebuilt["path"]))["project_id"] == "auth"
        assert store.status("auth")["archive"] == rebuilt
        assert store.archive("auth") == rebuilt
        assert original["sha256"] in "".join(
            p.read_text() for p in (store.project_path("auth") / "control/events").glob("*.json")
        )

    def test_self_consistent_zip_tamper(self, store, human_template, tmp_path) -> None:
        """Internal ZIP checksums cannot override the externally committed archive identity."""
        store.add_job("auth", "A", "x", human_template, job_id="a")
        client, _ = store.start_job("auth", "a")
        finish(client)
        result = store.archive("auth")
        path = Path(result["path"])
        with pytest.raises(ValueError, match="another path"):
            store.archive("auth", tmp_path / "other.zip")
        edited = tmp_path / "edited.zip"
        with zipfile.ZipFile(path) as source, zipfile.ZipFile(edited, "w") as output:
            manifest = json.loads(source.read("archive-manifest.json"))
            for item in source.infolist():
                if item.filename == "archive-manifest.json":
                    continue
                content = source.read(item.filename)
                if item.filename == "jobs/a/job.json":
                    content += b" "
                    manifest["files"][item.filename] = hashlib.sha256(content).hexdigest()
                output.writestr(item, content)
            output.writestr("archive-manifest.json", json.dumps(manifest))
        edited.replace(path)
        assert verify_archive(path)["project_id"] == "auth"
        with pytest.raises(ValueError, match="identity differs"):
            store.archive("auth")

    def test_recovery_rejects_changed_records(self, store, human_template, monkeypatch) -> None:
        """An existing ZIP cannot recover an interrupted publication if its source differs."""
        from ctlrm.managed.storage import Journal

        store.add_job("auth", "A", "x", human_template, job_id="a")
        client, _ = store.start_job("auth", "a")
        finish(client)
        original = Journal.commit

        def crash(self, state, kind, detail=None):
            """Interrupt after publication and before the archive identity is accepted."""
            if kind == "project-archived":
                raise RuntimeError("interrupted")
            return original(self, state, kind, detail)

        with monkeypatch.context() as patch:
            patch.setattr(Journal, "commit", crash)
            with pytest.raises(RuntimeError, match="interrupted"):
                store.archive("auth")
        path = store.root / "archives/auth.zip"
        before = path.read_bytes()
        (store.project_path("auth") / "unexpected.txt").write_text("later record")
        with pytest.raises(ValueError, match="does not match"):
            store.archive("auth")
        assert path.read_bytes() == before

    @pytest.mark.parametrize("timeout", [False, True])
    def test_complete_waits_for_supervisor(
        self, store, human_template, profile, monkeypatch, timeout
    ) -> None:
        """Completion waits for actual retirement and lock release, or reports pending work."""
        import itertools
        import threading
        import time
        from types import SimpleNamespace
        from ctlrm.communication.terminal import StopPending
        from ctlrm.managed import projects
        from ctlrm.managed.workflows import WorkflowEngine
        from conftest import FakeTerminal
        from test_workflow_engine import complete

        release = threading.Event()
        entered = threading.Event()
        errors = []
        pauses = []

        class WaitingTerminal(FakeTerminal):
            """Delay process termination until the simulated provider actually exits."""

            def stop(self, spec, identity):
                """A stop intent alone cannot certify process death."""
                if not release.is_set():
                    raise StopPending("provider still exiting")
                super().stop(spec, identity)

        human_template.profiles = {"agent": profile}
        human_template.roles["owner"].kind = "agent"
        human_template.roles["owner"].profile = "agent"
        store.add_job("auth", "A", "x", human_template, job_id="a")
        client, _ = store.start_job("auth", "a")
        terminal = WaitingTerminal()
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, terminal)
            engine.tick()
            complete(engine, client, "done")

        def serve():
            """Run the real workflow state machine under the real supervisor lock."""
            try:
                with client.area.journal.writer():
                    current = WorkflowEngine(client.area, terminal)
                    entered.set()
                    while not current.state["shutdown"]:
                        current.tick()
                        time.sleep(0.01)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=serve)

        def start(area):
            """Replace only detached process creation with an inspectable test thread."""
            assert area.root == client.area.root
            thread.start()
            assert entered.wait(3)

        def pause(duration):
            """Record actual entry into the project's retirement wait."""
            pauses.append(duration)
            if not timeout:
                release.set()
            time.sleep(0.01)

        counter = itertools.count(step=100)
        clock = (lambda: next(counter)) if timeout else time.monotonic
        try:
            with monkeypatch.context() as patch:
                patch.setattr(supervisor, "start", start)
                patch.setattr(projects, "time", SimpleNamespace(monotonic=clock, sleep=pause))
                if timeout:
                    with pytest.raises(RuntimeError, match="retirement is still pending"):
                        store.complete("auth")
                    assert not (store.job_path("auth", "a") / "retained").exists()
                else:
                    assert store.complete("auth")["status"] == "completed"
                    assert pauses
                    assert client.area.journal.replay()[0]["retired"]
                    assert not supervisor.running(client.area)
                    assert not terminal.terminals
        finally:
            release.set()
            if thread.ident is not None:
                thread.join(3)
        assert not errors and not thread.is_alive()

    def test_supervisor_retires_before_start_poll(self, store, human_template, monkeypatch) -> None:
        """A daemon can consume pending retirement and exit before start sees its lock."""
        from types import SimpleNamespace
        from ctlrm.managed.workflows import WorkflowEngine
        from conftest import FakeTerminal

        store.add_job("auth", "A", "x", human_template, job_id="a")
        client, _ = store.start_job("auth", "a")
        finish(client)
        client.request("workflow-retire", {})

        def popen(*args, **kwargs):
            """Complete the child's work before the parent's first liveness observation."""
            with client.area.journal.writer():
                engine = WorkflowEngine(client.area, FakeTerminal())
                engine.tick()
                assert engine.state["retired"]
            return SimpleNamespace(returncode=0, poll=lambda: 0)

        monkeypatch.setattr(
            supervisor,
            "subprocess",
            SimpleNamespace(Popen=popen, DEVNULL=supervisor.subprocess.DEVNULL),
        )
        supervisor.start(client.area)
        assert client.area.journal.replay()[0]["retired"]
        assert not supervisor.running(client.area)
