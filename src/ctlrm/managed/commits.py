"""Retained text documents for commits observed at an execution handoff."""

from pathlib import Path
import re
import subprocess
import tempfile

from ctlrm.managed.area import Area, git
from ctlrm.managed.storage import digest, publish, read_record
from ctlrm.runtime.paths import validate_path_component

MAX_COMMIT_TEXT = 128 * 1024
MAX_COMMITS = 50


def _git_text(root: Path, *arguments: str) -> tuple[str, bool]:
    """Bound memory and command duration when formatting potentially large patches."""
    with tempfile.TemporaryFile() as output:
        try:
            subprocess.run(
                ["git", "-C", str(root), *arguments],
                stdout=output,
                stderr=subprocess.PIPE,
                check=True,
                timeout=15,
            )
        except subprocess.SubprocessError as error:
            raise ValueError("commit text could not be read from Git") from error
        output.seek(0)
        data = output.read(MAX_COMMIT_TEXT + 1)
    truncated = len(data) > MAX_COMMIT_TEXT
    text = data[:MAX_COMMIT_TEXT].decode("utf-8", errors="replace")
    if truncated:
        text += (
            "\n\n[Display truncated at 128 KiB; inspect this commit in Git for the remainder.]\n"
        )
    return text, truncated


def retain_commit(area: Area, sha: str) -> dict:
    """Retain a commit message and patch independently of the worktree's lifetime.

    Parameters
    ----------
    area
        Owning coordination area.
    sha
        Full object ID obtained from Git, never an arbitrary revision expression.
    """
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise ValueError("commit requires a full Git object ID")
    title, _ = _git_text(area.root, "show", "-s", "--format=%s", "--encoding=UTF-8", sha, "--")
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
        sha,
        "--",
    )
    record = {
        "area_id": area.data["id"],
        "sha": sha,
        "title": title.strip()[:256],
        "text": text,
        "truncated": truncated,
    }
    reference = "commit-" + digest(record)
    publish(area.room.root / "commits" / f"{reference}.json", record)
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
    head_commit = retain_commit(area, head)
    if base is None or base == head:
        return {"head_commit": head_commit, "commits": [], "commits_note": ""}
    if git(area.root, "rev-list", "--max-count=1", base, "--not", head, "--"):
        return {
            "head_commit": head_commit,
            "commits": [],
            "commits_note": "History was rewritten; only the resulting HEAD is attributed to this snapshot.",
        }
    shas = git(
        area.root, "rev-list", f"--max-count={MAX_COMMITS + 1}", f"{base}..{head}", "--"
    ).splitlines()
    return {
        "head_commit": head_commit,
        "commits": [
            head_commit if sha == head else retain_commit(area, sha) for sha in shas[:MAX_COMMITS]
        ],
        "commits_note": f"Showing the newest {MAX_COMMITS} commits in this execution."
        if len(shas) > MAX_COMMITS
        else "",
    }


def read_commit(directory: Path, area_id: str, reference: str) -> dict:
    """Verify the identity and owner of a retained commit document."""
    validate_path_component(reference, label="commit reference", max_length=80)
    record = read_record(directory / "commits" / f"{reference}.json")
    if reference != "commit-" + digest(record) or record.get("area_id") != area_id:
        raise ValueError("invalid commit identity")
    return record
