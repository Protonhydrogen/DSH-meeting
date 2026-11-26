"""The persistent event queue: ordering, de-duplication, cursors and acks."""

from __future__ import annotations

import time

from scientific_conference.db import Database
from scientific_conference.events import EventQueue


def make_queue(tmp_path):
    return EventQueue(Database(tmp_path / "events.sqlite3"))


def test_sequence_is_monotonic_and_dense(tmp_path):
    q = make_queue(tmp_path)
    for i in range(5):
        event, created = q.append("s1", "turn.finalized", {"turn_id": f"t{i}"})
        assert created
        assert event["seq"] == i + 1
        assert event["schema_version"] == "1.0"
        assert event["session_id"] == "s1"
    assert q.max_seq("s1") == 5


def test_dedupe_key_suppresses_duplicates(tmp_path):
    q = make_queue(tmp_path)
    first, created = q.append("s1", "talk.closed", {"talk_id": "t1"}, dedupe_key="talk.closed:t1:r1")
    assert created
    second, created2 = q.append("s1", "talk.closed", {"talk_id": "t1"}, dedupe_key="talk.closed:t1:r1")
    assert not created2
    assert second["seq"] == first["seq"]
    assert second["event_id"] == first["event_id"]
    assert q.max_seq("s1") == 1


def test_poll_pagination_and_cursor(tmp_path):
    q = make_queue(tmp_path)
    for i in range(10):
        q.append("s1", "turn.finalized", {"turn_id": f"t{i}"})
    page = q.poll("s1", "c1", after_seq=0, limit=4)
    assert [e["seq"] for e in page["events"]] == [1, 2, 3, 4]
    assert page["next_seq"] == 4
    assert page["has_backlog"] is True

    q.ack("s1", "c1", 4)
    page2 = q.poll("s1", "c1", limit=10)
    assert [e["seq"] for e in page2["events"]] == [5, 6, 7, 8, 9, 10]
    assert page2["has_backlog"] is False
    assert page2["next_seq"] == 10


def test_ack_only_moves_forward(tmp_path):
    q = make_queue(tmp_path)
    for i in range(5):
        q.append("s1", "turn.finalized", {"turn_id": f"t{i}"})
    q.ack("s1", "c1", 3)
    assert q.cursor("s1", "c1") == 3
    q.ack("s1", "c1", 1)
    assert q.cursor("s1", "c1") == 3
    q.ack("s1", "c1", 5)
    assert q.cursor("s1", "c1") == 5


def test_consumers_are_independent(tmp_path):
    q = make_queue(tmp_path)
    q.append("s1", "turn.finalized", {"turn_id": "t1"})
    q.ack("s1", "agent-a", 1)
    assert q.cursor("s1", "agent-b") == 0
    assert q.lag("s1", "agent-b") == 1
    assert q.lag("s1", "agent-a") == 0


def test_poll_wait_returns_after_timeout(tmp_path):
    q = make_queue(tmp_path)
    start = time.monotonic()
    result = q.poll("s1", "c1", limit=5, wait_ms=300)
    elapsed = time.monotonic() - start
    assert result["events"] == []
    assert 0.25 <= elapsed < 5.0


def test_poll_wait_is_capped(tmp_path):
    q = make_queue(tmp_path)
    start = time.monotonic()
    q.poll("s1", "c1", limit=1, wait_ms=10_000_000)
    assert time.monotonic() - start < 30.0


def test_sessions_do_not_share_sequences(tmp_path):
    q = make_queue(tmp_path)
    a, _ = q.append("s1", "turn.finalized", {})
    b, _ = q.append("s2", "turn.finalized", {})
    assert a["seq"] == 1 and b["seq"] == 1


def test_count_by_type(tmp_path):
    q = make_queue(tmp_path)
    q.append("s1", "turn.finalized", {})
    q.append("s1", "turn.finalized", {})
    q.append("s1", "talk.closed", {})
    assert q.count_by_type("s1") == {"turn.finalized": 2, "talk.closed": 1}
