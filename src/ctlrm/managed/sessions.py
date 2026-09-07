"""Durable standalone session transitions and provider recovery orchestration."""

import copy
from collections.abc import Callable
from functools import partial
from pathlib import Path
import shlex
import sys
import time
from uuid import UUID

from ctlrm.communication.terminal import StopPending, Terminal, TmuxTerminal
from ctlrm.runtime.location import runtime_reference
from ctlrm.managed.area import Area
from ctlrm.managed.providers import ProviderProfile
from ctlrm.managed.storage import identifier, now, publish, read_record
from ctlrm.runtime.messages import MailboxMessage
from ctlrm.runtime.filesystem import write_exclusive_atomic


def mailbox(area: Area, participant: str, message: dict) -> None:
    """Publish a committed managed message, including bootstrap before signature claim."""
    if participant not in {*area.data["roles"], "coordinator"}:
        raise ValueError("recipient is outside the immutable managed roster")
    item = MailboxMessage.model_validate(message)
    path = area.runtime / "participants" / participant / "inbox" / f"{item.id}.md"
    if not path.exists():
        area.room.init_participant(participant)
        try:
            write_exclusive_atomic(path, item.to_markdown())
        except FileExistsError:
            pass
    if MailboxMessage.read(path) != MailboxMessage.parse(item.to_markdown()):
        raise ValueError(f"conflicting managed mailbox record: {path}")


def message(participant: str, kind: str, body: str, message_id: str | None = None) -> dict:
    """Allocate one immutable outbound payload for a committed transition."""
    return MailboxMessage(
        id=message_id or identifier("msg"),
        from_="coordinator",
        to=participant,
        kind=kind,
        title=kind,
        status="pending",
        created_at=now(),
        body=body,
    ).model_dump(by_alias=True)


def reference(participant: str, message_id: str, root: Path | None = None) -> str:
    """Return the bounded terminal hint for a durable mailbox record."""
    directory = runtime_reference(root) if root is not None else ".ctlrm"
    return f"Read {directory}/participants/{participant}/inbox/{message_id}.md and follow its instructions."


def pending_message(session: dict) -> dict | None:
    """Select durable work awaiting acknowledgment or recovery reconciliation."""
    if not session["ready"] or session["status"] not in {"ready", "reconciling"}:
        return None
    if session["recovering"]:
        return session.get("reconciliation")
    work = session.get("work")
    return work["message"] if work and work["status"] == "pending" else None


class SessionService:
    """Client API shared by CLI/web; only submissions and owned signatures are written."""

    def __init__(self, area: Area) -> None:
        """Bind services to one immutable coordination area."""
        self.area = area

    def request(self, kind: str, payload: dict, request_id: str | None = None) -> str:
        """Submit an operation without creating a second state authority."""
        if kind not in {"stop", "supervisor-stop", "workflow-cancel"}:
            self.area.validate()
        if self.area.journal.replay()[0].get("retired"):
            raise ValueError("job is retired; its runtime is read-only")
        return self.area.journal.submit(kind, payload, request_id)

    def status(self) -> dict:
        """Inspect committed progress and report identity or spool errors separately."""
        state, sequence, _ = self.area.journal.replay()
        _, errors = self.area.journal.pending(state)
        try:
            self.area.validate()
        except ValueError as error:
            errors.append(str(error))
        return {"area": self.area.data, "sequence": sequence, "state": state, "diagnostics": errors}

    def await_result(self, request_id: str, timeout: float = 30) -> dict:
        """Wait for committed acceptance before an agent acts on an acknowledgment."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.area.journal.replay()[0]["requests"].get(request_id)
            if result is not None:
                if result["status"] != "accepted":
                    raise ValueError(f"request {request_id} rejected: {result['error']}")
                return result
            time.sleep(0.1)
        raise RuntimeError(
            f"request {request_id} is still pending; inspect status and retry with the same ID"
        )

    def ready(self, session_id: str, generation: int, native_id: str) -> str:
        """Claim the assigned role and publish this generation's native recovery instructions."""
        state, _, _ = self.area.journal.replay()
        if state.get("retired") or state.get("retiring"):
            raise ValueError("job is retiring or retired")
        session = state["sessions"].get(session_id)
        if session is None:
            raise ValueError("unknown session")
        if session["generation"] != generation:
            raise ValueError("stale session generation")
        UUID(native_id)
        profile = ProviderProfile.model_validate(session["profile"]).checked()
        participant = session["participant"]
        role = self.area.data["roles"][participant]
        self.area.room.join_participant(
            participant_id=participant,
            name=role["name"],
            kind="agent",
            provider=profile.provider,
            capabilities=role.get("capabilities", ["managed-session"]),
            joined_at=now(),
            restart_command=role.get("restart_command"),
            resume=True,
        )
        record = {
            "schema_version": 1,
            "area_id": self.area.data["id"],
            "session_id": session_id,
            "generation": generation,
            "native_id": native_id,
            "root": str(self.area.root),
            "executable": profile.executable,
            "context": profile.context,
            "required_environment": profile.required_environment,
            "resume_argv": profile.command(native_id, "", resume=True),
            "at": now(),
        }
        record_id = identifier("recovery")
        publish(
            self.area.runtime / "sessions" / session_id / "recovery" / f"{record_id}.json", record
        )
        return self.request(
            "ready", {"session_id": session_id, "generation": generation, "revision": record_id}
        )


