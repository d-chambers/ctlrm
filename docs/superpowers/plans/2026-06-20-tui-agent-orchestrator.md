# TUI Agent Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first working Python vertical slice of `ctlrm`: a tmux-backed Textual TUI workbench with registry loading, project-local `.ctlrm` participant mailboxes/state, declarative workflow routing, and a read-only project/workflow view.

**Architecture:** Create a focused Python package with separate modules for registry, project runtime files, participants, tmux command construction, workflow scheduling, workspace state, and Textual rendering. Keep tmux process execution behind a small runner boundary so most behavior is unit-testable without tmux. Implement a minimal TUI shell after the data model and file/runtime behavior are tested.

**Tech Stack:** Python 3.11+, `uv`, `textual`, `pydantic`, `tomli-w` or `tomlkit`, `PyYAML`, `pytest`, `pytest-cov`, `ruff`, and standard library filesystem/subprocess APIs.

---

## File Structure

- `pyproject.toml`: package metadata, dependencies, CLI entry point, test/lint configuration.
- `src/ctlrm/__init__.py`: package marker and version.
- `src/ctlrm/__main__.py`: `python -m ctlrm` entry point.
- `src/ctlrm/app.py`: Textual app composition and key bindings.
- `src/ctlrm/models.py`: domain models: projects, participants, providers, states, tmux targets.
- `src/ctlrm/registry.py`: user registry TOML load/save with atomic writes.
- `src/ctlrm/runtime/__init__.py`: project-local `.ctlrm` initialization facade.
- `src/ctlrm/runtime/messages.py`: Markdown with YAML front matter mailbox parsing/writing.
- `src/ctlrm/runtime/state.py`: participant `state.md` parsing/writing.
- `src/ctlrm/runtime/workflows.py`: declarative workflow graph parsing and validation.
- `src/ctlrm/scheduler.py`: active-participant and message routing decisions.
- `src/ctlrm/tmux.py`: tmux target parsing, command construction, command runner boundary.
- `src/ctlrm/workspace.py`: UI-independent workspace state composition.
- `tests/`: focused pytest tests for each module.

## Task 1: Bootstrap Python Package

**Files:**
- Create: `pyproject.toml`
- Create: `src/ctlrm/__init__.py`
- Create: `src/ctlrm/__main__.py`
- Test: `tests/test_bootstrap.py`

- [ ] **Step 1: Write bootstrap test**

Create `tests/test_bootstrap.py`:

```python
import ctlrm


def test_package_imports() -> None:
    assert ctlrm.__version__ == "0.1.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_bootstrap.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'ctlrm'`.

- [ ] **Step 3: Create package metadata**

Write `pyproject.toml`:

```toml
[project]
name = "ctlrm"
version = "0.1.0"
description = "TUI workbench for orchestrating CLI agents and human workflow participants"
requires-python = ">=3.11"
dependencies = [
    "pydantic>=2.7",
    "pyyaml>=6.0",
    "textual>=0.70",
    "tomli-w>=1.0",
]

[project.scripts]
ctlrm = "ctlrm.__main__:main"

[dependency-groups]
dev = [
    "pytest>=8.2",
    "pytest-cov>=5.0",
    "ruff>=0.5",
]

[tool.ruff]
line-length = 100
src = ["src", "tests"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

Create `src/ctlrm/__init__.py`:

```python
__version__ = "0.1.0"
```

Create `src/ctlrm/__main__.py`:

```python
def main() -> None:
    print("ctlrm bootstrap")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Verify bootstrap passes**

Run: `uv run pytest tests/test_bootstrap.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/ctlrm/__init__.py src/ctlrm/__main__.py tests/test_bootstrap.py
git commit -m "chore: bootstrap python package"
```

## Task 2: Domain Models

**Files:**
- Create: `src/ctlrm/models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write model tests**

Create `tests/test_models.py`:

```python
from pathlib import Path

from ctlrm.models import Participant, ParticipantKind, ParticipantState, Provider, TmuxTarget


def test_human_participant_has_no_provider_or_tmux() -> None:
    participant = Participant.human("derrick", "Derrick", "product-owner", Path("/repo"))

    assert participant.kind is ParticipantKind.HUMAN
    assert participant.provider is None
    assert participant.tmux_target is None
    assert participant.state is ParticipantState.IDLE


