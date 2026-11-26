"""SQLite storage layer.

One database file per recording session lives inside the meeting directory
(``meetings/<session_id>/session.sqlite3``) so that a meeting folder is
self-contained and can be copied or archived.  A separate small index database
at ``<output_root>/index.sqlite3`` only records which sessions exist.

Design rules taken from the project plan:

* SQLite holds queues, cursors, revisions and task state; JSONL export is a
  derived artefact for auditing and migration.
* Events are stored with a per-session monotonically increasing ``seq``.
* Every write that matters is a single transaction.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from . import SCHEMA_VERSION

SCHEMA = """
CREATE TABLE IF NOT EXISTS transcript_edits (
 session_id TEXT NOT NULL, segment_id TEXT NOT NULL, original TEXT NOT NULL,
 corrected TEXT NOT NULL, reason TEXT NOT NULL, model TEXT NOT NULL,
 PRIMARY KEY(session_id, segment_id)
);

CREATE TABLE IF NOT EXISTS review_heads (
 session_id TEXT NOT NULL, segment_id TEXT NOT NULL, field TEXT NOT NULL,
 value TEXT, revision INTEGER NOT NULL DEFAULT 0, undo_stack TEXT NOT NULL DEFAULT '[]',
 PRIMARY KEY(session_id,segment_id,field)
);
CREATE TABLE IF NOT EXISTS review_history (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, segment_id TEXT NOT NULL,
 field TEXT NOT NULL, channel TEXT NOT NULL, value TEXT, action TEXT NOT NULL,
 created_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS speaker_alias_history (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, speaker_id TEXT NOT NULL,
 old_name TEXT, new_name TEXT NOT NULL, created_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS speaker_runs (
 run_id TEXT PRIMARY KEY, model TEXT NOT NULL, intervals_json TEXT NOT NULL, created_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    state           TEXT NOT NULL,              -- starting|recording|draining|closed|failed
    created_utc     TEXT NOT NULL,
    started_utc     TEXT,
    ended_utc       TEXT,
    device_id       TEXT,
    device_name     TEXT,
    sample_rate     INTEGER,
    channels        INTEGER,
    output_dir      TEXT NOT NULL,
    config_json     TEXT NOT NULL,
    config_hash     TEXT NOT NULL,
    asr_backend     TEXT NOT NULL,
    vad_backend     TEXT NOT NULL,
    diarization     TEXT NOT NULL,
    language_hints  TEXT NOT NULL,
    consent         INTEGER NOT NULL DEFAULT 0,
    title           TEXT,
    agenda          TEXT,
    last_error      TEXT,
    pid             INTEGER,
    updated_utc     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audio_chunks (
    session_id      TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL,
    path            TEXT,
    converted_path  TEXT,
    start_ms        INTEGER NOT NULL,
    end_ms          INTEGER NOT NULL,
    frames          INTEGER NOT NULL,
    sample_rate     INTEGER NOT NULL,
    channels        INTEGER NOT NULL,
    bytes           INTEGER NOT NULL DEFAULT 0,
    sha256          TEXT,
    gap_before_ms   INTEGER NOT NULL DEFAULT 0,
    created_utc     TEXT NOT NULL,
    PRIMARY KEY (session_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS talks (
    session_id  TEXT NOT NULL,
    talk_id     TEXT NOT NULL,
    title       TEXT,
    speaker_id  TEXT,
    start_ms    INTEGER NOT NULL,
    end_ms      INTEGER,
    revision    INTEGER NOT NULL DEFAULT 1,
    state       TEXT NOT NULL,                  -- open|closed
    created_utc TEXT NOT NULL,
    updated_utc TEXT NOT NULL,
    PRIMARY KEY (session_id, talk_id)
);

CREATE TABLE IF NOT EXISTS turns (
    session_id  TEXT NOT NULL,
    turn_id     TEXT NOT NULL,
    talk_id     TEXT,
    speaker_id  TEXT,
    start_ms    INTEGER NOT NULL,
    end_ms      INTEGER,
    revision    INTEGER NOT NULL DEFAULT 1,
    boundary_reason TEXT,
    state       TEXT NOT NULL,                  -- open|closing|finalized|superseded
    created_utc TEXT NOT NULL,
    updated_utc TEXT NOT NULL,
    PRIMARY KEY (session_id, turn_id)
);

CREATE TABLE IF NOT EXISTS segments (
    session_id      TEXT NOT NULL,
    segment_id      TEXT NOT NULL,
    turn_id         TEXT,
    talk_id         TEXT,
    start_ms        INTEGER NOT NULL,
    end_ms          INTEGER NOT NULL,
    text            TEXT NOT NULL,
    is_final        INTEGER NOT NULL DEFAULT 0,
    revision        INTEGER NOT NULL DEFAULT 1,
    speaker_id      TEXT,
    asr_confidence  REAL,
    flags           TEXT NOT NULL DEFAULT '[]',
    backend         TEXT,
    window_id       TEXT,
    source          TEXT NOT NULL DEFAULT 'asr', -- asr|manual|import
    superseded_by   TEXT,
    created_utc     TEXT NOT NULL,
    PRIMARY KEY (session_id, segment_id)
);

CREATE INDEX IF NOT EXISTS idx_segments_time ON segments (session_id, start_ms);
CREATE INDEX IF NOT EXISTS idx_segments_turn ON segments (session_id, turn_id, revision);

CREATE TABLE IF NOT EXISTS events (
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    event_id    TEXT NOT NULL,
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedupe ON events (session_id, dedupe_key);

CREATE TABLE IF NOT EXISTS consumers (
    session_id   TEXT NOT NULL,
    consumer_id  TEXT NOT NULL,
    acked_seq    INTEGER NOT NULL DEFAULT 0,
    updated_utc  TEXT NOT NULL,
    PRIMARY KEY (session_id, consumer_id)
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id           TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    event_seq         INTEGER,
    type              TEXT NOT NULL,
    mode              TEXT NOT NULL,            -- turn|talk|session
    target_id         TEXT,
    source_revision   INTEGER NOT NULL DEFAULT 1,
    idempotency_key   TEXT NOT NULL UNIQUE,
    payload           TEXT NOT NULL,
    state             TEXT NOT NULL,            -- pending|leased|done|failed|dead
    attempts          INTEGER NOT NULL DEFAULT 0,
    lease_owner       TEXT,
    lease_expires_utc TEXT,
    available_at_utc  TEXT NOT NULL,
    result_note_id    TEXT,
    last_error        TEXT,
    created_utc       TEXT NOT NULL,
    updated_utc       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks (session_id, state, available_at_utc);

CREATE TABLE IF NOT EXISTS notes (
    note_id         TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    mode            TEXT NOT NULL,
    target_id       TEXT,
    source_revision INTEGER NOT NULL,
    skill_version   TEXT NOT NULL,
    config_hash     TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    content_json    TEXT NOT NULL,
    content_md      TEXT NOT NULL,
    path            TEXT,
    agent           TEXT,
    provisional     INTEGER NOT NULL DEFAULT 0,
    state           TEXT NOT NULL DEFAULT 'active',  -- active|superseded
    created_utc     TEXT NOT NULL,
    updated_utc     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_target ON notes (session_id, mode, target_id, source_revision);

CREATE TABLE IF NOT EXISTS speaker_map (
    session_id   TEXT NOT NULL,
    speaker_id   TEXT NOT NULL,
    display_name TEXT,
    source       TEXT NOT NULL,     -- agenda|manual|auto
    updated_utc  TEXT NOT NULL,
    PRIMARY KEY (session_id, speaker_id)
);

CREATE TABLE IF NOT EXISTS segment_translations (
    session_id       TEXT NOT NULL,
    segment_id       TEXT NOT NULL,
    target_lang      TEXT NOT NULL,
    source_lang      TEXT,
    text             TEXT NOT NULL DEFAULT '',
    backend          TEXT NOT NULL,
    model            TEXT,
    confidence       REAL,
    segment_revision INTEGER NOT NULL DEFAULT 1,
    state            TEXT NOT NULL DEFAULT 'ok',   -- ok|error|skipped
    error            TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    created_utc      TEXT NOT NULL,
    updated_utc      TEXT NOT NULL,
    PRIMARY KEY (session_id, segment_id, target_lang)
);

CREATE INDEX IF NOT EXISTS idx_translations_lang ON segment_translations (session_id, target_lang);

CREATE TABLE IF NOT EXISTS session_log (
    session_id  TEXT NOT NULL,
    ts_utc      TEXT NOT NULL,
    level       TEXT NOT NULL,
    message     TEXT NOT NULL,
    data        TEXT
);

CREATE TABLE IF NOT EXISTS control (
    session_id  TEXT NOT NULL,
    command     TEXT NOT NULL,      -- stop|boundary|speaker|pause
    args        TEXT NOT NULL DEFAULT '{}',
    state       TEXT NOT NULL,      -- pending|applied|rejected
    result      TEXT,
    created_utc TEXT NOT NULL,
    updated_utc TEXT NOT NULL,
    id          INTEGER PRIMARY KEY AUTOINCREMENT
);

CREATE INDEX IF NOT EXISTS idx_control_state ON control (session_id, state, id);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def session_dir_name(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"sess_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"


class Database:
    """Thin, explicit wrapper around one SQLite file."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialise()

    # -- connection handling ------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def _initialise(self) -> None:
        with self._init_lock:
            conn = self.conn
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (SCHEMA_VERSION,),
            )

    # -- generic helpers ----------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params))

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        row = self.conn.execute(sql, params).fetchone()
        return row

    def log(self, session_id: str, level: str, message: str, data: Any = None) -> None:
        self.conn.execute(
            "INSERT INTO session_log (session_id, ts_utc, level, message, data) VALUES (?,?,?,?,?)",
            (session_id, utc_now(), level, message, json.dumps(data, ensure_ascii=False) if data is not None else None),
        )

    def logs(self, session_id: str, limit: int = 100) -> list[sqlite3.Row]:
        return self.query(
            "SELECT ts_utc, level, message, data FROM session_log WHERE session_id=? ORDER BY rowid DESC LIMIT ?",
            (session_id, limit),
        )


# --------------------------------------------------------------------------
# JSONL export helpers
# --------------------------------------------------------------------------
def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def write_jsonl(path: str | os.PathLike[str], rows: Iterable[dict[str, Any]]) -> int:
    """Write rows as JSONL (UTF-8, ``\\n`` terminated).  Returns row count."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    n = 0
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=False))
            fh.write("\n")
            n += 1
    os.replace(tmp, p)
    return n


def append_jsonl(path: str | os.PathLike[str], row: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, ensure_ascii=False))
        fh.write("\n")
