"""The browser uses actual ownership and version checks, including hostile local requests."""

import hashlib
import subprocess
from pathlib import Path

import pytest

from ctlrm.web.service import read_file


def respond(client, action, outcome, request_id):
    """Submit the exact assignment observed in the browser's human queue."""
    return client.post(
        f"/api/projects/{action['project']}/jobs/{action['job']}/respond",
        json={
            "request_id": request_id,
            "run_id": action["run_id"],
            "execution_id": action["active"]["id"],
            "participant": action["name"],
            "input_artifact": action["active"]["input"].get("artifact"),
            "outcome": outcome,
            "summary": "Checked the goal and source",
        },
    )


class TestLocalBoundary:
    """No public site can read project state or control the user's terminal."""

    def test_capability(self, browser) -> None:
        """Even read APIs require the local capability; the shell contains no token."""
        client, _ = browser
        assert client.get("/api/snapshot", headers={"Authorization": ""}).status_code == 401
        shell = client.get("/")
        assert shell.status_code == 200
        assert "test-capability" not in shell.text
        assert "frame-ancestors 'none'" in shell.headers["Content-Security-Policy"]
        assert client.get("/api/snapshot").json()["projects"] == []

    @pytest.mark.parametrize(
        "headers",
        [{"Host": "attacker.test:8766"}, {"Origin": "https://attacker.test"}, {"Origin": "null"}],
    )
    def test_origin(self, browser, headers) -> None:
        """A capability does not excuse a cross-origin or rebinding request."""
        client, _ = browser
        assert client.get("/api/snapshot", headers=headers).status_code == 403
        assert client.post("/api/projects", json={}, headers=headers).status_code == 403

    def test_large_body(self, browser) -> None:
        """The gateway rejects oversized bodies before JSON parsing."""
        client, _ = browser
        assert client.post("/api/projects", content="x" * (2 * 1024 * 1024 + 1)).status_code == 413

    def test_socket_auth(self, browser) -> None:
        """WebSocket capability validation precedes any session lookup or PTY attachment."""
        client, _ = browser
        with client.websocket_connect(
            "/api/terminal/p/j/s/1",
            headers={"Origin": "http://127.0.0.1:8766", "Host": "127.0.0.1:8766"},
        ) as socket:
            socket.send_json({"token": "wrong"})
            assert socket.receive()["code"] == 1008


