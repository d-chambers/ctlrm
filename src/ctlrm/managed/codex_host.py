"""A terminal-owned Codex exec loop with native resume and durable input hints."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import UUID

from ctlrm.managed.storage import read_record


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


def main() -> None:
    """Run bootstrap then consume each committed hint once within this generation."""
    config = json.loads(sys.argv[1])
    root = Path(os.environ["CTLRM_ROOT"])
    session_id = os.environ["CTLRM_SESSION"]
    generation = int(os.environ["CTLRM_GENERATION"])
    token = os.environ["CTLRM_LAUNCH_TOKEN"]
    native_id = turn(
        config["executable"], config["arguments"], config["native_id"], config["bootstrap"]
    )
    inputs = root / ".ctlrm/sessions" / session_id / f"input-{generation}"
    seen = set()
    print("Codex is waiting for a durable mailbox assignment.", flush=True)
    while True:
        for path in sorted(inputs.glob("*.json")):
            if path.name in seen:
                continue
            record = read_record(path)
            if record.get("token") != token or not isinstance(record.get("reference"), str):
                raise ValueError("invalid native input hint")
            seen.add(path.name)
            native_id = turn(
                config["executable"], config["arguments"], native_id, record["reference"]
            )
        time.sleep(0.2)


if __name__ == "__main__":
    main()
