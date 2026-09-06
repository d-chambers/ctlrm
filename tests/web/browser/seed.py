"""Seed real disposable browser records without starting providers or a supervisor."""

import json
import os
from pathlib import Path
import subprocess
import sys

import ctlrm
from ctlrm.managed.projects import ProjectStore
from ctlrm.managed.storage import now
from ctlrm.managed.workflows import WorkflowEngine
from ctlrm.runtime.messages import MailboxMessage
from ctlrm.runtime.workflows import WorkflowTemplate


def git(root: Path, *args: str) -> str:
    """Commit fixture code with isolated identity and captured output."""
    return subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            *args,
        ],
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


def seed(base: Path) -> dict:
    """Create one completed implementation and one pending human review."""
    installed = Path(ctlrm.__file__).resolve()
    if os.environ.get("CTLRM_BROWSER_REQUIRE_WHEEL") == "1":
        assert installed.is_relative_to(Path(sys.prefix)), "browser smoke imported the source tree"
    root = base / "code"
    root.mkdir()
    (root / "source.txt").write_text("initial\n")
    git(root, "init", "-b", "main")
    git(root, "add", "source.txt")
    git(root, "commit", "-m", "Initial")
    store = ProjectStore(base / "data")
    store.create(
        root,
        "Browser acceptance",
        ["Read exact task evidence"],
        project_id="demo",
        design="# Design\n\nKeep task evidence readable.",
    )
    template = WorkflowTemplate.parse("""name: browser-review
entry: implement
profiles: {}
roles: {writer: {kind: human}, reviewer: {kind: human}}
tasks:
  implement:
    role: writer
    instructions: Implement the goal and record findings.
    verify_input: false
    transitions: {done: review}
  review:
    role: reviewer
    instructions: Review the supplied version.
    verify_input: true
    transitions: {approved: 'terminal:completed', changes_requested: implement}
""")
    store.add_job(
        "demo", "Readable evidence", "Show task commits and messages", template, job_id="job"
    )
    store.assign_pr("demo", "job", 42, "example/project")
    client, _ = store.start_job("demo", "job")
    with client.area.journal.writer():
        engine = WorkflowEngine(client.area)
        engine.tick()
        client.join("writer")
        engine.tick()
        execution = engine.active()
        owner = {
            "run_id": engine.state["run"]["id"],
            "execution_id": execution["id"],
            "participant": "writer",
            "input_artifact": None,
        }
        note = MailboxMessage(
            id="note",
            from_="writer",
            to="coordinator",
            kind="message",
            title="Implementation findings <script>",
            status="new",
            created_at=now(),
            body="Ready for review.\n\n<script>window.injection=true</script>\n\nCheck boundaries.",
        )
        request = client.request(
            "workflow-message", {**owner, "message": note.model_dump(by_alias=True)}
        )
        engine.tick()
        assert engine.state["requests"][request]["status"] == "accepted"
        (client.area.root / "source.txt").write_text("implemented\n")
        git(client.area.root, "add", "source.txt")
        git(
            client.area.root,
            "commit",
            "-m",
            "Retain task evidence",
            "-m",
            "Keep execution histories separate.",
        )
        client.request("workflow-ack", owner)
        engine.tick()
        request = client.request(
            "workflow-report",
            {**owner, "outcome": "done", "summary": "Ready for the next reviewer."},
        )
        engine.tick()
        assert engine.state["requests"][request]["status"] == "accepted"
    return {"data": str(store.root), "installed_from": str(installed)}


if __name__ == "__main__":
    print(json.dumps(seed(Path(sys.argv[1]))))
