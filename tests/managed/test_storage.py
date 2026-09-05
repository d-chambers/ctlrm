"""Durable authority, idempotency, and corruption boundaries."""

import json
from pathlib import Path

import pytest

from ctlrm.managed.storage import Journal, publish


class TestJournal:
    """Accepted history must survive replay and reject ambiguous client retries."""

    def test_idempotent_submissions(self, repository: Path) -> None:
        """The same logical request ID cannot acquire conflicting payloads."""
        journal = Journal(repository)
        journal.submit("launch", {"participant": "agent"}, "one")
        journal.submit("launch", {"participant": "agent"}, "one")
        with pytest.raises(ValueError, match="conflict"):
            journal.submit("launch", {"participant": "other"}, "one")
        assert len(journal.pending(journal.replay()[0])[0]) == 1

    def test_lock_and_replay(self, area) -> None:
        """Two supervisors cannot append concurrently, even through different objects."""
        other = Journal(area.root)
        with area.journal.writer():
            with pytest.raises(RuntimeError, match="already has"):
                with other.writer():
                    pass
            state = area.journal.replay()[0]
            state["paused"] = "test pause"
            area.journal.commit(state, "pause")
        with other.writer():
            assert other.replay()[0]["paused"] == "test pause"
        with pytest.raises(RuntimeError, match="require"):
            other.commit({}, "invalid")

    def test_corrupt_history_blocks(self, area) -> None:
        """Never discard previously accepted events to get a supervisor running."""
        with area.journal.writer():
            area.journal.commit(area.journal.replay()[0], "initial")
        path = area.room.root / "events/000000000001.json"
        data = json.loads(path.read_text())
        data["state"]["paused"] = "tampered"
        path.write_text(json.dumps(data))
        with pytest.raises(ValueError, match="corrupted"):
            area.journal.replay()

    def test_bad_request_does_not_hide_valid(self, area) -> None:
        """Incomplete spool records remain diagnosed without losing valid requests."""
        area.journal.submit("launch", {"participant": "agent"}, "good")
        (area.room.root / "supervisor/requests/bad.json").write_text("{")
        requests, errors = area.journal.pending(area.journal.replay()[0])
        assert [item["id"] for item in requests] == ["good"]
        assert "bad.json" in errors[0]

    def test_record_conflict(self, repository: Path) -> None:
        """Immutable publication checks normalized content on retries."""
        path = repository / "record.json"
        publish(path, {"value": 1})
        publish(path, {"value": 1})
        with pytest.raises(ValueError, match="conflict"):
            publish(path, {"value": 2})


class TestDeepRequest:
    """A pathological client file cannot crash supervision of other participants."""

    def test_nested_request_is_diagnosed(self, area) -> None:
        """Excessive JSON nesting is rejected before it reaches domain transitions."""
        area.journal.submit("launch", {"participant": "agent"}, "good")
        path = area.room.root / "supervisor/requests/deep.json"
        path.write_text('{"payload":' + "[" * 1500 + "0" + "]" * 1500 + "}")
        requests, errors = area.journal.pending(area.journal.replay()[0])
        assert len(requests) == 1
        assert "nesting is too deep" in errors[0]


class TestRequestMetadataBounds:
    """Oversized rejected input must not poison the immutable disposition journal."""

    def test_large_kind_is_diagnosed(self, area) -> None:
        """Do not duplicate megabytes of an invalid kind into rejection events."""
        area.journal.submit("x" * 100000, {}, "large-kind")
        area.journal.submit("supervisor-stop", {}, "valid")
        requests, errors = area.journal.pending(area.journal.replay()[0])
        assert [request["id"] for request in requests] == ["valid"]
        assert "bounded ASCII" in errors[0]
        assert len(errors[0]) < 1000
