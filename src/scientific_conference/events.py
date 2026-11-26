"""Persistent event queue: append, poll, ack, lag.

Contract (see design document section 4.4):

* events are delivered **at least once**;
* ``seq`` is strictly increasing per session and never reused;
* a consumer acknowledges a *contiguous* prefix (``through_seq``);
* duplicate appends are suppressed by ``dedupe_key``;
* polling never removes an event - only ``ack`` advances the cursor.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from . import SCHEMA_VERSION
from .db import Database, new_id, utc_now

#: Event type constants.
TURN_FINALIZED = "turn.finalized"
TALK_CLOSED = "talk.closed"
SESSION_STARTED = "session.started"
SESSION_CLOSED = "session.closed"
TRANSCRIPT_REVISED = "transcript.revised"
AUDIO_GAP = "audio.gap"
ASR_SWITCHED = "asr.switched"
ASR_AUDIO_GAP = "asr.audio_gap"
SPEAKER_UPDATED = "speaker.updated"
NOTE_SAVED = "note.saved"
SESSION_ERROR = "session.error"
PROVISIONAL_TICK = "turn.provisional"
SEGMENT_TRANSLATED = "segment.translated"

#: ``events_poll`` waits at most this long, as documented in the tool contract.
MAX_WAIT_MS = 25000

TERMINAL_TYPES = {TALK_CLOSED, SESSION_CLOSED}


class EventQueue:
    """Event operations for one session database."""

    def __init__(self, db: Database):
        self.db = db

    # -- write --------------------------------------------------------------
    def append(
        self,
        session_id: str,
        type: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Append an event.  Returns ``(event, created)``.

        When ``dedupe_key`` already exists the stored event is returned with
        ``created=False`` and no new sequence number is consumed.
        """
        dedupe_key = dedupe_key or f"{type}:{new_id('auto')}"
        with self.db.tx() as conn:
            existing = conn.execute(
                "SELECT * FROM events WHERE session_id=? AND dedupe_key=?",
                (session_id, dedupe_key),
            ).fetchone()
            if existing is not None:
                return self._hydrate(existing), False

            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM events WHERE session_id=?",
                (session_id,),
            ).fetchone()
            seq = int(row["m"]) + 1
            event_id = new_id("evt")
            created = utc_now()
            envelope = {
                "schema_version": SCHEMA_VERSION,
                "session_id": session_id,
                "event_id": event_id,
                "seq": seq,
                "type": type,
                "created_utc": created,
            }
            envelope.update(payload)
            conn.execute(
                "INSERT INTO events (session_id, seq, event_id, type, payload, dedupe_key, created_utc)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    session_id,
                    seq,
                    event_id,
                    type,
                    json.dumps(envelope, ensure_ascii=False),
                    dedupe_key,
                    created,
                ),
            )
        return envelope, True

    # -- read ---------------------------------------------------------------
    def _hydrate(self, row: Any) -> dict[str, Any]:
        return json.loads(row["payload"])

    def get(self, session_id: str, seq: int) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT payload FROM events WHERE session_id=? AND seq=?", (session_id, seq)
        )
        return self._hydrate(row) if row else None

    def max_seq(self, session_id: str) -> int:
        row = self.db.query_one(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM events WHERE session_id=?", (session_id,)
        )
        return int(row["m"]) if row else 0

    def poll(
        self,
        session_id: str,
        consumer_id: str,
        after_seq: int | None = None,
        limit: int = 50,
        wait_ms: int = 0,
    ) -> dict[str, Any]:
        """Return events with ``seq > after_seq`` (or the consumer cursor)."""
        wait_ms = max(0, min(int(wait_ms), MAX_WAIT_MS))
        deadline = time.monotonic() + wait_ms / 1000.0

        while True:
            cursor = self.cursor(session_id, consumer_id) if after_seq is None else int(after_seq)
            rows = self.db.query(
                "SELECT payload FROM events WHERE session_id=? AND seq>? ORDER BY seq ASC LIMIT ?",
                (session_id, cursor, max(1, int(limit))),
            )
            events = [self._hydrate(r) for r in rows]
            if events or time.monotonic() >= deadline:
                max_seq = self.max_seq(session_id)
                last = events[-1]["seq"] if events else cursor
                return {
                    "session_id": session_id,
                    "consumer_id": consumer_id,
                    "after_seq": cursor,
                    "events": events,
                    "next_seq": last,
                    "max_seq": max_seq,
                    "has_backlog": last < max_seq,
                }
            time.sleep(0.1)

    def cursor(self, session_id: str, consumer_id: str) -> int:
        row = self.db.query_one(
            "SELECT acked_seq FROM consumers WHERE session_id=? AND consumer_id=?",
            (session_id, consumer_id),
        )
        return int(row["acked_seq"]) if row else 0

    def ack(self, session_id: str, consumer_id: str, through_seq: int) -> int:
        """Advance the cursor forward only (never backwards)."""
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT acked_seq FROM consumers WHERE session_id=? AND consumer_id=?",
                (session_id, consumer_id),
            ).fetchone()
            current = int(row["acked_seq"]) if row else 0
            new = max(current, int(through_seq))
            if row is None:
                conn.execute(
                    "INSERT INTO consumers (session_id, consumer_id, acked_seq, updated_utc) VALUES (?,?,?,?)",
                    (session_id, consumer_id, new, utc_now()),
                )
            else:
                conn.execute(
                    "UPDATE consumers SET acked_seq=?, updated_utc=? WHERE session_id=? AND consumer_id=?",
                    (new, utc_now(), session_id, consumer_id),
                )
        return new

    def lag(self, session_id: str, consumer_id: str) -> int:
        return self.max_seq(session_id) - self.cursor(session_id, consumer_id)

    def count_by_type(self, session_id: str) -> dict[str, int]:
        rows = self.db.query(
            "SELECT type, COUNT(*) AS n FROM events WHERE session_id=? GROUP BY type", (session_id,)
        )
        return {r["type"]: int(r["n"]) for r in rows}


@dataclass
class TurnEvent:
    """Convenience view used by tests and the runner."""

    seq: int
    turn_id: str
    talk_id: str | None
    speaker_id: str | None
    revision: int
    start_ms: int
    end_ms: int
    text: str
