"""Projects own independent jobs, central history, and searchable PR identities."""

from pathlib import Path
import subprocess

import pytest
from typer.testing import CliRunner

from ctlrm.__main__ import app
from ctlrm.managed.area import Area
from ctlrm.managed.projects import ProjectStore
from ctlrm.managed.workflows import WorkflowEngine, WorkflowService
from ctlrm.runtime.workflows import WorkflowTemplate
from conftest import FakeTerminal


@pytest.fixture
def human_template():
    """Complete jobs through the real human acknowledgment/report path."""
    return WorkflowTemplate.model_validate(
        {
            "name": "single",
            "entry": "work",
            "profiles": {},
            "roles": {"owner": {"kind": "human"}},
            "tasks": {
                "work": {
                    "role": "owner",
                    "instructions": "Review the goal",
                    "verify_input": False,
                    "transitions": {"done": "terminal:completed"},
                }
            },
        }
    )


@pytest.fixture
def store(tmp_path, repository):
    """Use a private central directory and real Git codebase."""
    result = ProjectStore(tmp_path / "data")
    result.create(repository, "Improve auth", ["Rotate tokens"], project_id="auth")
    return result


def finish(client):
    """Drive one human task to completion without a provider process."""
    client.join("owner")
    with client.area.journal.writer():
        engine = WorkflowEngine(client.area, FakeTerminal())
        engine.tick()
        item = engine.active()
        payload = {
            "run_id": engine.state["run"]["id"],
            "execution_id": item["id"],
            "participant": "owner",
        }
        client.request("workflow-ack", payload)
        engine.tick()
        client.request("workflow-report", {**payload, "outcome": "done", "summary": "Complete"})
        engine.tick()
        assert engine.state["run"]["status"] == "completed"


class TestProjects:
    """Planning, launches, and metadata retain ownership boundaries."""

    def test_central_handoff_and_dependency(self, store, human_template) -> None:
        """Dependencies block early start; each job then owns a distinct worktree/runtime."""
        store.add_job("auth", "Core", "Implement rotation", human_template, job_id="core")
        store.add_job(
            "auth",
            "Clients",
            "Migrate callers",
            human_template,
            job_id="clients",
            depends_on=["core"],
        )
        with pytest.raises(ValueError, match="dependency"):
            store.start_job("auth", "clients")
        client, request = store.start_job("auth", "core")
        assert client.area.root != Path(store.project("auth").codebase)
        assert client.area.room.root == store.job_path("auth", "core") / "runtime"
        assert (client.area.root / ".scratch/ctlrm").resolve() == store.job_path("auth", "core")
        assert not (client.area.root / ".ctlrm").is_symlink()
        assert Area.load(client.area.root).data == client.area.data
        again, same = store.start_job("auth", "core")
        assert same == request
        finish(client)
        other, _ = store.start_job("auth", "clients")
        assert other.area.data["id"] != client.area.data["id"]
        assert store.job_status("auth", "core")["status"] == "completed"
        assert store.status("auth")["status"] == "active"
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
        assert store.job_status("auth", "core")["status"] == "completed"

    def test_invalid_dependencies_and_foreign_tree(self, store, human_template, tmp_path) -> None:
        """Plans cannot contain cycles or bind work from another codebase."""
        with pytest.raises(ValueError):
            store.add_job("auth", "Bad", "x", human_template, job_id="bad", depends_on=["bad"])
        with pytest.raises(FileNotFoundError):
            store.add_job("auth", "Bad", "x", human_template, job_id="bad", depends_on=["missing"])
        store.add_job("auth", "Core", "x", human_template, job_id="core")
        other = tmp_path / "foreign"
        other.mkdir()
        subprocess.run(["git", "init", str(other)], check=True, capture_output=True)
        with pytest.raises(ValueError, match="another codebase"):
            store.start_job("auth", "core", root=other)
        assert store.job_status("auth", "core")["status"] == "planned"

    def test_pr_assignment_and_search(self, store, human_template, repository) -> None:
        """Repository filters disambiguate duplicate PR numbers and corrections retain history."""
        store.add_job("auth", "A", "x", human_template, job_id="a")
        store.add_job("auth", "B", "x", human_template, job_id="b")
        store.assign_pr("auth", "a", 42, "Owner/Repo", url="https://github.com/Owner/Repo/pull/42")
        store.assign_pr("auth", "b", 42, "Owner/Other")
        assert len(store.find_pr(42)) == 2
        assert store.find_pr(42, repository="owner/repo")[0]["job"]["id"] == "a"
        store.assign_pr("auth", "a", 43, "owner/repo")
        assert len(store.find_pr(42)) == 1
        assert store.journal("auth").replay()[1] == 3
        with pytest.raises(ValueError):
            store.assign_pr("auth", "a", 0, "owner/repo")
        with pytest.raises(ValueError):
            store.assign_pr("auth", "a", 1, "../repo")

    def test_scratch_collision(self, store, human_template, repository, tmp_path) -> None:
        """Existing scratch data is never replaced by a managed link."""
        root = tmp_path / "work"
        subprocess.run(
            ["git", "-C", str(repository), "worktree", "add", "-b", "work", str(root)],
            check=True,
            capture_output=True,
        )
        (root / ".scratch/ctlrm").mkdir(parents=True)
        store.add_job("auth", "A", "x", human_template, job_id="a")
        with pytest.raises(ValueError, match="already exists"):
            store.start_job("auth", "a", root=root)
        assert (root / ".scratch/ctlrm").is_dir()
        assert not (root / ".scratch/ctlrm").is_symlink()

    def test_cli_pr_lookup(self, store, human_template, monkeypatch) -> None:
        """The CLI can attach and find a PR without launching any job."""
        monkeypatch.setenv("CTLRM_DATA_HOME", str(store.root))
        store.add_job("auth", "A", "x", human_template, job_id="a")
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "job",
                "assign-pr",
                "--project",
                "auth",
                "--job",
                "a",
                "--number",
                "42",
                "--repository",
                "owner/repo",
            ],
        )
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["job", "find", "--pr", "42"])
        assert result.exit_code == 0, result.output
        assert '"number":42' in result.output
        assert store.job_status("auth", "a")["status"] == "planned"


