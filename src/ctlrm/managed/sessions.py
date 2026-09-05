"""Durable standalone session transitions and provider recovery orchestration."""

import copy
import shlex
import sys
import time
from uuid import UUID

from ctlrm.communication.terminal import StopPending, Terminal, TmuxTerminal
from ctlrm.managed.area import Area
from ctlrm.managed.providers import ProviderProfile
from ctlrm.managed.storage import identifier, now, publish, read_record
from ctlrm.runtime.messages import MailboxMessage
from ctlrm.runtime.room import _write_exclusive_atomic


def mailbox(area: Area, participant: str, message: dict) -> None:
    """Publish a committed managed message, including bootstrap before signature claim."""
    if participant not in area.data["roles"]:
        raise ValueError("recipient is outside the immutable managed roster")
    area.room.init_participant(participant)
    item = MailboxMessage.model_validate(message)
    path = area.room.root / "participants" / participant / "inbox" / f"{item.id}.md"
    try:
        _write_exclusive_atomic(path, item.to_markdown())
    except FileExistsError:
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


def reference(participant: str, message_id: str) -> str:
    """Return the bounded terminal hint for a durable mailbox record."""
    return (
        f"Read .ctlrm/participants/{participant}/inbox/{message_id}.md and follow its instructions."
    )


class SessionService:
    """Client API shared by CLI/TUI; only submissions and owned signatures are written."""

    def __init__(self, area: Area) -> None:
        """Bind services to one immutable coordination area."""
        self.area = area

    def request(self, kind: str, payload: dict, request_id: str | None = None) -> str:
        """Submit an operation without creating a second state authority."""
        if kind not in {"stop", "supervisor-stop"}:
            self.area.validate()
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
        session = state["sessions"].get(session_id)
        if session is None:
            raise ValueError("unknown session")
        if session["generation"] != generation:
            raise ValueError("stale session generation")
        UUID(native_id)
        profile = ProviderProfile.model_validate(session["profile"]).checked()
        participant = session["participant"]
        role = self.area.data["roles"][participant]
        self.area.room._join(
            self.area.room.read_room(),
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
            self.area.room.root / "sessions" / session_id / "recovery" / f"{record_id}.json", record
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
                f"Your provider profile and context are recorded in .ctlrm/area.yaml. "
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
                reference(session["participant"], bootstrap["id"]),
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

    def apply(self, kind: str, payload: dict) -> None:
        """Validate one client operation against current committed state."""
        if kind == "launch":
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
            }
            self._new_generation(session, resume=False)
            self.state["sessions"][session["id"]] = session
            return
        if kind == "supervisor-stop":
            self.state["shutdown"] = True
            return
        if kind == "reconcile-area":
            self.area.validate()
            self.state["paused"] = None
            return
        session = self._session(payload, generation=kind not in {"stop", "resume", "replace"})
        if kind == "ready":
            if session["status"] not in {"spawning", "starting", "ready", "reconciling"}:
                raise ValueError("session is not awaiting readiness")
            revision = payload["revision"]
            from ctlrm.runtime.paths import validate_path_component

            validate_path_component(revision, label="recovery revision", max_length=128)
            record = read_record(
                self.area.room.root / "sessions" / session["id"] / "recovery" / f"{revision}.json"
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
            return
        if kind == "prompt":
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
            return
        if kind in {"acknowledge", "disposition"}:
            work = session.get("work")
            if not work or work["id"] != payload.get("work_id"):
                raise ValueError("unknown or inactive work ID")
            if not session["ready"]:
                raise ValueError("session must acknowledge recovery before work")
            if kind == "acknowledge":
                if session["recovering"]:
                    raise ValueError("reconcile recovered work before acknowledging continuation")
                if work["status"] == "completed":
                    return
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
            return
        if kind == "stop":
            session.update(stopped=True, ready=False, status="stopping")
            return
        if kind in {"resume", "replace"}:
            if not session.get("identity") and self.terminal.exists(session["spec"]):
                raise ValueError("stop and reconcile the existing terminal before replacement")
            if (
                session.get("identity")
                and self.terminal.health(session["spec"], session["identity"]) == "running"
            ):
                raise ValueError("stop the live session before resume or replacement")
            self._plan_restart(session, resume=kind == "resume", reset_counter=True)
            return
        raise ValueError(f"unknown request kind: {kind}")

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
                if self.state["paused"] and request["kind"] not in {
                    "reconcile-area",
                    "supervisor-stop",
                    "stop",
                }:
                    raise ValueError(
                        "area is paused; explicitly reconcile after restoring its identity"
                    )
                self.apply(request["kind"], request["payload"])
                result = {"status": "accepted"}
            except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
                self.state = before
                result = {"status": "rejected", "error": str(error)[:2048]}
            self.state["requests"][request["id"]] = result
            self.commit("request", {"id": request["id"], **result})
        if self.state["shutdown"]:
            return errors
        if not self.state["paused"]:
            try:
                self.advance()
            except (ValueError, OSError, RuntimeError) as error:
                self.state = self.journal.replay()[0]
                errors.append(f"workflow dispatch: {error}")
                return errors
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
            if self.terminal.exists(spec):
                raise ValueError("terminal name occupied before launch; refusing adoption")
            publish(
                self.area.room.root
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
            outbound = (
                session.get("reconciliation")
                if session["recovering"]
                else (session.get("work") or {}).get("message")
            )
            pending = (
                session["recovering"] or (session.get("work") or {}).get("status") == "pending"
            )
            if outbound and pending:
                mailbox(self.area, session["participant"], outbound)
                if profile.input_mode == "manual":
                    return
                # Repeated wake hints preserve one logical message/work ID; acting requires acknowledgment.
                key = (session["id"], session["generation"], outbound["id"])
                if self.clock() - self.woken.get(key, float("-inf")) >= 15:
                    try:
                        self.terminal.wake(
                            spec,
                            session["identity"],
                            reference(session["participant"], outbound["id"]),
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
