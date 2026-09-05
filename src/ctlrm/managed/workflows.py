"""Workflow clients and transitions using the session supervisor's single journal."""

import json
from pathlib import Path
import shlex
import sys

from ctlrm.managed.area import Area, worktree
from ctlrm.managed.sessions import SessionEngine, SessionService, mailbox, message
from ctlrm.managed.storage import identifier, now
from ctlrm.runtime.participants import validate_participant_id
from ctlrm.runtime.workflows import StepExecution, Task, WorkflowRun, WorkflowTemplate
from ctlrm.scheduler import transition


class WorkflowService(SessionService):
    """Shared CLI/TUI workflow operations; clients submit, the supervisor commits."""

    @classmethod
    def submit(
        cls,
        root: Path,
        template: WorkflowTemplate,
        title: str,
        instructions: str,
        *,
        bindings: dict[str, str] | None = None,
        request_id: str | None = None,
    ) -> tuple["WorkflowService", str]:
        """Snapshot one task and template into a fresh area, with idempotent retries."""
        root, _ = worktree(root)
        bindings = bindings or {name: name for name in template.roles}
        if set(bindings) != set(template.roles) or len(set(bindings.values())) != len(bindings):
            raise ValueError("bind every role to a distinct participant")
        for participant in bindings.values():
            validate_participant_id(participant)
            if participant == "coordinator":
                raise ValueError("coordinator is reserved")
        profiles = {key: value.checked().model_dump() for key, value in template.profiles.items()}
        snapshot = {**template.model_dump(), "profiles": profiles}
        if (root / ".ctlrm/area.yaml").exists():
            area = Area.load(root)
            definition = area.data.get("definition") or {}
            expected = {"template": snapshot, "bindings": bindings}
            if (
                not request_id
                or definition.get("submission_id") != request_id
                or any(definition.get(k) != v for k, v in expected.items())
                or definition.get("task", {}).get("title") != title
                or definition.get("task", {}).get("instructions") != instructions
            ):
                raise ValueError("this worktree already coordinates another instance")
            area.validate()
            area.ensure_room()
        else:
            task = Task(id=identifier("task"), title=title, instructions=instructions)
            submission_id = request_id or identifier("request")
            definition = {
                "template": snapshot,
                "bindings": bindings,
                "task": task.model_dump(),
                "run_id": identifier("run"),
                "submission_id": submission_id,
            }
            roles = {
                bindings[key]: {"name": bindings[key], "role": key, **role.model_dump()}
                for key, role in template.roles.items()
            }
            area = Area.create(
                root,
                mode="workflow",
                roles=roles,
                profiles=profiles,
                prompt=instructions,
                definition=definition,
            )
        client = cls(area)
        request = client.request("workflow-start", {}, definition["submission_id"])
        return client, request

    def join(self, participant: str) -> None:
        """Explicitly accept an assigned human role without changing the roster."""
        role = self.area.data["roles"].get(participant)
        if not role or role["kind"] != "human":
            raise ValueError("human join requires an assigned human role")
        self.area.validate()
        self.area.room._join(
            self.area.room.read_room(),
            participant_id=participant,
            name=role["name"],
            kind="human",
            provider=None,
            capabilities=["workflow-response"],
            restart_command=None,
            joined_at=now(),
            resume=True,
        )