class SessionEngine:
    """Single-writer session state machine; external effects follow committed intent."""

    def __init__(self, area: Area, terminal: Terminal | None = None, clock=time.time) -> None:
        """Inject terminal/process behavior and time for deterministic crash tests."""
        self.area = area
        self.journal = area.journal
        self.terminal = terminal or TmuxTerminal(area.data["id"])
        self.clock = clock
        self.state = self.journal.replay()[0]
        self.woken = {}

    def commit(self, kind: str, detail: dict | None = None) -> None:
        """Persist the complete accepted state before the next side effect."""
        self.journal.commit(self.state, kind, detail)

    def _new_generation(self, session: dict, *, resume: bool) -> None:
        """Record a native resume or explicit replacement with a fresh generation."""
        profile = ProviderProfile.model_validate(session["profile"]).checked()
        if resume and not session.get("native_id"):
            raise ValueError(
                "native recovery information is unavailable; explicit replacement required"
            )
        self._interrupt_interactions(session, replace=not resume)
        if not resume:
            session.pop("recovery", None)
            session["native_id"] = profile.native_id()
        session["generation"] += 1
        generation = session["generation"]
        session.update(
            status="planned",
            identity=None,
            ready=False,
            error=None,
            created=self.clock(),
            retry_at=self.clock(),
            stopped=False,
        )
        command = shlex.join(
            [sys.executable, "-m", "ctlrm", "--root", str(self.area.root), "session"]
        )
        native = session["native_id"] or '"$CODEX_THREAD_ID"'
        bootstrap = message(
            session["participant"],
            "bootstrap",
            (
                f"You are {session['participant']} in role {self.area.data['roles'][session['participant']]['role']}.\n"
                f"{self.area.data['roles'][session['participant']].get('instructions', '')}\n"
                f"Accept your role and acknowledge your native recovery instructions by running:\n"
                f"{command} ready --session-id {session['id']} --generation {generation} --native-id {native}\n"
                f"Your provider profile and context are recorded in {runtime_reference(self.area.root)}/area.yaml. "
                f"For Codex, obtain your actual native thread ID from CODEX_THREAD_ID. "
                f"Do not invent an ID. If it is unavailable, explain the issue and wait.\n"
                f"After readiness, finish your turn and wait for a mailbox hint. Do not poll or run a waiting shell loop. Wait for a mailbox assignment. Before acting, obtain accepted acknowledgment by running {command} acknowledge "
                f"--session-id {session['id']} --generation {generation} --work-id WORK_ID.\n"
                f"Report standalone completion with {command} disposition --session-id {session['id']} "
                f"--generation {generation} --work-id WORK_ID --status completed.\n"
                "After native recovery, reconcile the referenced work ID: completed, not_started, "
                "in_progress, or uncertain. Do not repeat previously acknowledged work without reconciliation."
            ),
        )
        spec = {
            "id": session["id"],
            "participant": session["participant"],
            "generation": generation,
            "area_id": self.area.data["id"],
            "root": str(self.area.root),
            "token": identifier("launch"),
            "terminal_name": f"{session['id']}-g{generation}",
            "resume": resume,
            "argv": profile.command(
                session["native_id"],
                reference(session["participant"], bootstrap["id"], self.area.root),
                resume=resume,
            ),
            "bootstrap": bootstrap,
        }
        session["spec"] = spec
        session["recovering"] = bool(
            session.get("work") and session["work"]["status"] != "completed"
        )

    def _session(self, payload: dict, *, generation: bool = True) -> dict:
        """Validate identity and reject old-generation submissions."""
        session = self.state["sessions"].get(payload.get("session_id"))
        if session is None:
            raise ValueError("unknown session")
        if generation and payload.get("generation") != session["generation"]:
            raise ValueError("stale session generation; retained as rejected evidence")
        return session

    def _handlers(self) -> dict[str, Callable[[dict], None]]:
        """Map public request names to their operation-specific validation and mutation."""
        return {
            "launch": self._launch,
            "supervisor-stop": self._shutdown,
            "reconcile-area": self._reconcile_area,
            "ready": self._ready,
            "prompt": self._prompt,
            "acknowledge": partial(self._work_status, kind="acknowledge"),
            "disposition": partial(self._work_status, kind="disposition"),
            "stop": self._stop,
            "resume": partial(self._restart, resume=True),
            "replace": partial(self._restart, resume=False),
            "interact": partial(self._conversation, kind="interact"),
            "interaction-start": partial(self._conversation, kind="interaction-start"),
            "interaction-finish": partial(self._conversation, kind="interaction-finish"),
        }

    def _guard(self, kind: str, payload: dict) -> None:
        """Keep all public dispatch, including direct callers, behind the paused-area policy."""
        if self.state["paused"] and kind not in {
            "reconcile-area",
            "supervisor-stop",
            "stop",
            "workflow-cancel",
        }:
            raise ValueError("area is paused; explicitly reconcile after restoring its identity")

    def apply(self, kind: str, payload: dict) -> None:
        """Enforce shared guards once, then invoke the named operation handler."""
        self._guard(kind, payload)
        handler = self._handlers().get(kind)
        if handler is None:
            raise ValueError(f"unknown request kind: {kind}")
        handler(payload)

    def _launch(self, payload: dict) -> None:
        """Prepare one participant session using its checked provider profile."""
        participant = payload["participant"]
        role = self.area.data["roles"].get(participant)
        if not role or role["kind"] != "agent":
            raise ValueError("launch requires an assigned agent role")
        if any(s["participant"] == participant for s in self.state["sessions"].values()):
            raise ValueError("participant already has a session; use resume or replace")
        profile = ProviderProfile.model_validate(
            self.area.data["profiles"][role["profile"]]
        ).checked()
        session = {
            "id": identifier("session"),
            "participant": participant,
            "profile": profile.model_dump(),
            "generation": 0,
            "recoveries": 0,
            "work": None,
            "interactions": [],
        }
        self._new_generation(session, resume=False)
        self.state["sessions"][session["id"]] = session

    def _shutdown(self, payload: dict) -> None:
        """Stop supervision after the request is committed."""
        self.state["shutdown"] = True

    def _reconcile_area(self, payload: dict) -> None:
        """Clear a pause only after the recorded worktree identity is restored."""
        self.area.validate()
        self.state["paused"] = None

    def _ready(self, payload: dict) -> None:
        """Accept recovery evidence for the current native session generation."""
        session = self._session(payload)
        if session["status"] not in {"spawning", "starting", "ready", "reconciling"}:
            raise ValueError("session is not awaiting readiness")
        revision = payload["revision"]
        from ctlrm.runtime.paths import validate_path_component

        validate_path_component(revision, label="recovery revision", max_length=128)
        record = read_record(
            self.area.runtime / "sessions" / session["id"] / "recovery" / f"{revision}.json"
        )
        profile = ProviderProfile.model_validate(session["profile"]).checked()
        if not isinstance(record.get("native_id"), str):
            raise ValueError("native session ID must be a UUID string")
        UUID(record["native_id"])
        expected = {
            "schema_version": 1,
            "area_id": self.area.data["id"],
            "session_id": session["id"],
            "generation": session["generation"],
            "root": str(self.area.root),
            "executable": profile.executable,
            "context": profile.context,
            "required_environment": profile.required_environment,
            "resume_argv": profile.command(record["native_id"], "", resume=True),
        }
        if any(record.get(key) != value for key, value in expected.items()):
            raise ValueError("recovery revision does not match the authoritative profile/area")
        if session.get("native_id") and session["native_id"] != record["native_id"]:
            raise ValueError("native session ID changed during resume")
        self.area.room.read_signature(session["participant"])
        if session["status"] == "spawning":
            session["identity"] = self.terminal.adopt(session["spec"])
        session.update(
            native_id=record["native_id"],
            recovery=record,
            recoveries=0,
            ready=True,
            status="reconciling" if session["recovering"] else "ready",
            error=None,
        )
        if session["recovering"]:
            work = session["work"]
            session["reconciliation"] = message(
                session["participant"],
                "reconcile",
                f"Reconcile work ID {work['id']} from its persisted mailbox. Report completed, not_started, "
                "in_progress (explicit continuation), or uncertain using session disposition. Do not replay effects blindly.",
            )

    def _prompt(self, payload: dict) -> None:
        """Assign one standalone prompt to a ready session."""
        session = self._session(payload)
        if self.area.data["mode"] != "standalone":
            raise ValueError("workflow sessions receive work only from the workflow scheduler")
        if session.get("work") and session["work"]["status"] != "completed":
            raise ValueError("the previous prompt still needs completion or reconciliation")
        if not session["ready"] or session["status"] != "ready":
            raise ValueError("session is not ready")
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("prompt must contain text")
        if len(text.encode()) > 65536:
            raise ValueError("prompt exceeds 65536 bytes")
        work_id = identifier("work")
        outbound = message(
            session["participant"],
            "prompt",
            f"Work ID: {work_id}\nAcknowledge this work ID before acting.\n\n{text}",
        )
        session["work"] = {
            "id": work_id,
            "message": outbound,
            "status": "pending",
            "generation": session["generation"],
        }

    def _stop(self, payload: dict) -> None:
        """Plan an explicit stop without restarting or completing assigned work."""
        session = self._session(payload, generation="generation" in payload)
        self._interrupt_interactions(session)
        session.update(stopped=True, ready=False, status="stopping")

    def _work_status(self, payload: dict, *, kind: str) -> None:
        """Acknowledge or reconcile the exact work assigned to this generation."""
        session = self._session(payload)
        work = session.get("work")
        if not work or work["id"] != payload.get("work_id"):
            raise ValueError("unknown or inactive work ID")
        if not session["ready"]:
            raise ValueError("session must acknowledge recovery before work")
        if kind == "acknowledge":
            if session["recovering"]:
                raise ValueError("reconcile recovered work before acknowledging continuation")
            if work["status"] == "completed":
                raise ValueError("completed work cannot be acknowledged for new action")
            work.update(status="acknowledged", generation=session["generation"])
            return
        disposition = payload.get("status")
        if disposition not in {"completed", "not_started", "in_progress", "uncertain"}:
            raise ValueError("unknown recovery disposition")
        if work["status"] == "completed":
            if disposition != "completed":
                raise ValueError("completed work cannot be reopened")
            return
        if disposition == "completed":
            if work["status"] == "pending" and not session["recovering"]:
                raise ValueError("acknowledge work before reporting completion")
            work["status"] = "completed"
        elif disposition == "not_started":
            if not session["recovering"]:
                raise ValueError("not_started is only valid during recovery reconciliation")
            work["status"] = "pending"
        elif disposition == "in_progress":
            work["status"] = "acknowledged"
        else:
            session.update(status="blocked", error="work disposition is uncertain")
            return
        session.update(recovering=False, status="ready", error=None)
        session.pop("reconciliation", None)

    def _restart(self, payload: dict, *, resume: bool) -> None:
        """Restart only after proving the old terminal is no longer running."""
        session = self._session(payload, generation="generation" in payload)
        if not session.get("identity") and self.terminal.exists(session["spec"]):
            raise ValueError("stop and reconcile the existing terminal before replacement")
        if (
            session.get("identity")
            and self.terminal.health(session["spec"], session["identity"]) == "running"
        ):
            raise ValueError("stop the live session before resume or replacement")
        self._plan_restart(session, resume=resume, reset_counter=True)

    def _conversation(self, payload: dict, *, kind: str) -> None:
        """Validate the generation before changing its conversation input queue."""
        session = self._session(payload)
        self._interaction(session, kind, payload)

    def _interrupt_interactions(self, session: dict, *, replace: bool = False) -> None:
        """Retain interrupted messages as evidence; never replay their possible effects."""
        for item in session.get("interactions", []):
            if item["status"] == "running":
                item.update(
                    status="uncertain", error="Runner interrupted; inspect before resending"
                )
            elif replace and item["status"] == "queued":
                item.update(status="canceled", error="The native conversation was replaced")

    def _interaction(self, session: dict, kind: str, payload: dict) -> None:
        """Serialize conversation input without assigning or acknowledging workflow tasks."""
        if session["profile"]["input_mode"] != "native":
            raise ValueError("use the interactive terminal for this provider")
        items = session.setdefault("interactions", [])
        if kind == "interact":
            if not session["ready"] or session["status"] != "ready" or session["recovering"]:
                raise ValueError("session must be ready and reconciled before receiving messages")
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip() or len(text.encode()) > 16384:
                raise ValueError("message must contain text and be at most 16384 bytes")
            if sum(item["status"] in {"queued", "running"} for item in items) >= 8:
                raise ValueError("message queue is full; wait for the agent to finish")
            # Recent messages remain in the snapshot; the event journal retains older messages.
            while len(items) >= 32:
                removable = next(
                    i for i, item in enumerate(items) if item["status"] not in {"queued", "running"}
                )
                items.pop(removable)
            items.append(
                {
                    "id": identifier("interaction"),
                    "text": text,
                    "status": "queued",
                    "created": self.clock(),
                    "native_id": session["native_id"],
                }
            )
            return
        if payload.get("token") != session["spec"]["token"]:
            raise ValueError("native runner ownership changed")
        item = next((item for item in items if item["id"] == payload.get("interaction_id")), None)
        if item is None or item["native_id"] != session["native_id"]:
            raise ValueError("unknown message or changed native conversation")
        if kind == "interaction-start":
            queued = next((item for item in items if item["status"] == "queued"), None)
            if (
                not session["ready"]
                or session["status"] != "ready"
                or session["recovering"]
                or pending_message(session)
                or item is not queued
                or any(other["status"] == "running" for other in items)
            ):
                raise ValueError("message cannot start until the active assignment is delivered")
            item.update(status="running", generation=session["generation"])
            return
        if item["status"] != "running":
            raise ValueError("message is no longer running")
        status = payload.get("status")
        if status not in {"completed", "uncertain"}:
            raise ValueError("invalid message completion status")
        error = payload.get("error")
        if error is not None and (not isinstance(error, str) or len(error) > 2048):
            raise ValueError("invalid message error")
        item.update(status=status, error=error)

    def tick(self) -> list[str]:
        """Accept requests then reconcile committed effects; one writer holds the lock."""
        self.state = self.journal.replay()[0]
        try:
            self.area.validate()
        except ValueError as error:
            if self.state["paused"] != str(error):
                self.state["paused"] = str(error)
                self.commit("area-paused")
        requests, errors = self.journal.pending(self.state)
        for request in requests:
            before = copy.deepcopy(self.state)
            try:
                self.apply(request["kind"], request["payload"])
                result = {"status": "accepted"}
            except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
                self.state = before
                result = {"status": "rejected", "error": str(error)[:2048]}
            self.state["requests"][request["id"]] = result
            self.commit("request", {"id": request["id"], **result})
        if self.state["shutdown"]:
            return errors
        if not self.state["paused"] or (self.state.get("run") or {}).get("status") == "canceling":
            try:
                self.advance()
            except (ValueError, OSError, RuntimeError) as error:
                self.state = self.journal.replay()[0]
                errors.append(f"workflow dispatch: {error}")
        for session in list(self.state["sessions"].values()):
            try:
                if self.state["paused"] and session["status"] != "stopping":
                    continue
                self._effects(session)
            except StopPending as error:
                if session.get("error") != str(error):
                    session["error"] = str(error)
                    self.commit("terminal-stop-pending", {"session_id": session["id"]})
            except (ValueError, OSError, RuntimeError) as error:
                session.update(status="blocked", error=str(error))
                self.commit("session-blocked", {"session_id": session["id"], "error": str(error)})
        return errors

    def advance(self) -> None:
        """Allow the workflow scheduler to commit assignments before terminal effects."""

    def _effects(self, session: dict) -> None:
        """Reconcile launches, readiness, delivery, and confirmed process death."""
        status = session["status"]
        profile = ProviderProfile.model_validate(session["profile"])
        spec = session["spec"]
        if status in {"planned", "stopping"}:
            retiring = session.get("retiring")
            if retiring:
                if retiring["identity"]:
                    self.terminal.stop(retiring["spec"], retiring["identity"])
                session.pop("retiring")
                session["error"] = None
                self.commit("terminal-retired", {"session_id": session["id"]})
        if status == "planned":
            self.area.validate()
            if self.terminal.exists(spec):
                raise ValueError("terminal name occupied before launch; refusing adoption")
            publish(
                self.area.runtime
                / "sessions"
                / session["id"]
                / f"launch-{session['generation']}.json",
                spec,
            )
            mailbox(self.area, session["participant"], spec["bootstrap"])
            session["status"] = "spawning"
            self.commit("spawn-intent", {"session_id": session["id"]})
            status = "spawning"
        if status == "spawning":
            session["identity"] = self.terminal.ensure(spec)
            session["status"] = "starting"
            session["created"] = self.clock()
            self.commit("terminal-created", {"session_id": session["id"]})
            return
        if status == "stopping":
            if not session.get("identity") and self.terminal.exists(spec):
                session["identity"] = self.terminal.ensure(spec)
                self.commit("stop-identity-reconciled", {"session_id": session["id"]})
            if session.get("identity"):
                self.terminal.stop(spec, session["identity"])
            session.update(status="stopped", ready=False, error=None)
            self.commit("session-stopped", {"session_id": session["id"]})
            return
        if status in {"blocked", "stopped"}:
            return
        if status == "backoff":
            self.area.validate()
            if self.clock() < session["retry_at"]:
                return
            self._plan_restart(session, resume=True)
            self.commit("native-resume", {"session_id": session["id"]})
            return
        health = self.terminal.health(spec, session["identity"])
        if (
            status == "starting"
            and self.clock() - session["created"] > profile.registration_timeout
        ):
            raise ValueError(
                "readiness timeout: inspect the terminal for trust, permission, or bootstrap errors"
            )
        if health in {"exited", "missing"}:
            self._schedule_recovery(session, profile)
            return
        if session["ready"]:
            outbound = pending_message(session)
            if outbound:
                mailbox(self.area, session["participant"], outbound)
                if profile.input_mode == "manual":
                    return
                if profile.input_mode == "native":
                    publish(
                        self.area.runtime
                        / "sessions"
                        / session["id"]
                        / f"input-{session['generation']}"
                        / f"{outbound['id']}.json",
                        {
                            "token": spec["token"],
                            "reference": reference(
                                session["participant"], outbound["id"], self.area.root
                            ),
                        },
                    )
                    return
                # Repeated wake hints preserve one logical message/work ID; acting requires acknowledgment.
                key = (session["id"], session["generation"], outbound["id"])
                if self.clock() - self.woken.get(key, float("-inf")) >= 15:
                    try:
                        self.terminal.wake(
                            spec,
                            session["identity"],
                            reference(session["participant"], outbound["id"], self.area.root),
                        )
                    except (ValueError, RuntimeError):
                        current_health = self.terminal.health(spec, session["identity"])
                        if current_health == "running":
                            raise
                        if current_health == "starting":
                            return
                        self._schedule_recovery(session, profile)
                        return
                    self.woken[key] = self.clock()

    def _schedule_recovery(self, session: dict, profile: ProviderProfile) -> None:
        """Retry a confirmed death without confusing it with an ownership failure."""
        if not session.get("recovery"):
            raise ValueError("provider exited before acknowledging native recovery")
        if session["recoveries"] >= profile.recovery_limit:
            raise ValueError("native recovery limit reached; explicitly resume, replace, or stop")
        self._interrupt_interactions(session)
        session["recoveries"] += 1
        session.update(
            status="backoff",
            ready=False,
            retry_at=self.clock() + profile.backoff * session["recoveries"],
        )
        self.commit("provider-exited", {"session_id": session["id"]})

    def _plan_restart(self, session: dict, *, resume: bool, reset_counter: bool = False) -> None:
        """Validate and commit replacement intent before retiring the previous terminal."""
        if session.get("retiring"):
            raise ValueError("previous terminal retirement is pending; retry after cleanup")
        retiring = {"spec": session["spec"], "identity": session.get("identity")}
        prepared = copy.deepcopy(session)
        if reset_counter:
            prepared["recoveries"] = 0
        self._new_generation(prepared, resume=resume)
        prepared["retiring"] = retiring
        session.clear()
        session.update(prepared)
