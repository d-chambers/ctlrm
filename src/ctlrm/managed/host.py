"""Stable terminal parent for one provider process; never restarts it implicitly."""

import os
from pathlib import Path
import subprocess
import sys
import time
from typing import NoReturn

from ctlrm.runtime.location import runtime_path
from ctlrm.runtime.paths import validate_path_component
from ctlrm.managed.storage import publish, read_record


def run_provider(root: Path, session_id: str, generation: int) -> int:
    """Run the committed command once and durably record its exit status."""
    validate_path_component(session_id, label="session ID", max_length=128)
    if type(generation) is not int or generation < 1:
        raise ValueError("generation must be a positive integer")
    root = root.resolve()
    path = runtime_path(root) / "sessions" / session_id / f"launch-{generation}.json"
    launch = read_record(path)
    token = os.environ.get("CTLRM_LAUNCH_TOKEN")
    if not token or launch.get("token") != token:
        raise ValueError("host launch token does not match committed intent")
    environment = os.environ.copy()
    environment.update(
        CTLRM_ROOT=str(root), CTLRM_SESSION=session_id, CTLRM_GENERATION=str(generation)
    )
    result = subprocess.run(launch["argv"], cwd=root, env=environment)
    publish(
        path.parent / f"exit-{generation}.json",
        {"token": token, "returncode": result.returncode},
    )
    return result.returncode


def wait_for_inspection() -> NoReturn:
    """Keep the terminal parent available after the provider exits."""
    while True:
        time.sleep(60)


def main() -> None:
    """Launch only the committed argv and remain inspectable after provider exit."""
    root_text, session_id, generation = sys.argv[1:]
    root = Path(root_text).resolve()
    os.chdir(root)
    returncode = run_provider(root, session_id, int(generation))
    print(
        f"\n[ctlrm provider exited with status {returncode}; host remains for inspection]",
        flush=True,
    )
    wait_for_inspection()


if __name__ == "__main__":
    main()
