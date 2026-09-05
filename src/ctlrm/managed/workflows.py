"""Workflow clients and transitions using the session supervisor's single journal."""

import json
import os
from pathlib import Path
import shlex
import sys

from ctlrm.managed.area import Area, git, worktree
from ctlrm.managed.artifacts import capture, verify
from ctlrm.managed.sessions import SessionEngine, SessionService, mailbox, message
from ctlrm.managed.storage import identifier, now, read_record
from ctlrm.runtime.location import runtime_path, runtime_reference
from ctlrm.runtime.participants import validate_participant_id
from ctlrm.runtime.paths import validate_path_component
from ctlrm.runtime.projects import PullRequest
from ctlrm.runtime.workflows import JobInput, TaskExecution, WorkflowRun, WorkflowTemplate
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
        job_id: str | None = None,
    ) -> tuple["WorkflowService", str]:
        """Snapshot one task and template into a fresh area, with idempotent retries."""
        if request_id is not None:
            validate_path_component(request_id, label="request id", max_length=128)
        root, _ = worktree(root)
        bindings = bindings or {name: name for name in template.roles}
        if set(bindings) != set(template.roles) or len(set(bindings.values())) != len(bindings):
            raise ValueError("bind every role to a distinct participant")
        for participant in bindings.values():
            validate_participant_id(participant)
            if participant == "coordinator":
                raise ValueError("coordinator is reserved")
        profiles = {}
        runtime = runtime_path(root)
        if runtime != root / ".ctlrm" and job_id is None:
            raise ValueError("central jobs must be submitted through job start")
        for key, value in template.profiles.items():
            checked = value.checked()
            if runtime != root / ".ctlrm" and checked.provider in {"codex", "claude"}:
                checked = checked.model_copy(
                    update={"arguments": [*checked.arguments, "--add-dir", str(runtime.parent)]}
                )
            profiles[key] = checked.model_dump()
        snapshot = {**template.model_dump(), "profiles": profiles}
        if (runtime_path(root) / "area.yaml").exists():
            area = Area.load(root)
            definition = area.data.get("definition") or {}
            expected = {"template": snapshot, "bindings": bindings}
            if request_id is None:
                raise ValueError(
                    f"area already exists; retry identical submission with --request-id {definition.get('submission_id', 'ORIGINAL_ID')}"
                )
            if (
                not request_id
                or definition.get("submission_id") != request_id
                or any(definition.get(k) != v for k, v in expected.items())
                or (job_id is not None and definition.get("job", {}).get("id") != job_id)
                or definition.get("job", {}).get("title") != title
                or definition.get("job", {}).get("instructions") != instructions
            ):
                raise ValueError("this worktree already coordinates another instance")
            area.validate()
            area.ensure_room()
        else:
            job = JobInput(id=job_id or identifier("job"), title=title, instructions=instructions)
            submission_id = request_id or identifier("request")
            definition = {
                "template": snapshot,
                "bindings": bindings,
                "job": job.model_dump(),
                "run_id": identifier("run"),
                "submission_id": submission_id,
                "base_commit": git(root, "rev-parse", "HEAD")
                if any(task.specialist_above_lines for task in template.tasks.values())
                else None,
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
        state = self.area.journal.replay()[0]
        if state.get("retiring") or state.get("retired"):
            raise ValueError("job is retiring or retired")
        role = self.area.data["roles"].get(participant)
        if not role or role["kind"] != "human":
            raise ValueError("human join requires an assigned human role")
        if os.environ.get("CTLRM_SESSION"):
            raise ValueError("managed agent sessions cannot accept a human role")
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
        reason = self._execution_limit(destination)
        if reason:
            run.update(status="blocked", active=None, reason=reason)
            return
        task = self.template.tasks[destination]
        role = task.role
        previous_session = next(
            (
                s
                for s in self.state["sessions"].values()
                if s["participant"] == self.definition["bindings"][role]
            ),
            None,
        )
        execution = TaskExecution(
            id=identifier("execution"),
            task=destination,
            participant=self.definition["bindings"][role],
            fresh_after_native_id=previous_session["native_id"]
            if task.fresh_session and previous_session
            else None,
            input=incoming,
        ).model_dump()
        run["executions"].append(execution)
        run["active"] = execution["id"]

    def _execution_limit(self, task_name: str) -> str | None:
        """Apply the same job/task/specialist budgets to transitions and explicit retries."""
        executions = self.state["run"]["executions"]
        task = self.template.tasks[task_name]
        if len(executions) >= self.template.limits.max_task_executions:
            return "task execution limit reached"
        if (
            task.max_executions is not None
            and sum(e["task"] == task_name for e in executions) >= task.max_executions
        ):
            return f"task execution limit reached: {task_name}"
        if (
            task.optional
            and sum(self.template.tasks[e["task"]].optional for e in executions)
            >= self.template.limits.max_specialist_executions
        ):
            return "specialist execution limit reached"
        return None

    def _specialists(self, execution: dict, payload: dict, outcome: str) -> list[str]:
        """Validate discretionary and size-triggered reviews before accepting an outcome."""
        task = self.template.tasks[execution["task"]]
        selected = payload.get("specialists") or []
        if not isinstance(selected, list) or any(not isinstance(name, str) for name in selected):
            raise ValueError("specialists must be a list of task names")
        if len(set(selected)) != len(selected) or set(selected) - set(task.specialist_tasks):
            raise ValueError("specialist request is duplicate or outside the workflow allowlist")
        reason = payload.get("specialist_reason")
        if reason is not None and (
            not isinstance(reason, str) or not reason.strip() or len(reason) > 4096
        ):
            raise ValueError("specialist reason must be nonblank and at most 4096 characters")
        if selected and (outcome != "approved" or reason is None):
            raise ValueError("specialist requests require an approved outcome and a bounded reason")
        used = sum(self.template.tasks[e["task"]].optional for e in self.state["run"]["executions"])
        if used + len(selected) > self.template.limits.max_specialist_executions:
            raise ValueError("specialist execution limit reached")
        if outcome != "approved":
            return []
        selected = list(selected)
        if task.specialist_above_lines:
            base = self.definition.get("base_commit")
            if not base:
                raise ValueError("size-triggered review needs a snapshotted base commit")
            counts = git(self.area.root, "diff", "--numstat", base, "--").splitlines()
            lines = sum(
                int(value) for line in counts for value in line.split("\t")[:2] if value.isdigit()
            )
            selected.extend(
                name
                for name, threshold in task.specialist_above_lines.items()
                if lines > threshold and name not in selected
            )
        return selected

    def _route_report(
        self,
        execution: dict,
        destination: str,
        incoming: dict,
        specialists: list[str],
        reason: str | None,
    ) -> None:
        """Run bounded specialists sequentially, retaining findings and the normal continuation."""
        context = execution["input"].get("specialist_context")
        if self.template.tasks[execution["task"]].optional:
            if not context:
                raise ValueError("specialist task has no recorded continuation")
            reviews = [*context.get("reviews", []), incoming.copy()]
            if execution["outcome"] == "approved":
                pending = context["pending"]
                destination = pending[0] if pending else context["destination"]
                if pending:
                    incoming["specialist_context"] = {
                        **context,
                        "pending": pending[1:],
                        "reviews": reviews,
                    }
            if "specialist_context" not in incoming:
                incoming["reviews"] = reviews
            # Findings route through the task's declared fix destination; remaining reviews are superseded.
        elif specialists:
            incoming["specialist_context"] = {
                "requester": execution["id"],
                "reason": reason or "change-size threshold",
                "destination": destination,
                "pending": specialists[1:],
                "reviews": [],
            }
            destination = specialists[0]
        self._visit(destination, incoming)

    def _owner(self, payload: dict) -> tuple[dict, dict | None]:
        """Reject inactive, unaccepted, or stale ownership before acknowledgment/report."""
        execution = self.active()
        if (
            payload.get("run_id") != self.state["run"]["id"]
            or payload.get("execution_id") != execution["id"]
            or payload.get("participant") != execution["participant"]
        ):
            raise ValueError("unknown or inactive run/execution/participant")
        signature = self.area.room.read_signature(execution["participant"])
        role = self.area.data["roles"][execution["participant"]]
        expected_provider = (
            self.area.data["profiles"][role["profile"]]["provider"]
            if role["kind"] == "agent"
            else None
        )
        if (signature.kind, signature.name, signature.provider) != (
            role["kind"],
            role["name"],
            expected_provider,
        ):
            raise ValueError("participant signature conflicts with the managed role")
        if role["kind"] == "human" and os.environ.get("CTLRM_SESSION"):
            raise ValueError("managed agent sessions cannot submit human responses")
        session = None
        if role["kind"] == "agent":
            session = self._session(payload)
            if (
                session["id"] != execution["session_id"]
                or not session["ready"]
                or session["status"] not in {"ready", "reconciling"}
            ):
                raise ValueError("execution session is not ready or does not own the work")
            if session["recovering"]:
                raise ValueError("reconcile recovered work before reporting an outcome")
        if not execution["message"]:
            raise ValueError("execution is waiting for assignment")
        return execution, session

    def _cancel(self) -> None:
        """Commit cancellation authority before stopping every owned provider generation."""
        run = self.state.get("run")
        if not run or run["status"] not in {"running", "blocked", "canceling"}:
            raise ValueError("only unfinished workflows can be canceled")
        run.update(status="canceling", reason="waiting for owned providers to stop")
        for session in self.state["sessions"].values():
            if session["status"] not in {"stopped", "stopping"}:
                super().apply("stop", {"session_id": session["id"]})

    def _retry(self, payload: dict) -> None:
        """Explicitly abandon one visit, refreshing its input artifact when requested."""
        execution = self.active()
        if payload.get("execution_id") != execution["id"]:
            raise ValueError("retry requires the active execution ID")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 16384:
            raise ValueError("retry requires a bounded explanation")
        session = self.state["sessions"].get(execution["session_id"])
        if session and session["status"] != "stopped":
            raise ValueError("explicitly stop the assigned session before retrying work")
        destination = execution["task"]
        incoming = {**execution["input"], "retry_of": execution["id"], "retry_reason": reason}
        if incoming.get("artifact"):
            incoming.update(
                previous_execution_id=incoming.get("execution_id"),
                previous_artifact=incoming["artifact"],
                execution_id=execution["id"],
                outcome="retried",
                summary=reason,
            )
            incoming["artifact"] = capture(self.area, self.state["run"]["id"], execution["id"])
            before_version = read_record(
                self.area.room.root / "artifacts" / f"{incoming['previous_artifact']}.json"
            )["version"]
            after_version = read_record(
                self.area.room.root / "artifacts" / f"{incoming['artifact']}.json"
            )["version"]
            if before_version != after_version:
                if self.template.tasks[execution["task"]].optional:
                    destination = self.template.tasks[execution["task"]].transitions[
                        "changes_requested"
                    ]
                    incoming["invalidated_specialist_context"] = incoming.pop("specialist_context")
                    incoming.pop("reviews", None)
                elif (
                    self.template.tasks[destination].verify_input
                    or self.template.tasks[destination].verified_outcomes
                ) and any(
                    prior["outcome"] == "approved"
                    and self.template.tasks[prior["task"]].checks_input("approved")
                    and read_record(
                        self.area.room.root / "artifacts" / f"{prior['artifact']}.json"
                    )["version"]
                    == before_version
                    for prior in self.state["run"]["executions"]
                ):
                    raise ValueError(
                        "retry would invalidate completed reviews; restore the reviewed version "
                        "and report the workflow's fix outcome, or cancel and start a new job"
                    )
        reason_limit = self._execution_limit(destination)
        if reason_limit:
            raise ValueError(reason_limit + "; cancel or use a fresh worktree")
        execution["status"] = "abandoned"
        if session:
            session["work"] = None
            session["recovering"] = False
        self._visit(destination, incoming)

    def apply(self, kind: str, payload: dict) -> None:
        """Validate workflow operations alongside existing session operations."""
        if self.state.get("retiring") or self.state.get("retired"):
            if kind not in {"workflow-retire", "stop", "supervisor-stop", "reconcile-area"}:
                raise ValueError("job is retiring or archived; no further work can start")
        if kind == "workflow-retire":
            if (self.state.get("run") or {}).get("status") != "completed":
                raise ValueError("only completed jobs may retire for archival")
            self.state["retiring"] = True
            for session in self.state["sessions"].values():
                if session["status"] not in {"stopped", "stopping"}:
                    super().apply("stop", {"session_id": session["id"]})
            return
        if kind == "workflow-cancel":
            self._cancel()
            return
        if kind == "workflow-retry":
            self.area.validate()
            self._retry(payload)
            return
        run = self.state.get("run")
        if (
            run
            and run["status"] in {"canceling", "canceled"}
            and kind not in {"stop", "supervisor-stop", "reconcile-area"}
        ):
            raise ValueError("workflow is canceled; no further work can start")
        if kind == "launch":
            raise ValueError("workflow roles are launched by the active step")
        if kind == "workflow-start":
            if self.state.get("run"):
                raise ValueError("workflow is already started")
            self.state["run"] = WorkflowRun(
                id=self.definition["run_id"], job_id=self.definition["job"]["id"]
            ).model_dump()
            self._visit(self.template.entry, {})
            return
        if kind in {"workflow-ack", "workflow-report"}:
            self.area.validate()
            execution, session = self._owner(payload)
            if payload.get("input_artifact") != execution["input"].get("artifact"):
                raise ValueError("acknowledge and report the exact assigned input artifact")
            if kind == "workflow-ack":
                execution["status"] = "acknowledged"
                if session:
                    execution["generation"] = session["generation"]
                    super().apply("acknowledge", {**payload, "work_id": execution["id"]})
                return
            if execution["status"] != "acknowledged":
                raise ValueError("acknowledge the assignment before reporting")
            if session and (
                session["work"]["status"] != "acknowledged"
                or execution["generation"] != session["generation"]
            ):
                raise ValueError(
                    "acknowledge this execution in the current generation before reporting"
                )
            outcome, summary = payload.get("outcome"), payload.get("summary")
            if not isinstance(outcome, str) or not isinstance(summary, str) or len(summary) > 16384:
                raise ValueError("outcome and a bounded summary are required")
            destination = transition(self.template, execution["task"], outcome)
            specialists = self._specialists(execution, payload, outcome)
            pr = (
                PullRequest.model_validate(payload["pr"]).model_dump()
                if payload.get("pr") is not None
                else None
            )
            if outcome in self.template.tasks[execution["task"]].requires_pr and pr is None:
                raise ValueError("this task requires repository-qualified PR metadata")
            artifact = capture(self.area, self.state["run"]["id"], execution["id"])
            if self.template.tasks[execution["task"]].checks_input(outcome) and execution[
                "input"
            ].get("artifact"):
                captured = read_record(self.area.room.root / "artifacts" / f"{artifact}.json")
                verify(self.area, execution["input"]["artifact"], version=captured["version"])
            self.area.validate()
            execution.update(
                status="completed", outcome=outcome, summary=summary, artifact=artifact
            )
            if session:
                session["work"]["status"] = "completed"
            if pr is not None:
                self.state["pr"] = pr
            self._route_report(
                execution,
                destination,
                {
                    "execution_id": execution["id"],
                    "outcome": outcome,
                    "summary": summary,
                    "artifact": execution["artifact"],
                },
                specialists,
                payload.get("specialist_reason"),
            )
            return
        if kind == "disposition" and payload.get("status") == "completed":
            raise ValueError("workflow completion requires workflow report with an allowed outcome")
        super().apply(kind, payload)

    def advance(self) -> None:
        """Bind ready sessions or humans and publish only already committed assignments."""
        if self.state.get("retiring"):
            if all(session["status"] == "stopped" for session in self.state["sessions"].values()):
                self.state.update(retiring=False, retired=True, shutdown=True)
                self.commit("job-retired")
            return
        run = self.state.get("run")
        if run and run["status"] == "canceling":
            if all(s["status"] == "stopped" for s in self.state["sessions"].values()):
                run.update(status="canceled", active=None, reason=None)
                self.commit("workflow-canceled")
            return
        if not run or run["status"] != "running":
            return
        self.area.validate()
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
        if role["kind"] == "agent" and not session:
            super().apply("launch", {"participant": execution["participant"]})
            self.commit("role-launch-planned", {"participant": execution["participant"]})
            return
        task = self.template.tasks[execution["task"]]
        if (
            role["kind"] == "agent"
            and session
            and task.fresh_session
            and not execution["message"]
            and execution.get("fresh_after_native_id") is not None
            and session["native_id"] == execution["fresh_after_native_id"]
        ):
            if session["status"] == "ready":
                execution["reset_generation"] = session["generation"] + 1
                super().apply("stop", {"session_id": session["id"]})
                self.commit("fresh-review-stop-planned", {"execution_id": execution["id"]})
                return
            if execution.get("reset_generation") is not None and session["status"] == "stopped":
                super().apply("replace", {"session_id": session["id"]})
                self.commit("fresh-review-session-planned", {"execution_id": execution["id"]})
                return
        if role["kind"] == "agent" and (not session["ready"] or session["status"] != "ready"):
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
            if execution["input"].get("artifact"):
                arguments += f" --input-artifact {execution['input']['artifact']}"
            body = (
                f"Area: {self.area.data['id']}\nRun: {run['id']}\nWork ID: {execution['id']}\n"
                f"Role instructions: {role['instructions']}\nJob goal: {runtime_reference(self.area.root)}/job.md\n"
                f"Review records directory: {runtime_reference(self.area.root)}/reviews\n"
                f"Task: {execution['task']}\nTask instructions: {self.template.tasks[execution['task']].instructions}\n"
                f"Input: {json.dumps(execution['input'])}\n"
                f"Input verification policy: all outcomes={self.template.tasks[execution['task']].verify_input}; named outcomes={self.template.tasks[execution['task']].verified_outcomes}\n"
                f"Optional specialists: {task.specialist_tasks}; use report --specialist TASK --specialist-reason REASON with an approved outcome.\n"
                f"PR metadata required: {task.requires_pr}; report --pr-number N --pr-repository OWNER/REPO [--pr-url URL].\n"
                f"Allowed outcomes: {', '.join(self.template.tasks[execution['task']].transitions)}\n"
                f"Before acting, run: {command} acknowledge {arguments}\n"
                f"Report with: {command} report {arguments} --outcome OUTCOME --summary SUMMARY\n"
                "After native resume use your CURRENT generation in these commands. "
                "Do not choose the next recipient; the workflow routes accepted outcomes. "
                "After an accepted report, finish your turn and wait; do not perform further work."
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