def test_cli_agent_records_provider_and_tmux_target() -> None:
    participant = Participant.agent(
        "codex-impl",
        "Codex",
        "implementer",
        Provider.CODEX,
        Path("/repo"),
        TmuxTarget(session="ctlrm", window="agents", pane="%12"),
    )

    assert participant.kind is ParticipantKind.AGENT
    assert participant.provider is Provider.CODEX
    assert participant.tmux_target is not None
    assert participant.tmux_target.selector == "ctlrm:agents.%12"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'ctlrm.models'`.

- [ ] **Step 3: Implement models**

Create `src/ctlrm/models.py`:

```python
from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field


class ParticipantKind(str, Enum):
    AGENT = "agent"
    HUMAN = "human"


class Provider(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"
    PI = "pi"


class ParticipantState(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    MISSING = "missing"
    UNMANAGED = "unmanaged"


class TmuxTarget(BaseModel):
    session: str
    window: str
    pane: str

    @property
    def selector(self) -> str:
        return f"{self.session}:{self.window}.{self.pane}"


class Participant(BaseModel):
    id: str
    name: str
    role: str
    kind: ParticipantKind
    provider: Provider | None = None
    workdir: Path
    tmux_target: TmuxTarget | None = None
    state: ParticipantState = ParticipantState.IDLE

    @classmethod
    def human(cls, id: str, name: str, role: str, workdir: Path) -> "Participant":
        return cls(id=id, name=name, role=role, kind=ParticipantKind.HUMAN, workdir=workdir)

    @classmethod
    def agent(
        cls,
        id: str,
        name: str,
        role: str,
        provider: Provider,
        workdir: Path,
        tmux_target: TmuxTarget | None = None,
    ) -> "Participant":
        return cls(
            id=id,
            name=name,
            role=role,
            kind=ParticipantKind.AGENT,
            provider=provider,
            workdir=workdir,
            tmux_target=tmux_target,
        )


class Project(BaseModel):
    id: str
    name: str
    root: Path
    tmux_session: str | None = None
    participants: list[Participant] = Field(default_factory=list)
```

- [ ] **Step 4: Verify model tests pass**

Run: `uv run pytest tests/test_models.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ctlrm/models.py tests/test_models.py
git commit -m "feat: add participant domain models"
```

## Task 3: Registry TOML Load And Save

**Files:**
- Create: `src/ctlrm/registry.py`
- Test: `tests/test_registry.py`

- [ ] **Step 1: Write registry tests**

Create `tests/test_registry.py`:

```python
from pathlib import Path

from ctlrm.models import ParticipantKind, Provider
from ctlrm.registry import Registry


def test_loads_participants_from_toml() -> None:
    data = """
[[projects]]
id = "ctlrm"
name = "ctlrm"
root = "/repo"
tmux_session = "ctlrm"

[[projects.participants]]
id = "codex-impl"
name = "Codex"
role = "implementer"
kind = "agent"
provider = "codex"
workdir = "/repo"

[[projects.participants]]
id = "derrick"
name = "Derrick"
role = "product-owner"
kind = "human"
workdir = "/repo"
"""

    registry = Registry.from_toml(data)

    assert len(registry.projects) == 1
    assert registry.projects[0].participants[0].provider is Provider.CODEX
    assert registry.projects[0].participants[1].kind is ParticipantKind.HUMAN


def test_missing_registry_loads_empty(tmp_path: Path) -> None:
    registry = Registry.load(tmp_path / "registry.toml")

    assert registry.projects == []


def test_save_atomic_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "registry.toml"
    registry = Registry(projects=[])

    registry.save_atomic(path)

    assert Registry.load(path) == registry
    assert not path.with_suffix(".toml.tmp").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_registry.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'ctlrm.registry'`.

- [ ] **Step 3: Implement registry**

Create `src/ctlrm/registry.py`:

```python
from __future__ import annotations

import os
import tomllib
from pathlib import Path

import tomli_w
from pydantic import BaseModel, Field

from ctlrm.models import Project


class Registry(BaseModel):
    projects: list[Project] = Field(default_factory=list)

    @classmethod
    def from_toml(cls, data: str) -> "Registry":
        return cls.model_validate(tomllib.loads(data))

    @classmethod
    def load(cls, path: Path) -> "Registry":
        if not path.exists():
            return cls()
        return cls.from_toml(path.read_text())

    def to_toml(self) -> str:
        return tomli_w.dumps(self.model_dump(mode="json", exclude_none=True))

    def save_atomic(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(self.to_toml())
        os.replace(tmp_path, path)
```

- [ ] **Step 4: Verify registry tests pass**

Run: `uv run pytest tests/test_registry.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ctlrm/registry.py tests/test_registry.py
git commit -m "feat: add participant registry"
```

## Task 4: Project Runtime Directory And Front Matter Files

**Files:**
- Create: `src/ctlrm/runtime/__init__.py`
- Create: `src/ctlrm/runtime/messages.py`
- Create: `src/ctlrm/runtime/state.py`
- Test: `tests/test_runtime.py`

- [ ] **Step 1: Write runtime tests**

Create `tests/test_runtime.py`:

```python
from pathlib import Path

from ctlrm.runtime import ProjectRuntime
from ctlrm.runtime.messages import MailboxMessage
from ctlrm.runtime.state import ParticipantStateFile


def test_initializes_ctlrm_directory(tmp_path: Path) -> None:
    runtime = ProjectRuntime(tmp_path)

    runtime.init_project()
    runtime.init_participant("derrick")

    assert (tmp_path / ".ctlrm" / "participants" / "derrick" / "inbox").is_dir()
    assert (tmp_path / ".ctlrm" / "participants" / "derrick" / "outbox").is_dir()
    assert (tmp_path / ".ctlrm" / "workflows").is_dir()
    assert (tmp_path / ".ctlrm" / "runs").is_dir()


def test_parses_mailbox_message() -> None:
    message = MailboxMessage.parse(
        """---
id: msg-1
from: codex-impl
to: derrick
kind: question
title: Need decision
status: new
created_at: 2026-06-20T16:30:00+02:00
files:
  - src/ctlrm/tmux.py
---
Should restart reuse the same pane?
"""
    )

    assert message.id == "msg-1"
    assert message.to == "derrick"
    assert message.body.strip() == "Should restart reuse the same pane?"


def test_parses_participant_state_file() -> None:
    state = ParticipantStateFile.parse(
        """---
participant_id: derrick
kind: human
state: idle
active: false
updated_at: 2026-06-20T16:30:00+02:00
summary: Waiting for review
---
No active task.
"""
    )

    assert state.participant_id == "derrick"
    assert state.kind == "human"
    assert state.active is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_runtime.py -q`

Expected: FAIL with missing runtime modules.

- [ ] **Step 3: Implement project runtime facade**

Create `src/ctlrm/runtime/__init__.py`:

```python
from pathlib import Path


class ProjectRuntime:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.root = project_root / ".ctlrm"

    def init_project(self) -> None:
        (self.root / "participants").mkdir(parents=True, exist_ok=True)
        (self.root / "workflows").mkdir(parents=True, exist_ok=True)
        (self.root / "runs").mkdir(parents=True, exist_ok=True)

    def init_participant(self, participant_id: str) -> None:
        participant_root = self.root / "participants" / participant_id
        (participant_root / "inbox").mkdir(parents=True, exist_ok=True)
        (participant_root / "outbox").mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 4: Implement mailbox message parsing**

Create `src/ctlrm/runtime/messages.py`:

```python
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class MailboxMessage(BaseModel):
    id: str
    from_: str = Field(alias="from")
    to: str
    kind: str
    title: str
    status: str
    created_at: str
    workflow_id: str | None = None
    run_id: str | None = None
    node_id: str | None = None
    files: list[str] = Field(default_factory=list)
    body: str = ""

    @classmethod
    def parse(cls, text: str) -> "MailboxMessage":
        front_matter, body = _split_front_matter(text)
        message = cls.model_validate(yaml.safe_load(front_matter))
        message.body = body
        return message

    @classmethod
    def read(cls, path: Path) -> "MailboxMessage":
        return cls.parse(path.read_text())


def _split_front_matter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        raise ValueError("missing YAML front matter")
    try:
        front_matter, body = text[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise ValueError("front matter is not closed") from exc
    return front_matter, body
```

- [ ] **Step 5: Implement participant state parsing**

Create `src/ctlrm/runtime/state.py`:

```python
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from ctlrm.runtime.messages import _split_front_matter


class ParticipantStateFile(BaseModel):
    participant_id: str
    kind: str
    state: str
    active: bool
    updated_at: str
    current_run_id: str | None = None
    current_node_id: str | None = None
    summary: str | None = None
    body: str = ""

    @classmethod
    def parse(cls, text: str) -> "ParticipantStateFile":
        front_matter, body = _split_front_matter(text)
        state = cls.model_validate(yaml.safe_load(front_matter))
        state.body = body
        return state

    @classmethod
    def read(cls, path: Path) -> "ParticipantStateFile":
        return cls.parse(path.read_text())
```

- [ ] **Step 6: Verify runtime tests pass**

Run: `uv run pytest tests/test_runtime.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/ctlrm/runtime tests/test_runtime.py
git commit -m "feat: add project runtime files"
```

## Task 5: Declarative Workflow Graphs And Scheduler

**Files:**
- Create: `src/ctlrm/runtime/workflows.py`
- Create: `src/ctlrm/scheduler.py`
- Test: `tests/test_workflows.py`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Write workflow and scheduler tests**

Create `tests/test_workflows.py`:

```python
import pytest

from ctlrm.runtime.workflows import WorkflowGraph


WORKFLOW = """
id: implement_review_fix
title: Implement, review, and fix
nodes:
  implement:
    participant: codex-impl
  review:
    participant: derrick
edges:
  - from: implement
    to: review
    when: result
  - from: review
    to: done
    when: approved
"""


def test_parses_workflow_with_human_participant_node() -> None:
    workflow = WorkflowGraph.parse(WORKFLOW)

    assert workflow.nodes["review"].participant == "derrick"


def test_rejects_unknown_edge_node() -> None:
    with pytest.raises(ValueError, match="unknown to node"):
        WorkflowGraph.parse(
            """
id: broken
title: Broken
nodes:
  implement:
    participant: codex-impl
edges:
  - from: implement
    to: review
    when: result
"""
        )
```

Create `tests/test_scheduler.py`:

```python
from ctlrm.runtime.workflows import WorkflowGraph
from ctlrm.scheduler import next_participant


def test_routes_matching_edge_to_next_participant() -> None:
    workflow = WorkflowGraph.parse(
        """
id: implement_review_fix
title: Implement, review, and fix
nodes:
  implement:
    participant: codex-impl
  review:
    participant: derrick
edges:
  - from: implement
    to: review
    when: result
"""
    )

    assert next_participant(workflow, "implement", "result") == "derrick"


def test_done_edge_returns_none() -> None:
    workflow = WorkflowGraph.parse(
        """
id: review
title: Review
nodes:
  review:
    participant: derrick
edges:
  - from: review
    to: done
    when: approved
"""
    )

    assert next_participant(workflow, "review", "approved") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_workflows.py tests/test_scheduler.py -q`

Expected: FAIL with missing workflow and scheduler modules.

- [ ] **Step 3: Implement workflow graph parser**

Create `src/ctlrm/runtime/workflows.py`:

```python
from __future__ import annotations

import yaml
from pydantic import BaseModel, Field


class WorkflowNode(BaseModel):
    participant: str


class WorkflowEdge(BaseModel):
    from_: str = Field(alias="from")
    to: str
    when: str


class WorkflowGraph(BaseModel):
    id: str
    title: str
    nodes: dict[str, WorkflowNode]
    edges: list[WorkflowEdge] = Field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> "WorkflowGraph":
        workflow = cls.model_validate(yaml.safe_load(text))
        workflow.validate_graph()
        return workflow

    def validate_graph(self) -> None:
        for edge in self.edges:
            if edge.from_ not in self.nodes:
                raise ValueError(f"edge references unknown from node {edge.from_}")
            if edge.to != "done" and edge.to not in self.nodes:
                raise ValueError(f"edge references unknown to node {edge.to}")
```

- [ ] **Step 4: Implement scheduler**

Create `src/ctlrm/scheduler.py`:

```python
from ctlrm.runtime.workflows import WorkflowGraph


def next_participant(workflow: WorkflowGraph, current_node: str, event: str) -> str | None:
    for edge in workflow.edges:
        if edge.from_ == current_node and edge.when == event:
            if edge.to == "done":
                return None
            return workflow.nodes[edge.to].participant
    return None
```

- [ ] **Step 5: Verify tests pass**

Run: `uv run pytest tests/test_workflows.py tests/test_scheduler.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/ctlrm/runtime/workflows.py src/ctlrm/scheduler.py tests/test_workflows.py tests/test_scheduler.py
git commit -m "feat: add declarative workflow routing"
```

## Task 6: Tmux Adapter Boundary

**Files:**
- Create: `src/ctlrm/tmux.py`
- Test: `tests/test_tmux.py`

- [ ] **Step 1: Write tmux adapter tests**

Create `tests/test_tmux.py`:

```python
from ctlrm.tmux import TmuxCommand, TmuxTargetRef


def test_formats_send_keys_command() -> None:
    target = TmuxTargetRef(session="ctlrm", window="agents", pane="%12")

    command = TmuxCommand.send_keys(target, "hello")

    assert command.program == "tmux"
    assert command.args == ["send-keys", "-t", "ctlrm:agents.%12", "hello", "Enter"]


def test_formats_capture_pane_command() -> None:
    target = TmuxTargetRef(session="ctlrm", window="agents", pane="%12")

    command = TmuxCommand.capture_pane(target)

    assert command.args == ["capture-pane", "-p", "-t", "ctlrm:agents.%12"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tmux.py -q`

Expected: FAIL with missing tmux module.

- [ ] **Step 3: Implement tmux command objects and runner**

Create `src/ctlrm/tmux.py`:

```python
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TmuxTargetRef:
    session: str
    window: str
    pane: str

    @property
    def selector(self) -> str:
        return f"{self.session}:{self.window}.{self.pane}"


@dataclass(frozen=True)
class TmuxCommand:
    program: str
    args: list[str]

    @classmethod
    def send_keys(cls, target: TmuxTargetRef, text: str) -> "TmuxCommand":
        return cls("tmux", ["send-keys", "-t", target.selector, text, "Enter"])

    @classmethod
    def capture_pane(cls, target: TmuxTargetRef) -> "TmuxCommand":
        return cls("tmux", ["capture-pane", "-p", "-t", target.selector])


class CommandRunner(Protocol):
    def run(self, command: TmuxCommand) -> str: ...


class SubprocessCommandRunner:
    def run(self, command: TmuxCommand) -> str:
        result = subprocess.run(
            [command.program, *command.args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout
```

- [ ] **Step 4: Verify tmux tests pass**

Run: `uv run pytest tests/test_tmux.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ctlrm/tmux.py tests/test_tmux.py
git commit -m "feat: add tmux command adapter"
```

## Task 7: Workspace State

**Files:**
- Create: `src/ctlrm/workspace.py`
- Test: `tests/test_workspace.py`

- [ ] **Step 1: Write workspace tests**

Create `tests/test_workspace.py`:

```python
from pathlib import Path

from ctlrm.models import Participant, Project
from ctlrm.workspace import Workspace


def test_selects_first_project_and_participant() -> None:
    project = Project(
        id="ctlrm",
        name="ctlrm",
        root=Path("/repo"),
        tmux_session="ctlrm",
        participants=[Participant.human("derrick", "Derrick", "product-owner", Path("/repo"))],
    )

    workspace = Workspace.from_projects([project])

    assert workspace.selected_project_id == "ctlrm"
    assert workspace.selected_participant_id == "derrick"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_workspace.py -q`

Expected: FAIL with missing workspace module.

- [ ] **Step 3: Implement workspace state**

Create `src/ctlrm/workspace.py`:

```python
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from ctlrm.models import Project


class Workspace(BaseModel):
    projects: list[Project]
    selected_project_id: str | None = None
    selected_participant_id: str | None = None
    selected_file: Path | None = None
    status: str | None = None

    @classmethod
    def from_projects(cls, projects: list[Project]) -> "Workspace":
        selected_project = projects[0] if projects else None
        selected_participant = selected_project.participants[0] if selected_project and selected_project.participants else None
        return cls(
            projects=projects,
            selected_project_id=selected_project.id if selected_project else None,
            selected_participant_id=selected_participant.id if selected_participant else None,
        )
```

- [ ] **Step 4: Verify workspace tests pass**

Run: `uv run pytest tests/test_workspace.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ctlrm/workspace.py tests/test_workspace.py
git commit -m "feat: add workspace state model"
```

## Task 8: Minimal Textual TUI

**Files:**
- Create: `src/ctlrm/app.py`
- Modify: `src/ctlrm/__main__.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write Textual app test**

Create `tests/test_app.py`:

```python
import pytest
from pathlib import Path

from ctlrm.app import CtlrmApp
from ctlrm.models import Participant, Project
from ctlrm.workspace import Workspace


@pytest.mark.asyncio
async def test_app_renders_human_participant() -> None:
    project = Project(
        id="ctlrm",
        name="ctlrm",
        root=Path("/repo"),
        participants=[Participant.human("derrick", "Derrick", "product-owner", Path("/repo"))],
    )
    app = CtlrmApp(Workspace.from_projects([project]))

    async with app.run_test() as pilot:
        assert "Derrick" in str(pilot.app.query_one("#participants").renderable)
```

- [ ] **Step 2: Add pytest asyncio dependency**

Modify `pyproject.toml` dev dependencies to include `pytest-asyncio>=0.23`:

```toml
[dependency-groups]
dev = [
    "pytest>=8.2",
    "pytest-asyncio>=0.23",
    "pytest-cov>=5.0",
    "ruff>=0.5",
]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_app.py -q`

Expected: FAIL with missing `ctlrm.app`.

- [ ] **Step 4: Implement minimal Textual app**

Create `src/ctlrm/app.py`:

```python
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Footer, Header, Static

from ctlrm.workspace import Workspace


class CtlrmApp(App[None]):
    CSS = """
    #main { height: 1fr; }
    #participants { width: 28; border: solid $primary; }
    #center { width: 1fr; }
    #file { height: 1fr; border: solid $primary; }
    #selected { height: 8; border: solid $primary; }
    #tree { width: 32; border: solid $primary; }
    """

    BINDINGS = [("ctrl+q", "quit", "Quit")]

    def __init__(self, workspace: Workspace) -> None:
        super().__init__()
        self.workspace = workspace

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            yield Static(self._participants_text(), id="participants")
            with Vertical(id="center"):
                yield Static("Read-only file viewer", id="file")
                yield Static("Selected participant panel", id="selected")
            yield Static("Project tree", id="tree")
        yield Footer()

    def _participants_text(self) -> str:
        if not self.workspace.projects:
            return "No participants"
        lines = []
        for participant in self.workspace.projects[0].participants:
            lines.append(f"{participant.name} · {participant.role} · {participant.kind.value}")
        return "\n".join(lines)
```

- [ ] **Step 5: Wire CLI entry point**

Replace `src/ctlrm/__main__.py` with:

```python
from pathlib import Path

from ctlrm.app import CtlrmApp
from ctlrm.models import Participant, Project
from ctlrm.workspace import Workspace


def main() -> None:
    cwd = Path.cwd()
    project = Project(
        id=cwd.name or "project",
        name=cwd.name or "project",
        root=cwd,
        tmux_session=cwd.name or None,
        participants=[Participant.human("you", "You", "human", cwd)],
    )
    CtlrmApp(Workspace.from_projects([project])).run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Verify app tests and import test pass**

Run: `uv run pytest tests/test_app.py tests/test_bootstrap.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/ctlrm/app.py src/ctlrm/__main__.py tests/test_app.py
git commit -m "feat: render initial textual workbench"
```

## Task 9: Final Verification

**Files:**
- Modify only if verification reveals defects.

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest -q`

Expected: all tests pass.

- [ ] **Step 2: Run lint**

Run: `uv run ruff check .`

Expected: no lint failures.

- [ ] **Step 3: Run the TUI manually**

Run: `uv run ctlrm`

Expected: Textual TUI opens with project header, participants panel, current file panel, selected participant panel, project tree panel, and footer. Press `Ctrl+Q` to quit.

- [ ] **Step 4: Commit final fixes if needed**

```bash
git status --short
git add pyproject.toml src tests docs
git commit -m "test: verify initial ctlrm vertical slice"
```

If there are no changes after verification, do not create an empty commit.

## Self-Review Notes

- Spec coverage: this plan covers the Python package baseline, Textual TUI baseline, participant model including humans, registry TOML, `.ctlrm` runtime directory, mailbox/state/workflow file formats, declarative scheduler, tmux command boundary, workspace model, and initial TUI layout.
- Deferred by design: deep provider-specific behavior, embedded workflow scripts, direct file editing, full file tree navigation, and real tmux integration tests. These are outside the approved v1 vertical slice or marked opt-in in the spec.
- Execution risk: the current workspace is not a Git repository, so commit steps require initializing or moving into a real repo before execution.
