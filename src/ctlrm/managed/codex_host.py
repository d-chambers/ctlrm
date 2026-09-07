"""A terminal-owned Codex exec loop with native resume and durable input hints."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import UUID

from ctlrm.managed.area import Area
from ctlrm.managed.sessions import SessionService, pending_message
from ctlrm.managed.storage import Journal, read_record


def turn(executable: str, arguments: list[str], native_id: str | None, prompt: str) -> str:
    """Run one native turn; no shared TUI daemon or terminal approval dialog is involved."""
    argv = [executable, "exec", *arguments, "--json"]
    if native_id:
        argv.extend(["resume", native_id])
    argv.append(prompt)
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
    assert process.stdout is not None
    try:
        for line in process.stdout:
            print(line, end="", flush=True)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "thread.started":
                observed = str(UUID(event["thread_id"]))
                if native_id and native_id != observed:
                    raise ValueError("Codex resumed a different native thread")
                native_id = observed
        if process.wait() != 0:
            raise RuntimeError("Codex turn failed; inspect the terminal output")
        if not native_id:
            raise ValueError("Codex did not report its native thread ID")
        return native_id
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


class SessionEnded(ValueError):
    """This native runner no longer owns an active session generation."""


class InputLoop:
    """Retry native hints until committed acknowledgment, with bounded wake frequency."""

    def __init__(
        self,
        root: Path,
        session_id: str,
        generation: int,
        token: str,
        config: dict,
        native_id: str,
        clock=time.monotonic,
    ) -> None:
        """Bind the input reader to one owned generation and its native conversation."""
        self.service = SessionService(Area.load(root))
        self.journal = Journal(root)
        self.session_id = session_id
        self.generation = generation
        self.token = token
        self.config = config
        self.native_id = native_id
        self.clock = clock
        self.attempted = {"bootstrap": self.clock()}

    def poll(self) -> bool:
        """Deliver only the currently pending logical hint; rejected acks remain retryable."""
        state = self.journal.replay()[0]
        session = self._current(state)
        if state["paused"] or state["shutdown"]:
            return False
        outbound = pending_message(session)
        if not session["ready"] and session["status"] in {"starting", "spawning"}:
            message_id = "bootstrap"
            reference = self.config["bootstrap"]
        else:
            if not outbound:
                return self.interact(session)
            message_id = outbound["id"]
            path = (
                self.journal.root
                / "sessions"
                / self.session_id
                / f"input-{self.generation}"
                / f"{message_id}.json"
            )
            if not path.exists():
                return False
            record = read_record(path)
            if record.get("token") != self.token or not isinstance(record.get("reference"), str):
                raise ValueError("invalid native input hint")
            reference = record["reference"]
        if self.clock() - self.attempted.get(message_id, float("-inf")) < 15:
            return False
        self.native_id = turn(
            self.config["executable"], self.config["arguments"], self.native_id, reference
        )
        self.attempted[message_id] = self.clock()
        return True

    def _current(self, state: dict) -> dict:
        """Distinguish normal ownership loss from corrupt state or unexpected request errors."""
        session = state["sessions"].get(self.session_id)
        if not session or session["generation"] != self.generation:
            raise SessionEnded("native runner generation is no longer current")
        if session["status"] == "stopped" or state.get("retired"):
            raise SessionEnded("native session has stopped")
        return session

    def interact(self, session: dict) -> bool:
        """Claim one message before resuming the same thread, without automatic replay."""
        if not session["ready"] or session["status"] != "ready" or session["recovering"]:
            return False
        item = next(
            (item for item in session.get("interactions", []) if item["status"] == "queued"), None
        )
        if item is None:
            return False
        payload = {
            "session_id": self.session_id,
            "generation": self.generation,
            "token": self.token,
            "interaction_id": item["id"],
        }
        try:
            request = self.service.request("interaction-start", payload)
            self.service.await_result(request)
        except ValueError:
            # A newly routed assignment can win the race for this idle turn.
            return False
        status, error = "completed", None
        try:
            self.native_id = turn(
                self.config["executable"], self.config["arguments"], self.native_id, item["text"]
            )
        except (ValueError, RuntimeError, OSError) as failure:
            status, error = "uncertain", str(failure)[:2048]
            print(f"Conversation message interrupted: {error}", flush=True)
        try:
            request = self.service.request(
                "interaction-finish", {**payload, "status": status, "error": error}
            )
            self.service.await_result(request)
        except ValueError as failure:
            current = self._current(self.journal.replay()[0])
            latest = next(i for i in current["interactions"] if i["id"] == item["id"])
            if latest["status"] not in {"uncertain", "canceled"}:
                raise
            print(f"Conversation message interrupted: {failure}", flush=True)
        return True


def main() -> None:
    """Run bootstrap then use committed acknowledgment to drive native input delivery."""
    config = json.loads(sys.argv[1])
    root = Path(os.environ["CTLRM_ROOT"])
    native_id = turn(
        config["executable"], config["arguments"], config["native_id"], config["bootstrap"]
    )
    inputs = InputLoop(
        root,
        os.environ["CTLRM_SESSION"],
        int(os.environ["CTLRM_GENERATION"]),
        os.environ["CTLRM_LAUNCH_TOKEN"],
        config,
        native_id,
    )
    print("Codex is waiting for a durable mailbox assignment.", flush=True)
    try:
        while True:
            inputs.poll()
            time.sleep(0.5)
    except SessionEnded as error:
        print(f"Codex session ended: {error}", flush=True)


if __name__ == "__main__":
    main()
