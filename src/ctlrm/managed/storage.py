"""Bounded immutable records and a single-writer, hash-chained event journal."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from ctlrm.runtime.filesystem import mkdir_shared
from ctlrm.runtime.paths import validate_path_component
from ctlrm.runtime.room import _write_exclusive_atomic

MAX_RECORD_BYTES = 8 * 1024 * 1024


def now() -> str:
    """Return a UTC timestamp for immutable records."""
    return datetime.now(timezone.utc).isoformat()


def identifier(prefix: str) -> str:
    """Allocate a compact identifier safe for mailbox and record paths."""
    return f"{prefix}-{uuid4().hex}"


def encoded(value: object) -> str:
    """Encode canonical JSON, also valid as a YAML document."""
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n"


def digest(value: object) -> str:
    """Hash a normalized record for journal continuity and identity."""
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def read_record(path: Path) -> dict:
    """Read one bounded JSON record, rejecting duplicate keys and non-objects."""

    def unique(pairs: list) -> dict:
        """Reject duplicate object keys rather than silently accepting the last."""
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate record key: {key}")
            result[key] = value
        return result

    with path.open("rb") as stream:
        data = stream.read(MAX_RECORD_BYTES + 1)
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError(f"record exceeds {MAX_RECORD_BYTES} bytes: {path}")
    try:
        value = json.loads(data, object_pairs_hook=unique)
    except RecursionError as error:
        raise ValueError(f"record nesting is too deep: {path}") from error
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 64:
            raise ValueError(f"record nesting is too deep: {path}")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    if not isinstance(value, dict):
        raise ValueError(f"record must be an object: {path}")
    return value


def publish(path: Path, value: dict) -> None:
    """Publish once, allowing retries only with identical normalized content."""
    text = encoded(value)
    if len(text.encode()) > MAX_RECORD_BYTES:
        raise ValueError("record too large")
    mkdir_shared(path.parent)
    try:
        _write_exclusive_atomic(path, text)
    except FileExistsError:
        if read_record(path) != value:
            raise ValueError(f"immutable record conflict: {path}")


class Journal:
    """Store client submissions and supervisor-committed snapshots separately."""

    def __init__(self, root: Path) -> None:
        """Locate an area's journal without creating it."""
        self.root = root / ".ctlrm"
        self.locked = False

    @contextmanager
    def writer(self) -> Iterator[None]:
        """Hold the persistent supervisor lock; never replace its inode."""
        mkdir_shared(self.root / "supervisor")
        with (self.root / "supervisor/lock").open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("this worktree already has a supervisor") from error
            self.locked = True
            try:
                yield
            finally:
                self.locked = False

    def replay(self) -> tuple[dict, int, str]:
        """Reconstruct state, refusing gaps or corrupted accepted history."""
        state = {"sessions": {}, "requests": {}, "paused": None, "shutdown": False}
        previous = ""
        sequence = 0
        for path in sorted((self.root / "events").glob("*.json")):
            event = read_record(path)
            sequence += 1
            if path.name != f"{sequence:012d}.json" or event.get("sequence") != sequence:
                raise ValueError(f"event sequence gap or filename mismatch: {path}")
            checksum = event.pop("checksum", None)
            if event.get("previous") != previous or digest(event) != checksum:
                raise ValueError(f"corrupted accepted history: {path}")
            if not isinstance(event.get("state"), dict):
                raise ValueError(f"invalid event state: {path}")
            state = event["state"]
            previous = checksum
        return state, sequence, previous

    def commit(self, state: dict, kind: str, detail: dict | None = None) -> None:
        """Commit a complete transition before performing its external effects."""
        if not self.locked:
            raise RuntimeError("event commits require the supervisor lock")
        _, sequence, previous = self.replay()
        event = {
            "sequence": sequence + 1,
            "previous": previous,
            "at": now(),
            "kind": kind,
            "detail": detail or {},
            "state": state,
        }
        event["checksum"] = digest(event)
        publish(self.root / "events" / f"{sequence + 1:012d}.json", event)

    def submit(self, kind: str, payload: dict, request_id: str | None = None) -> str:
        """Submit an idempotent request; callers retain its ID for retries."""
        request_id = request_id or identifier("req")
        validate_path_component(request_id, label="request id", max_length=128)
        path = self.root / "supervisor/requests" / f"{request_id}.json"
        request = {"id": request_id, "kind": kind, "payload": payload, "at": now()}
        if path.exists():
            existing = read_record(path)
            request["at"] = existing["at"]
        try:
            publish(path, request)
        except ValueError:
            existing = read_record(path)
            if any(existing.get(key) != request[key] for key in ("id", "kind", "payload")):
                raise
        return request_id

    def pending(self, state: dict) -> tuple[list[dict], list[str]]:
        """Read submissions in timestamp order and visibly diagnose malformed files."""
        requests, errors = [], []
        for path in sorted((self.root / "supervisor/requests").glob("*.json")):
            if path.stem in state["requests"]:
                continue
            try:
                item = read_record(path)
                if item.get("id") != path.stem or not isinstance(item.get("payload"), dict):
                    raise ValueError("request identity or payload mismatch")
                if not isinstance(item.get("at"), str) or not isinstance(item.get("kind"), str):
                    raise ValueError("request timestamp/kind missing")
                if not 1 <= len(item["kind"]) <= 64 or not item["kind"].isascii():
                    raise ValueError("request kind must be bounded ASCII text")
                if len(item["at"]) > 64:
                    raise ValueError("request timestamp is too long")
                validate_path_component(item["id"], label="request id", max_length=128)
                requests.append(item)
            except (ValueError, OSError) as error:
                errors.append(f"{path}: {error}")
        return sorted(requests, key=lambda item: (item["at"], item["id"])), errors
