"""Retained text documents for commits observed at an execution handoff."""

import os
from pathlib import Path
import re
import selectors
import subprocess
import time

from ctlrm.managed.area import Area
from ctlrm.managed.storage import digest, publish, read_record
from ctlrm.runtime.paths import validate_path_component

MAX_COMMIT_TEXT = 128 * 1024
MAX_COMMITS = 50
PREVIEW_SECONDS = 5


def _git_text(root: Path, *arguments: str, deadline: float) -> tuple[str, bool]:
    """Read a capped pipe and reap Git within the shared preview budget, without a disk spool."""
    if time.monotonic() >= deadline:
        raise ValueError("commit preview time budget exhausted")
    try:
        with subprocess.Popen(
            ["git", "-C", str(root), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as process:
            assert process.stdout is not None
            data = bytearray()
            try:
                with selectors.DefaultSelector() as ready:
                    ready.register(process.stdout, selectors.EVENT_READ)
                    while len(data) <= MAX_COMMIT_TEXT:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not ready.select(remaining):
                            raise ValueError("commit preview time budget exhausted")
                        chunk = os.read(
                            process.stdout.fileno(), min(65536, MAX_COMMIT_TEXT + 1 - len(data))
                        )
                        if not chunk:
                            if process.wait(timeout=max(0, deadline - time.monotonic())):
                                raise ValueError("commit text could not be read from Git")
                            break
                        data.extend(chunk)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("commit text could not be read from Git") from error
    truncated = len(data) > MAX_COMMIT_TEXT
    text = data[:MAX_COMMIT_TEXT].decode("utf-8", errors="replace")
    if truncated:
        text += (
            "\n\n[Display truncated at 128 KiB; inspect this commit in Git for the remainder.]\n"
        )
    return text, truncated


def retain_commit(area: Area, sha: str, *, deadline: float | None = None) -> dict:
    """Retain a commit message and patch independently of the worktree's lifetime.

    Parameters
    ----------
    area
        Owning coordination area.
    sha
        Full object ID obtained from Git, never an arbitrary revision expression.
    deadline
        Shared monotonic deadline; omitted for a single-commit preview budget.
    """
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise ValueError("commit requires a full Git object ID")
    if deadline is None:
        deadline = time.monotonic() + PREVIEW_SECONDS
    title = sha[:12]
    try:
        text, truncated = _git_text(
            area.root,
            "show",
            "--format=fuller",
            "--date=iso-strict",
            "--encoding=UTF-8",
            "--stat",
            "--patch",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--no-show-signature",
            sha,
            "--",
            deadline=deadline,
        )
        body = text.partition("\n\n")[2]
        if body.startswith("    "):
            title = body.splitlines()[0].strip()[:256]
    except ValueError as error:
        text = (
            f"Commit: {sha}\n\n[Commit preview unavailable: {error}. Inspect this commit in Git.]\n"
        )
        truncated = True
    record = {
        "area_id": area.data["id"],
        "sha": sha,
        "title": title,
        "text": text,
        "truncated": truncated,
    }
    reference = "commit-" + digest(record)
    publish(area.runtime / "commits" / f"{reference}.json", record)
    return {"id": reference, "sha": sha, "title": record["title"], "truncated": truncated}


def retain_commits(area: Area, base: str | None, head: str) -> dict:
    """Freeze the bounded range introduced since this execution began.

    Parameters
    ----------
    area
        Owning coordination area.
    base
        Execution's initial HEAD, or None for an unattributed version snapshot.
    head
        Exact HEAD from the captured artifact manifest.
    """
    deadline = time.monotonic() + PREVIEW_SECONDS
    head_commit = retain_commit(area, head, deadline=deadline)
    result = {"head_commit": head_commit, "commits": [], "commits_note": ""}
    if base is None or base == head:
        return result
    try:
        rewritten, _ = _git_text(
            area.root, "rev-list", "--max-count=1", base, "--not", head, "--", deadline=deadline
        )
        if rewritten:
            result["commits_note"] = (
                "History was rewritten; only the resulting HEAD is attributed to this snapshot."
            )
            return result
        history, _ = _git_text(
            area.root,
            "rev-list",
            f"--max-count={MAX_COMMITS + 1}",
            f"{base}..{head}",
            "--",
            deadline=deadline,
        )
    except ValueError:
        result["commits_note"] = (
            "Commit history is unavailable; only the resulting HEAD is attributed to this snapshot."
        )
        return result
    shas = history.splitlines()
    for sha in shas[:MAX_COMMITS]:
        if time.monotonic() >= deadline:
            result["commits_note"] = (
                "Commit preview time budget reached; the remaining commits are not listed."
            )
            break
        result["commits"].append(
            head_commit if sha == head else retain_commit(area, sha, deadline=deadline)
        )
    else:
        if len(shas) > MAX_COMMITS:
            result["commits_note"] = f"Showing the newest {MAX_COMMITS} commits in this execution."
    return result


def read_commit(directory: Path, area_id: str, reference: str) -> dict:
    """Verify the identity and owner of a retained commit document."""
    validate_path_component(reference, label="commit reference", max_length=80)
    record = read_record(directory / "commits" / f"{reference}.json")
    if reference != "commit-" + digest(record) or record.get("area_id") != area_id:
        raise ValueError("invalid commit identity")
    return record