class WorkflowEngine(SessionEngine):
    """Sequential task transitions integrated with provider lifecycle and replay."""

    def __init__(self, area: Area, terminal=None, **kwargs) -> None:
        """Load the immutable template; never reload the caller's mutable source file."""
        super().__init__(area, terminal, **kwargs)
        self.definition = area.data["definition"]
        self.template = WorkflowTemplate.model_validate(self.definition["template"])

    def active(self) -> dict:
        """Select the current visit from committed run state."""
        run = self.state.get("run")
        if not run or run["status"] != "running" or not run["active"]:
            raise ValueError("workflow has no active execution")
        return next(item for item in run["executions"] if item["id"] == run["active"])

    def _visit(self, destination: str, incoming: dict) -> None:
        """Allocate a visit once before its assignment can be delivered."""
        run = self.state["run"]
        if destination.startswith("terminal:"):
            run.update(status=destination.removeprefix("terminal:"), active=None, reason=None)
            return
        if len(run["executions"]) >= self.template.limits.max_step_executions:
            run.update(status="blocked", active=None, reason="step execution limit reached")
            return
        role = self.template.steps[destination].role
        execution = StepExecution(
            id=identifier("execution"),
            step=destination,
            participant=self.definition["bindings"][role],
            input=incoming,
        ).model_dump()
        run["executions"].append(execution)
        run["active"] = execution["id"]

    def _owner(self, payload: dict) -> tuple[dict, dict | None]:
        """Reject inactive, unaccepted, or stale ownership before acknowledgment/report."""
        execution = self.active()
        if (
            payload.get("run_id") != self.state["run"]["id"]
            or payload.get("execution_id") != execution["id"]
            or payload.get("participant") != execution["participant"]
        ):
            raise ValueError("unknown or inactive run/execution/participant")
        self.area.room.read_signature(execution["participant"])
        role = self.area.data["roles"][execution["participant"]]
        session = None
        if role["kind"] == "agent":
            session = self._session(payload)
            if session["id"] != execution["session_id"] or not session["ready"]:
                raise ValueError("execution session is not ready or does not own the work")
            if session["recovering"]:
                raise ValueError("reconcile recovered work before reporting an outcome")
        if not execution["message"]:
            raise ValueError("execution is waiting for assignment")
        return execution, session

    def apply(self, kind: str, payload: dict) -> None:
        """Validate workflow operations alongside existing session operations."""
        if kind == "workflow-start":
            if self.state.get("run"):
                raise ValueError("workflow is already started")
            self.state["run"] = WorkflowRun(
                id=self.definition["run_id"], task_id=self.definition["task"]["id"]
            ).model_dump()
            self._visit(self.template.entry, {})
            return
        if kind in {"workflow-ack", "workflow-report"}:
            execution, session = self._owner(payload)
            if kind == "workflow-ack":
                execution["status"] = "acknowledged"
                if session:
                    super().apply("acknowledge", {**payload, "work_id": execution["id"]})
                return
            if execution["status"] != "acknowledged":
                raise ValueError("acknowledge the assignment before reporting")
            outcome, summary = payload.get("outcome"), payload.get("summary")
            if not isinstance(outcome, str) or not isinstance(summary, str) or len(summary) > 16384:
                raise ValueError("outcome and a bounded summary are required")
            destination = transition(self.template, execution["step"], outcome)
            execution.update(status="completed", outcome=outcome, summary=summary)
            if session:
                session["work"]["status"] = "completed"
            self._visit(
                destination,
                {
                    "execution_id": execution["id"],
                    "outcome": outcome,
                    "summary": summary,
                    "artifact": execution["artifact"],
                },
            )
            return
        if kind == "disposition" and payload.get("status") == "completed":
            raise ValueError("workflow completion requires workflow report with an allowed outcome")
        super().apply(kind, payload)

    def advance(self) -> None:
        """Bind ready sessions or humans and publish only already committed assignments."""
        run = self.state.get("run")
        if not run or run["status"] != "running":
            return
        execution = self.active()
        role = self.area.data["roles"][execution["participant"]]
        session = next(
            (
                s
                for s in self.state["sessions"].values()
                if s["participant"] == execution["participant"]
            ),
            None,
        )
        if role["kind"] == "agent" and (not session or not session["ready"]):
            return
        if not execution["message"]:
            command = shlex.join(
                [sys.executable, "-m", "ctlrm", "--root", str(self.area.root), "workflow"]
            )
            arguments = (
                f"--run-id {run['id']} --execution-id {execution['id']} "
                f"--participant {execution['participant']}"
            )
            if session:
                arguments += f" --session-id {session['id']} --generation {session['generation']}"
                execution.update(session_id=session["id"], generation=session["generation"])
            body = (
                f"Area: {self.area.data['id']}\nRun: {run['id']}\nWork ID: {execution['id']}\n"
                f"Role instructions: {role['instructions']}\nTask: .ctlrm/task.md\n"
                f"Input: {json.dumps(execution['input'])}\n"
                f"Allowed outcomes: {', '.join(self.template.steps[execution['step']].transitions)}\n"
                f"Before acting, run: {command} acknowledge {arguments}\n"
                f"Report with: {command} report {arguments} --outcome OUTCOME --summary SUMMARY\n"
                "After native resume use your CURRENT generation in these commands. "
                "Do not choose the next recipient; the workflow routes accepted outcomes."
            )
            execution.update(
                status="pending", message=message(execution["participant"], "assignment", body)
            )
            if session:
                session["work"] = {
                    "id": execution["id"],
                    "message": execution["message"],
                    "status": "pending",
                    "generation": session["generation"],
                }
            self.commit("assignment-planned", {"execution_id": execution["id"]})
        mailbox(self.area, execution["participant"], execution["message"])
