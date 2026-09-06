"""Task artifact text stays attributable and readable after later rounds and archival."""

from pathlib import Path
import subprocess

import pytest
from typer.testing import CliRunner

from ctlrm.__main__ import app
from ctlrm.managed.area import Area
from ctlrm.managed.storage import now
from ctlrm.managed.workflows import WorkflowService
from ctlrm.runtime.messages import MailboxMessage
from .test_app import respond

BASE = "/api/projects/project/jobs/job"


def commit(root: Path, title: str, body: str) -> str:
    """Commit a real code change with a multiline message using a disposable identity."""
    (root / "source.txt").write_text(body)
    subprocess.run(["git", "-C", str(root), "add", "source.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-m",
            title,
            "-m",
            "Detailed explanation\nwith a second line.",
        ],
        capture_output=True,
        check=True,
    )
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


def start(planned):
    """Start a real human task and register its sender without reporting completion."""
    client, store = planned
    assert client.post(BASE + "/start", json={}).status_code == 200
    action = client.get("/api/snapshot").json()["actions"][0]
    root = Path(store.job_status("project", "job")["launch"]["root"])
    service = WorkflowService(Area.load(root))
    service.join("writer")
    return client, store, action, root, service


def message(identifier="note", **kwargs):
    """Build a sent note whose literal markup must remain plain text."""
    return MailboxMessage(
        id=identifier,
        from_="writer",
        to="coordinator",
        title="Implementation note",
        kind="message",
        status="new",
        created_at=now(),
        body=kwargs.pop("body", "First line\n<script>window.injection=true</script>\nLast line"),
        **kwargs,
    )


class TestTaskArtifacts:
    """The public read APIs enforce task membership and use retained documents."""

    def test_rounds_and_archive(self, planned) -> None:
        """Later commits/messages cannot leak into an earlier visit, even after worktree removal."""
        client, store, first, root, service = start(planned)
        first_id = first["active"]["id"]
        service.send_message(message(), first_id)
        assert service.area.room.read_inbox("coordinator")[0].execution_id == first_id
        sha = commit(root, "Improve the implementation", "better\n")
        assert respond(client, first, "done", "first").status_code == 200
        review = client.get("/api/snapshot").json()["actions"][0]
        assert respond(client, review, "changes_requested", "changes").status_code == 200
        second = client.get("/api/snapshot").json()["actions"][0]
        second_id = second["active"]["id"]
        service.send_message(message("second-note", body="A later round"), second_id)
        later_sha = commit(root, "Follow up", "best\n")
        assert respond(client, second, "done", "second").status_code == 200
        review = client.get("/api/snapshot").json()["actions"][0]
        assert respond(client, review, "approved", "approved").status_code == 200
        assert client.post("/api/projects/project/archive", json={}).status_code == 200
        subprocess.run(["git", "-C", str(root), "worktree", "remove", str(root)], check=True)

        url = BASE + f"/executions/{first_id}/artifacts"
        listing = client.get(url).json()
        assert len(listing["items"]) == 3
        item = next(i for i in listing["items"] if i["kind"] == "commit")
        assert item["sha"] == sha
        text = client.get(url + f"/commit/{item['id']}").json()["text"]
        assert "Detailed explanation\n    with a second line." in text
        assert "+better" in text and later_sha not in text
        text = client.get(url + "/message/note").json()["text"]
        assert "To: coordinator" in text and "<script>window.injection=true</script>" in text
        assert "A later round" not in text
        report = client.get(url + f"/report/{first_id}").json()["text"]
        assert "Outcome: done" in report and "Checked the goal and source" in report
        second_items = client.get(BASE + f"/executions/{second_id}/artifacts").json()["items"]
        assert any(i.get("sha") == later_sha for i in second_items)
        assert not any(i["id"] == "note" for i in second_items)
        assert client.get(url + "/message/second-note").status_code == 409
        foreign = next(i for i in second_items if i.get("sha") == later_sha)
        assert client.get(url + f"/commit/{foreign['id']}").status_code == 409
        assert client.get(BASE + "/executions/missing/artifacts").status_code == 409
        assert client.get(url, headers={"Authorization": ""}).status_code == 401

    def test_stale_and_large_messages(self, planned) -> None:
        """Rejected messages are neither delivered nor attributed to another execution."""
        client, _, first, _, service = start(planned)
        with pytest.raises(ValueError, match="unknown or inactive"):
            service.send_message(message(), "old-execution")
        assert not service.area.room.read_inbox("coordinator")
        with pytest.raises(ValueError, match="exceeds"):
            service.send_message(message("large", body="x" * 16385), first["active"]["id"])
        assert not service.area.room.read_inbox("coordinator")
        assert (
            client.get(BASE + f"/executions/{first['active']['id']}/artifacts").json()["items"]
            == []
        )

    def test_cli_attribution(self, planned) -> None:
        """The real send command requires the assignment ID and records a viewable message."""
        client, _, action, root, _ = start(planned)
        args = [
            "--root",
            str(root),
            "send",
            "--from",
            "writer",
            "--to",
            "coordinator",
            "--title",
            "CLI note",
            "--body",
            "Full CLI body",
            "--id",
            "cli-note",
        ]
        missing = CliRunner().invoke(app, args)
        assert missing.exit_code == 2 and "--execution-id" in missing.output
        result = CliRunner().invoke(app, [*args, "--execution-id", action["active"]["id"]])
        assert result.exit_code == 0, result.output
        retry = CliRunner().invoke(app, [*args, "--execution-id", action["active"]["id"]])
        assert retry.exit_code == 0, retry.output
        url = BASE + f"/executions/{action['active']['id']}/artifacts/message/cli-note"
        assert "Full CLI body" in client.get(url).json()["text"]
        assert "sent_messages" not in client.get("/api/snapshot").text