class TestProjectFlows:
    """Planning, handoff, PR lookup, and artifacts remain projections of the managed store."""

    def test_plan_without_launch(self, planned) -> None:
        """Reading a planned job never provisions a worktree or launches agents."""
        client, store = planned
        snapshot = client.get("/api/snapshot").json()
        assert len(snapshot["participants"]) == 2
        assert snapshot["actions"] == []
        assert snapshot["projects"][0]["jobs"][0]["status"] == "planned"
        assert not (store.root / "worktrees").exists()
        templates = client.get("/api/templates").json()
        assert {item["name"] for item in templates} == {"ship", "single-agent", "implement-review"}

    def test_handoff_and_artifact(self, planned) -> None:
        """Human responses route through the real graph and produce immutable reports."""
        client, store = planned
        assert client.post("/api/projects/project/jobs/job/start", json={}).status_code == 200
        first = client.get("/api/snapshot").json()["actions"][0]
        assert first["name"] == "writer"
        assert respond(client, first, "done", "implement").status_code == 200
        second = client.get("/api/snapshot").json()["actions"][0]
        assert second["name"] == "reviewer"
        reference = second["active"]["input"]["artifact"]
        manifest = client.get(f"/api/projects/project/jobs/job/artifacts/{reference}")
        assert manifest.status_code == 200
        assert (
            manifest.json()["version"]["files"]["source.txt"]["sha256"]
            == hashlib.sha256(b"initial\n").hexdigest()
        )
        assert respond(client, second, "approved", "approval").status_code == 200
        assert respond(client, second, "approved", "approval").status_code == 200
        assert client.get("/api/snapshot").json()["actions"] == []
        assert store.job_status("project", "job")["status"] == "completed"
        assert client.get("/api/projects/project/activity").json()
        pr = client.put(
            "/api/projects/project/jobs/job/pr", json={"number": 42, "repository": "owner/repo"}
        )
        assert pr.status_code == 200
        assert store.find_pr(42)[0]["job"]["id"] == "job"

    def test_stale_review(self, planned) -> None:
        """Changed code is neither shown as the reviewed version nor accepted for approval."""
        client, store = planned
        client.post("/api/projects/project/jobs/job/start", json={})
        first = client.get("/api/snapshot").json()["actions"][0]
        respond(client, first, "done", "implement")
        second = client.get("/api/snapshot").json()["actions"][0]
        reference = second["active"]["input"]["artifact"]
        root = Path(store.job_status("project", "job")["launch"]["root"])
        (root / "source.txt").write_text("changed after handoff\n")
        response = client.get(
            "/api/projects/project/jobs/job/file",
            params={"name": "source.txt", "artifact": reference},
        )
        assert response.status_code == 409
        assert "changed" in response.json()["error"]
        assert respond(client, second, "approved", "stale").status_code == 409
        assert (
            client.get("/api/snapshot").json()["actions"][0]["active"]["id"]
            == second["active"]["id"]
        )

    def test_stale_execution(self, planned) -> None:
        """A response from an old tab cannot report for a newer human assignment."""
        client, _ = planned
        client.post("/api/projects/project/jobs/job/start", json={})
        first = client.get("/api/snapshot").json()["actions"][0]
        assert respond(client, first, "done", "first").status_code == 200
        assert respond(client, first, "done", "different").status_code == 409

    def test_cancel_after_branch_drift(self, planned) -> None:
        """Branch drift pauses dispatch but must not disable the explicit cancel control."""
        client, store = planned
        client.post("/api/projects/project/jobs/job/start", json={})
        root = Path(store.job_status("project", "job")["launch"]["root"])
        subprocess.run(
            ["git", "-C", str(root), "checkout", "-b", "unexpected-branch"],
            check=True,
            capture_output=True,
        )
        response = client.post(
            "/api/projects/project/jobs/job/action",
            json={
                "action": "cancel",
                "request_id": "cancel-drift",
            },
        )
        assert response.status_code == 200, response.text
        assert store.job_status("project", "job")["status"] == "canceled"

    def test_bad_workflow(self, browser, repository) -> None:
        """Invalid YAML cannot publish a partially planned job."""
        client, store = browser
        store.create(repository, "Test", ["Goal"], project_id="p")
        response = client.post(
            "/api/projects/p/jobs",
            json={
                "job_id": "j",
                "title": "Bad",
                "instructions": "Goal",
                "workflow_yaml": "name: duplicate\nname: duplicate",
            },
        )
        assert response.status_code == 409
        assert store.status("p")["jobs"] == []


class TestCodeBoundary:
    """The viewer cannot follow symlinks, traverse paths, or serve arbitrary executable content."""

    @pytest.mark.parametrize("name", ["../secret", "/etc/passwd", "a/../../secret", "a//b"])
    def test_traversal(self, tmp_path, name) -> None:
        """Reject unsafe paths before opening a directory entry."""
        with pytest.raises(ValueError, match="safe relative"):
            read_file(tmp_path, name)

    def test_symlink(self, tmp_path) -> None:
        """Both file and directory links are refused, even when targeting readable files."""
        root = tmp_path / "root"
        root.mkdir()
        (tmp_path / "secret").write_text("private")
        (root / "file").symlink_to(tmp_path / "secret")
        (root / "directory").symlink_to(tmp_path, target_is_directory=True)
        for name in ("file", "directory/secret"):
            with pytest.raises(OSError):
                read_file(root, name)

    def test_manifest_integrity(self, planned) -> None:
        """Tampered manifests cannot masquerade as an accepted review artifact."""
        client, store = planned
        client.post("/api/projects/project/jobs/job/start", json={})
        first = client.get("/api/snapshot").json()["actions"][0]
        respond(client, first, "done", "first")
        reference = client.get("/api/snapshot").json()["actions"][0]["active"]["input"]["artifact"]
        path = store.job_path("project", "job") / "runtime/artifacts" / f"{reference}.json"
        path.write_text(path.read_text().replace('"dirty":false', '"dirty":true'))
        assert (
            client.get(f"/api/projects/project/jobs/job/artifacts/{reference}").status_code == 409
        )