class TestCentralProvider:
    """Native launch and recovery retain narrowly scoped central storage access."""

    def test_native_profile_and_recovery(self, store, repository, monkeypatch) -> None:
        """The session can acknowledge from central storage without granting all project data."""
        import json
        import sys

        monkeypatch.setenv("CODEX_HOME", str(repository))
        template = WorkflowTemplate.model_validate(
            {
                "name": "native",
                "entry": "work",
                "profiles": {
                    "coder": {
                        "provider": "codex",
                        "executable": sys.executable,
                        "context": str(repository),
                        "input_mode": "native",
                        "arguments": ["--sandbox", "workspace-write"],
                    }
                },
                "roles": {"coder": {"kind": "agent", "profile": "coder"}},
                "tasks": {
                    "work": {
                        "role": "coder",
                        "verify_input": False,
                        "transitions": {"done": "terminal:completed"},
                    }
                },
            }
        )
        store.add_job("auth", "Native", "Implement goal", template, job_id="native")
        client, _ = store.start_job("auth", "native")
        with client.area.journal.writer():
            engine = WorkflowEngine(client.area, FakeTerminal())
            engine.tick()
            session = next(iter(engine.state["sessions"].values()))
            config = json.loads(session["spec"]["argv"][-1])
            assert config["arguments"][-2:] == ["--add-dir", str(store.job_path("auth", "native"))]
            assert ".scratch/ctlrm/runtime/participants/" in config["bootstrap"]
            client.ready(session["id"], 1, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
            engine.tick()
            session = engine.state["sessions"][session["id"]]
            assert session["ready"]
            assert (
                json.loads(session["recovery"]["resume_argv"][-1])["arguments"]
                == config["arguments"]
            )

    def test_broken_locator_does_not_fallback(self, store, human_template) -> None:
        """A missing central scratch link cannot silently start a second local runtime."""
        store.add_job("auth", "A", "x", human_template, job_id="a")
        client, _ = store.start_job("auth", "a")
        (client.area.root / ".scratch/ctlrm").unlink()
        with pytest.raises(ValueError, match="scratch link"):
            Area.load(client.area.root)
        assert store.job_status("auth", "a")["status"] == "starting"


class TestBindingRecovery:
    """Damaged bindings and failed provisioning never create another runtime."""

    def test_missing_locator(self, store, human_template) -> None:
        """A remaining central link prevents local fallback and start repairs the binding."""
        client = self.started(store, human_template)
        original = client.area.data["id"]
        (client.area.root / ".ctlrm/location.json").unlink()
        with pytest.raises(ValueError, match="locator is missing"):
            WorkflowService.submit(client.area.root, human_template, "Other", "Other")
        repaired, _ = store.start_job("auth", "a")
        assert repaired.area.data["id"] == original

    @staticmethod
    def started(store, template):
        """Create one centrally bound human job."""
        store.add_job("auth", "A", "x", template, job_id="a")
        return store.start_job("auth", "a")[0]

    def test_cli_with_broken_link(self, store, human_template, monkeypatch) -> None:
        """Central commands still work from a worktree needing binding repair."""
        client = self.started(store, human_template)
        monkeypatch.setenv("CTLRM_DATA_HOME", str(store.root))
        monkeypatch.chdir(client.area.root)
        (client.area.root / ".scratch/ctlrm").unlink()
        result = CliRunner().invoke(app, ["job", "status", "--project", "auth", "--job", "a"])
        assert result.exit_code == 0, result.output
        store.start_job("auth", "a")
        assert Area.load(client.area.root).data["id"] == client.area.data["id"]

    def test_branch_collision(self, store, human_template) -> None:
        """A rejected branch choice does not bind the planned job permanently."""
        store.add_job("auth", "A", "x", human_template, job_id="a")
        with pytest.raises(ValueError, match="branch already exists"):
            store.start_job("auth", "a", branch="main")
        assert store.job_status("auth", "a")["status"] == "planned"
        client, _ = store.start_job("auth", "a", branch="available")
        assert client.area.data["branch"] == "refs/heads/available"

    def test_legacy_scratch_remains_versioned(self, repository) -> None:
        """Only a verified central link is excluded from artifact capture."""
        from ctlrm.managed.artifacts import fingerprint

        scratch = repository / ".scratch/ctlrm/review.txt"
        scratch.parent.mkdir(parents=True)
        scratch.write_text("first")
        before = fingerprint(repository)
        scratch.write_text("second")
        assert fingerprint(repository) != before
