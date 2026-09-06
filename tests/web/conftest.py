"""Real disposable projects with a deterministic single-writer human workflow."""

from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient

from ctlrm.managed.projects import ProjectStore
from ctlrm.managed.sessions import SessionService
from ctlrm.managed.workflows import WorkflowEngine
from ctlrm.web.app import create_app


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """Create a committed local repository isolated from user data and Git identity."""
    root = tmp_path / "code"
    root.mkdir()
    (root / "source.txt").write_text("initial\n")
    for args in (["init", "-b", "main"], ["add", "source.txt"], ["commit", "-m", "Initial"]):
        subprocess.run(
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
            check=True,
            capture_output=True,
        )
    return root


@pytest.fixture
def browser(tmp_path, repository, monkeypatch):
    """Use real storage and workflow transitions without spawning a background supervisor."""
    store = ProjectStore(tmp_path / "data")
    app = create_app(store, token="test-capability", port=8766)
    original = SessionService.await_result

    def apply(client, request_id, timeout=30):
        """Run the same writer used by the supervisor before observing acceptance."""
        with client.area.journal.writer():
            WorkflowEngine(client.area).tick()
        return original(client, request_id, timeout=0.2)

    monkeypatch.setattr("ctlrm.web.api.supervisor.start", lambda area: None)
    monkeypatch.setattr(SessionService, "await_result", apply)
    with TestClient(
        app, base_url="http://127.0.0.1:8766", headers={"Authorization": "Bearer test-capability"}
    ) as client:
        yield client, store


@pytest.fixture
def planned(browser, repository):
    """Plan an implementation and version-verified human review using the HTTP surface."""
    client, store = browser
    response = client.post(
        "/api/projects",
        json={
            "project_id": "project",
            "root": str(repository),
            "name": "Improve code",
            "goals": ["Make it clear"],
            "design": "# Design\n\nTwo tasks.",
        },
    )
    assert response.status_code == 200, response.text
    template = """name: human-review
entry: implement
profiles: {}
roles:
  writer: {kind: human}
  reviewer: {kind: human}
tasks:
  implement:
    role: writer
    instructions: Implement the goal
    verify_input: false
    transitions: {done: review}
  review:
    role: reviewer
    instructions: Review the exact input
    verify_input: true
    transitions: {approved: 'terminal:completed', changes_requested: implement}
"""
    response = client.post(
        "/api/projects/project/jobs",
        json={
            "job_id": "job",
            "title": "First PR",
            "instructions": "Improve this code",
            "workflow_yaml": template,
        },
    )
    assert response.status_code == 200, response.text
    return client, store
