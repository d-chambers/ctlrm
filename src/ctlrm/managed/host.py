"""Stable terminal parent for one provider process; never restarts it implicitly."""

import os
from pathlib import Path
import subprocess
import sys
import time

from ctlrm.runtime.location import runtime_path
from ctlrm.managed.storage import publish, read_record


def main() -> None:
    """Launch only the committed argv and remain inspectable after provider exit."""
    root, session_id, generation = sys.argv[1:]
    path = runtime_path(Path(root)) / "sessions" / session_id / f"launch-{generation}.json"
    launch = read_record(path)
    if launch["token"] != os.environ.get("CTLRM_LAUNCH_TOKEN"):
        raise ValueError("host launch token does not match committed intent")
    os.chdir(root)
    environment = os.environ.copy()
    environment.update(CTLRM_ROOT=root, CTLRM_SESSION=session_id, CTLRM_GENERATION=generation)
    result = subprocess.run(launch["argv"], env=environment)
    publish(
        path.parent / f"exit-{generation}.json",
        {"token": launch["token"], "returncode": result.returncode},
    )
    print(
        f"\n[ctlrm provider exited with status {result.returncode}; host remains for inspection]",
        flush=True,
    )
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
