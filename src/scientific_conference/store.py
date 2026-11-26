"""Domain storage: sessions, audio chunks, talks, turns, segments, tasks, notes."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import SCHEMA_VERSION, SKILL_VERSION
from .config import Config, to_plain
from .db import Database, new_id, row_to_dict, utc_now, write_jsonl

# --------------------------------------------------------------------------
# session states
# --------------------------------------------------------------------------
STATE_STARTING = "starting"
STATE_RECORDING = "recording"
STATE_DRAINING = "draining"
STATE_CLOSED = "closed"
STATE_FAILED = "failed"
# A meeting record created before the real meeting.  It owns metadata (title,
# report identity, schedule, speaker dossiers) but never starts audio.  Its
# ``started_utc`` stays NULL until a recorder actually begins, so the meeting
# clock is never confused with the pre-creation time.
STATE_PLANNED = "planned"

TURN_OPEN = "open"
TURN_FINALIZED = "finalized"

MODE_TURN = "turn"
MODE_TALK = "talk"
MODE_SESSION = "session"

TASK_PENDING = "pending"
TASK_LEASED = "leased"
TASK_DONE = "done"
TASK_FAILED = "failed"
TASK_DEAD = "dead"


class StoreError(RuntimeError):
    pass


@dataclass
class SessionPaths:
    root: Path
    audio: Path
    converted: Path
    notes: Path
    transcript_jsonl: Path
    events_jsonl: Path
    db: Path
    manifest: Path
    log: Path


def session_paths(output_root: Path, session_id: str) -> SessionPaths:
    root = Path(output_root) / session_id
    return SessionPaths(
        root=root,
        audio=root / "audio",
        converted=root / "audio" / "asr16k",
        notes=root / "notes",
        transcript_jsonl=root / "transcript.jsonl",
        events_jsonl=root / "events.jsonl",
        db=root / "session.sqlite3",
        manifest=root / "manifest.json",
        log=root / "service.log",
    )


class Store:
    """All persistence for one session."""

    def __init__(self, db: Database, session_id: str, paths: SessionPaths | None = None):
        self.db = db
        self.session_id = session_id
        self.paths = paths

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    @classmethod
    def create(
        cls,
        output_root: Path,
        session_id: str,
        cfg: Config,
        *,
        device_id: str = "",
        device_name: str = "",
        sample_rate: int = 0,
        channels: int = 1,
        consent: bool = False,
        title: str = "",
        agenda: str = "",
        state: str = STATE_STARTING,
        created_utc: str | None = None,
    ) -> "Store":
        paths = session_paths(output_root, session_id)
        for p in (paths.root, paths.audio, paths.converted, paths.notes):
            p.mkdir(parents=True, exist_ok=True)
        db = Database(paths.db)
        now = utc_now()
        # Pre-created meetings keep their original creation time when a later
        # start reuses the same session row.  ``started_utc`` is never seeded
        # from a schedule: the recorder owns the authoritative meeting clock.
        created = created_utc or now
        with db.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sessions (session_id, state, created_utc, started_utc, device_id,"
                " device_name, sample_rate, channels, output_dir, config_json, config_hash, asr_backend,"
                " vad_backend, diarization, language_hints, consent, title, agenda, pid, updated_utc)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    state,
                    created,
                    None,
                    str(device_id),
                    device_name,
                    int(sample_rate),
                    int(channels),
                    str(paths.root),
                    json.dumps(to_plain(cfg), ensure_ascii=False),
                    cfg.asr_config_hash(),
                    cfg.asr.backend,
                    cfg.vad.backend,
                    cfg.diarization.backend,
                    json.dumps(cfg.asr.language_hints, ensure_ascii=False),
                    1 if consent else 0,
                    title,
                    agenda,
                    None,
                    now,
                ),
            )
        store = cls(db, session_id, paths)
        store.write_manifest()
        return store

    @classmethod
    def open(cls, output_root: Path, session_id: str) -> "Store":
        paths = session_paths(output_root, session_id)
        if not paths.db.exists():
            raise StoreError(f"session database not found: {paths.db}")
        return cls(Database(paths.db), session_id, paths)

    # -- accessors ------------------------------------------------------
    def session(self) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM sessions WHERE session_id=?", (self.session_id,))
        if row is None:
            raise StoreError(f"unknown session {self.session_id}")
        return dict(row)

    def session_state(self) -> str:
        row = self.db.query_one("SELECT state FROM sessions WHERE session_id=?", (self.session_id,))
        return str(row["state"]) if row else STATE_FAILED

    def set_state(self, state: str, *, error: str | None = None) -> None:
        now = utc_now()
        fields = ["state=?", "updated_utc=?"]
        params: list[Any] = [state, now]
        if state == STATE_RECORDING:
            fields.append("started_utc=COALESCE(started_utc, ?)")
            params.append(now)
        if state in (STATE_CLOSED, STATE_FAILED):
            fields.append("ended_utc=?")
            params.append(now)
        if error is not None:
            fields.append("last_error=?")
            params.append(error)
        params.append(self.session_id)
        with self.db.tx() as conn:
            conn.execute(f"UPDATE sessions SET {', '.join(fields)} WHERE session_id=?", params)

    def set_pid(self, pid: int | None) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE sessions SET pid=?, updated_utc=? WHERE session_id=?",
                (pid, utc_now(), self.session_id),
            )

    def set_control(self, **kwargs: Any) -> None:
        if not kwargs:
            return
        cols = ", ".join(f"{k}=?" for k in kwargs)
        with self.db.tx() as conn:
            conn.execute(
                f"UPDATE sessions SET {cols}, updated_utc=? WHERE session_id=?",
                [*kwargs.values(), utc_now(), self.session_id],
            )

    # ------------------------------------------------------------------
    # audio chunks
    # ------------------------------------------------------------------
    def add_chunk(
        self,
        chunk_index: int,
        *,
        path: str | None,
        converted_path: str | None,
        start_ms: int,
        end_ms: int,
        frames: int,
        sample_rate: int,
        channels: int,
        nbytes: int = 0,
        sha256: str | None = None,
        gap_before_ms: int = 0,
    ) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO audio_chunks (session_id, chunk_index, path, converted_path, start_ms,"
                " end_ms, frames, sample_rate, channels, bytes, sha256, gap_before_ms, created_utc)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.session_id,
                    chunk_index,
                    path,
                    converted_path,
                    start_ms,
                    end_ms,
                    frames,
                    sample_rate,
                    channels,
                    nbytes,
                    sha256,
                    gap_before_ms,
                    utc_now(),
                ),
            )

    def chunks(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM audio_chunks WHERE session_id=? ORDER BY chunk_index", (self.session_id,)
            )
        ]

    def audio_stats(self) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT COUNT(*) AS chunks, COALESCE(MAX(end_ms),0) AS duration_ms,"
            " COALESCE(SUM(bytes),0) AS bytes, COALESCE(SUM(gap_before_ms),0) AS gap_ms"
            " FROM audio_chunks WHERE session_id=?",
            (self.session_id,),
        )
        return dict(row) if row else {"chunks": 0, "duration_ms": 0, "bytes": 0, "gap_ms": 0}

    # ------------------------------------------------------------------
    # speaker map
    # ------------------------------------------------------------------
    def set_speaker(self, speaker_id: str, display_name: str | None, source: str = "manual") -> None:
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO speaker_map (session_id, speaker_id, display_name, source, updated_utc)"
                " VALUES (?,?,?,?,?) ON CONFLICT(session_id, speaker_id) DO UPDATE SET"
                " display_name=excluded.display_name, source=excluded.source, updated_utc=excluded.updated_utc",
                (self.session_id, speaker_id, display_name, source, utc_now()),
            )

    def speaker_name(self, speaker_id: str | None) -> str | None:
        if not speaker_id:
            return None
        row = self.db.query_one(
            "SELECT display_name FROM speaker_map WHERE session_id=? AND speaker_id=?",
            (self.session_id, speaker_id),
        )
        return row["display_name"] if row else None

    def speakers(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM speaker_map WHERE session_id=? ORDER BY speaker_id", (self.session_id,)
            )
        ]

    # ------------------------------------------------------------------
    # talks
    # ------------------------------------------------------------------
    def open_talk(self, talk_id: str, start_ms: int, speaker_id: str | None = None, title: str = "") -> None:
        now = utc_now()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO talks (session_id, talk_id, title, speaker_id, start_ms, end_ms, revision, state,"
                " created_utc, updated_utc) VALUES (?,?,?,?,?,NULL,1,'open',?,?)"
                " ON CONFLICT(session_id, talk_id) DO NOTHING",
                (self.session_id, talk_id, title, speaker_id, start_ms, now, now),
            )

    def close_talk(self, talk_id: str, end_ms: int, *, speaker_id: str | None = None, title: str | None = None) -> None:
        now = utc_now()
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE talks SET end_ms=?, state='closed', speaker_id=COALESCE(?, speaker_id),"
                " title=COALESCE(?, title), updated_utc=? WHERE session_id=? AND talk_id=?",
                (end_ms, speaker_id, title, now, self.session_id, talk_id),
            )

    def newest_open_talk(self) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM talks WHERE session_id=? AND state='open' ORDER BY start_ms DESC LIMIT 1",
            (self.session_id,),
        )
        return dict(row) if row else None

    def talk(self, talk_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM talks WHERE session_id=? AND talk_id=?", (self.session_id, talk_id)
        )
        return dict(row) if row else None

    def talks(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM talks WHERE session_id=? ORDER BY start_ms", (self.session_id,)
            )
        ]

    # ------------------------------------------------------------------
    # turns
    # ------------------------------------------------------------------
    def open_turn(
        self, turn_id: str, start_ms: int, *, talk_id: str | None = None, speaker_id: str | None = None
    ) -> None:
        now = utc_now()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO turns (session_id, turn_id, talk_id, speaker_id, start_ms, end_ms, revision,"
                " boundary_reason, state, created_utc, updated_utc) VALUES (?,?,?,?,?,NULL,1,NULL,'open',?,?)"
                " ON CONFLICT(session_id, turn_id) DO NOTHING",
                (self.session_id, turn_id, talk_id, speaker_id, start_ms, now, now),
            )

    def update_turn(
        self,
        turn_id: str,
        *,
        end_ms: int | None = None,
        speaker_id: str | None = None,
        talk_id: str | None = None,
        state: str | None = None,
        boundary_reason: str | None = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        for col, value in (
            ("end_ms", end_ms),
            ("speaker_id", speaker_id),
            ("talk_id", talk_id),
            ("state", state),
            ("boundary_reason", boundary_reason),
        ):
            if value is not None:
                sets.append(f"{col}=?")
                params.append(value)
        if not sets:
            return
        sets.append("updated_utc=?")
        params.extend([utc_now(), self.session_id, turn_id])
        with self.db.tx() as conn:
            conn.execute(
                f"UPDATE turns SET {', '.join(sets)} WHERE session_id=? AND turn_id=?", params
            )

    def bump_turn_revision(self, turn_id: str) -> int:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE turns SET revision=revision+1, updated_utc=? WHERE session_id=? AND turn_id=?",
                (utc_now(), self.session_id, turn_id),
            )
            row = conn.execute(
                "SELECT revision FROM turns WHERE session_id=? AND turn_id=?", (self.session_id, turn_id)
            ).fetchone()
        return int(row["revision"]) if row else 1

    def turn(self, turn_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM turns WHERE session_id=? AND turn_id=?", (self.session_id, turn_id)
        )
        return dict(row) if row else None

    def turns(self, *, state: str | None = None) -> list[dict[str, Any]]:
        if state:
            rows = self.db.query(
                "SELECT * FROM turns WHERE session_id=? AND state=? ORDER BY start_ms",
                (self.session_id, state),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM turns WHERE session_id=? ORDER BY start_ms", (self.session_id,)
            )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # segments
    # ------------------------------------------------------------------
    def add_segment(
        self,
        segment_id: str,
        *,
        start_ms: int,
        end_ms: int,
        text: str,
        turn_id: str | None = None,
        talk_id: str | None = None,
        is_final: bool = True,
        revision: int = 1,
        speaker_id: str | None = None,
        asr_confidence: float | None = None,
        flags: Sequence[str] = (),
        backend: str | None = None,
        window_id: str | None = None,
        source: str = "asr",
    ) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO segments (session_id, segment_id, turn_id, talk_id, start_ms, end_ms,"
                " text, is_final, revision, speaker_id, asr_confidence, flags, backend, window_id, source,"
                " superseded_by, created_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
                (
                    self.session_id,
                    segment_id,
                    turn_id,
                    talk_id,
                    start_ms,
                    end_ms,
                    text,
                    1 if is_final else 0,
                    revision,
                    speaker_id,
                    asr_confidence,
                    json.dumps(list(flags), ensure_ascii=False),
                    backend,
                    window_id,
                    source,
                    utc_now(),
                ),
            )

    def supersede_segments(self, segment_ids: Iterable[str], superseded_by: str) -> None:
        ids = list(segment_ids)
        if not ids:
            return
        with self.db.tx() as conn:
            conn.executemany(
                "UPDATE segments SET superseded_by=? WHERE session_id=? AND segment_id=?",
                [(superseded_by, self.session_id, sid) for sid in ids],
            )

    def segments(
        self,
        *,
        turn_id: str | None = None,
        talk_id: str | None = None,
        include_superseded: bool = False,
        only_final: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM segments WHERE session_id=?"
        params: list[Any] = [self.session_id]
        if turn_id:
            sql += " AND turn_id=?"
            params.append(turn_id)
        if talk_id:
            sql += " AND talk_id=?"
            params.append(talk_id)
        if not include_superseded:
            sql += " AND superseded_by IS NULL"
        if only_final:
            sql += " AND is_final=1"
        sql += " ORDER BY start_ms, segment_id"
        rows = [dict(r) for r in self.db.query(sql, params)]
        edits = {r['segment_id']: dict(r) for r in self.db.query(
            "SELECT * FROM transcript_edits WHERE session_id=?", (self.session_id,))}
        for row in rows:
            edit = edits.get(row['segment_id'])
            if edit and edit['original'] == row['text'] and edit['corrected'] != row['text']:
                row['raw_text'] = row['text']
                row['text'] = edit['corrected']
                row['correction_reason'] = edit['reason']
        from .review_layers import overlay
        return overlay(self,rows)

    def segment(self, segment_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM segments WHERE session_id=? AND segment_id=?",
            (self.session_id, segment_id),
        )
        return dict(row) if row else None

    def max_segment_revision(self, turn_id: str) -> int:
        row = self.db.query_one(
            "SELECT COALESCE(MAX(revision),0) AS m FROM segments WHERE session_id=? AND turn_id=?",
            (self.session_id, turn_id),
        )
        return int(row["m"]) if row else 0

    # ------------------------------------------------------------------
    # tasks (runner queue)
    # ------------------------------------------------------------------
    def enqueue_task(
        self,
        *,
        type: str,
        mode: str,
        target_id: str | None,
        source_revision: int,
        payload: dict[str, Any],
        idempotency_key: str,
        event_seq: int | None = None,
    ) -> tuple[str, bool]:
        task_id = new_id("task")
        now = utc_now()
        with self.db.tx() as conn:
            existing = conn.execute(
                "SELECT task_id FROM tasks WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                return str(existing["task_id"]), False
            conn.execute(
                "INSERT INTO tasks (task_id, session_id, event_seq, type, mode, target_id, source_revision,"
                " idempotency_key, payload, state, attempts, available_at_utc, created_utc, updated_utc)"
                " VALUES (?,?,?,?,?,?,?,?,?,'pending',0,?,?,?)",
                (
                    task_id,
                    self.session_id,
                    event_seq,
                    type,
                    mode,
                    target_id,
                    int(source_revision),
                    idempotency_key,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    now,
                    now,
                ),
            )
        return task_id, True

    def lease_tasks(self, owner: str, limit: int, lease_seconds: int) -> list[dict[str, Any]]:
        """Atomically lease up to ``limit`` due tasks."""
        now = utc_now()
        leased: list[dict[str, Any]] = []
        with self.db.tx() as conn:
            # Reclaim expired leases first.
            conn.execute(
                "UPDATE tasks SET state='pending', lease_owner=NULL, lease_expires_utc=NULL, updated_utc=?"
                " WHERE session_id=? AND state='leased' AND (lease_expires_utc IS NULL OR lease_expires_utc<?)",
                (now, self.session_id, now),
            )
            rows = conn.execute(
                "SELECT * FROM tasks WHERE session_id=? AND state IN ('pending','failed')"
                " AND available_at_utc<=? ORDER BY created_utc LIMIT ?",
                (self.session_id, now, int(limit)),
            ).fetchall()
            for row in rows:
                expires = _utc_plus(lease_seconds)
                conn.execute(
                    "UPDATE tasks SET state='leased', lease_owner=?, lease_expires_utc=?, updated_utc=?"
                    " WHERE task_id=?",
                    (owner, expires, now, row["task_id"]),
                )
                item = dict(row)
                item["state"] = TASK_LEASED
                item["lease_owner"] = owner
                item["lease_expires_utc"] = expires
                leased.append(item)
        return leased

    def complete_task(self, task_id: str, note_id: str | None = None) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE tasks SET state='done', result_note_id=?, lease_owner=NULL, lease_expires_utc=NULL,"
                " updated_utc=? WHERE task_id=?",
                (note_id, utc_now(), task_id),
            )

    def fail_task(self, task_id: str, error: str, *, retry_at: str | None, dead: bool) -> None:
        with self.db.tx() as conn:
            if dead:
                conn.execute(
                    "UPDATE tasks SET state='dead', last_error=?, lease_owner=NULL, lease_expires_utc=NULL,"
                    " available_at_utc=?, updated_utc=? WHERE task_id=?",
                    (error, utc_now(), utc_now(), task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET state='failed', attempts=attempts+1, last_error=?, lease_owner=NULL,"
                    " lease_expires_utc=NULL, available_at_utc=?, updated_utc=? WHERE task_id=?",
                    (error, retry_at or utc_now(), utc_now(), task_id),
                )

    def task(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        return dict(row) if row else None

    def tasks(self, state: str | None = None) -> list[dict[str, Any]]:
        if state:
            rows = self.db.query(
                "SELECT * FROM tasks WHERE session_id=? AND state=? ORDER BY created_utc",
                (self.session_id, state),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM tasks WHERE session_id=? ORDER BY created_utc", (self.session_id,)
            )
        return [dict(r) for r in rows]

    def task_counts(self) -> dict[str, int]:
        rows = self.db.query(
            "SELECT state, COUNT(*) AS n FROM tasks WHERE session_id=? GROUP BY state", (self.session_id,)
        )
        return {r["state"]: int(r["n"]) for r in rows}

    # ------------------------------------------------------------------
    # notes
    # ------------------------------------------------------------------
    def save_note(
        self,
        *,
        mode: str,
        target_id: str | None,
        source_revision: int,
        content_json: dict[str, Any],
        content_md: str,
        agent: str,
        config_hash: str,
        idempotency_key: str,
        path: str | None = None,
        skill_version: str = SKILL_VERSION,
        provisional: bool = False,
        validate_source=None,
    ) -> tuple[str, bool, str | None]:
        """Persist a note.

        Returns ``(note_id, created, rejected_reason)``.  A note derived from an
        older transcript revision never overwrites a newer one, and a final note
        always supersedes a provisional note for the same revision.
        """
        note_id = new_id("note")
        now = utc_now()
        with self.db.tx() as conn:
            if validate_source is not None:
                validate_source()
            if '_review_epoch' in content_json:
                from .review_layers import epoch
                if content_json['_review_epoch']!=epoch(self):
                    return '',False,'Human review changed while summary was generated; request a new summary'

            existing = conn.execute(
                "SELECT note_id, source_revision FROM notes WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                return str(existing["note_id"]), False, None

            newer = conn.execute(
                "SELECT note_id, source_revision FROM notes WHERE session_id=? AND mode=? AND"
                " IFNULL(target_id,'')=IFNULL(?,'') AND source_revision>? AND state='active'"
                " ORDER BY source_revision DESC LIMIT 1",
                (self.session_id, mode, target_id, int(source_revision)),
            ).fetchone()
            if newer is not None:
                return "", False, (
                    f"stale result rejected: revision {source_revision} < existing revision"
                    f" {newer['source_revision']} (note {newer['note_id']})"
                )

            # Everything older is superseded; a provisional note of the same
            # revision is superseded by the final note that replaces it.
            stale = conn.execute(
                "SELECT note_id FROM notes WHERE session_id=? AND mode=? AND IFNULL(target_id,'')=IFNULL(?,'')"
                " AND state='active' AND (source_revision<? OR (source_revision=? AND (provisional=1 OR ?=0)))",
                (self.session_id, mode, target_id, int(source_revision), int(source_revision), 1 if provisional else 0),
            ).fetchall()
            for row in stale:
                conn.execute(
                    "UPDATE notes SET state='superseded', updated_utc=? WHERE note_id=?",
                    (now, row["note_id"]),
                )

            # A provisional note that arrives after the final note for the same
            # revision is stored as superseded straight away, so the two can
            # never both be active.
            final_exists = conn.execute(
                "SELECT 1 FROM notes WHERE session_id=? AND mode=? AND IFNULL(target_id,'')=IFNULL(?,'')"
                " AND source_revision=? AND provisional=0 AND state='active' LIMIT 1",
                (self.session_id, mode, target_id, int(source_revision)),
            ).fetchone()
            state = "superseded" if (provisional and final_exists) else "active"

            conn.execute(
                "INSERT INTO notes (note_id, session_id, mode, target_id, source_revision, skill_version,"
                " config_hash, idempotency_key, content_json, content_md, path, agent, provisional, state,"
                " created_utc, updated_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    note_id,
                    self.session_id,
                    mode,
                    target_id,
                    int(source_revision),
                    skill_version,
                    config_hash,
                    idempotency_key,
                    json.dumps(content_json, ensure_ascii=False),
                    content_md,
                    path,
                    agent,
                    1 if provisional else 0,
                    state,
                    now,
                    now,
                ),
            )

            # Keep exactly one active note per target: the newest revision, and
            # among equal revisions the final (non-provisional) one.
            conn.execute(
                "UPDATE notes SET state='superseded', updated_utc=? WHERE session_id=? AND mode=?"
                " AND IFNULL(target_id,'')=IFNULL(?,'') AND note_id<>? AND state='active'"
                " AND (source_revision<? OR (source_revision=? AND (provisional=1 OR ?=0)))",
                (now, self.session_id, mode, target_id, note_id, int(source_revision), int(source_revision), 1 if provisional else 0),
            )
        return note_id, True, None

    def note(self, note_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM notes WHERE note_id=?", (note_id,))
        return dict(row) if row else None

    def active_note(self, mode: str, target_id: str | None) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM notes WHERE session_id=? AND mode=? AND IFNULL(target_id,'')=IFNULL(?,'')"
            " AND state='active' ORDER BY source_revision DESC LIMIT 1",
            (self.session_id, mode, target_id),
        )
        return dict(row) if row else None

    def notes(self, *, mode: str | None = None, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM notes WHERE session_id=?"
        params: list[Any] = [self.session_id]
        if mode:
            sql += " AND mode=?"
            params.append(mode)
        if active_only:
            sql += " AND state='active'"
        sql += " ORDER BY created_utc"
        return [dict(r) for r in self.db.query(sql, params)]

    # ------------------------------------------------------------------
    # control channel (CLI/MCP -> service process)
    # ------------------------------------------------------------------
    def push_control(self, command: str, args: dict[str, Any] | None = None) -> int:
        now = utc_now()
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO control (session_id, command, args, state, created_utc, updated_utc)"
                " VALUES (?,?,?,'pending',?,?)",
                (self.session_id, command, json.dumps(args or {}, ensure_ascii=False), now, now),
            )
            return int(cur.lastrowid or 0)

    def pop_control(self, limit: int = 10, *, reclaim_after_s: float = 60.0) -> list[dict[str, Any]]:
        """Claim pending control rows for this process.

        Rows are marked ``claimed`` while they are being applied, so that a
        command is never executed twice; a claim that was never resolved (the
        service crashed mid-command) becomes eligible again after
        ``reclaim_after_s``.
        """
        now = utc_now()
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE control SET state='pending', updated_utc=?"
                " WHERE session_id=? AND state='claimed' AND updated_utc < ?",
                (now, self.session_id, _utc_minus(reclaim_after_s)),
            )
            rows = conn.execute(
                "SELECT * FROM control WHERE session_id=? AND state='pending' ORDER BY id LIMIT ?",
                (self.session_id, int(limit)),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE control SET state='claimed', updated_utc=? WHERE id=?", (now, row["id"])
                )
        return [dict(r) for r in rows]

    def resolve_control(self, control_id: int, state: str, result: Any = None) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE control SET state=?, result=?, updated_utc=? WHERE id=?",
                (state, json.dumps(result, ensure_ascii=False) if result is not None else None, utc_now(), control_id),
            )

    # ------------------------------------------------------------------
    # translations
    # ------------------------------------------------------------------
    def save_translation(
        self,
        segment_id: str,
        *,
        target_lang: str,
        text: str,
        backend: str,
        model: str | None = None,
        source_lang: str | None = None,
        confidence: float | None = None,
        segment_revision: int = 1,
        source_review_revision: int = 0,
        state: str = "ok",
        error: str | None = None,
        attempts: int = 0,
    ) -> None:
        now = utc_now()
        with self.db.tx() as conn:
            from .review_layers import record_ai
            record_ai(conn,self.session_id,segment_id,'translation:'+target_lang,text)
            conn.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('translation_review:'+target_lang+':'+segment_id,str(source_review_revision)))
            conn.execute(
                "INSERT INTO segment_translations (session_id, segment_id, target_lang, source_lang, text,"
                " backend, model, confidence, segment_revision, state, error, attempts, created_utc, updated_utc)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(session_id, segment_id, target_lang) DO UPDATE SET"
                " text=excluded.text, backend=excluded.backend, model=excluded.model,"
                " source_lang=excluded.source_lang, confidence=excluded.confidence,"
                " segment_revision=excluded.segment_revision, state=excluded.state,"
                " error=excluded.error, attempts=excluded.attempts, updated_utc=excluded.updated_utc",
                (
                    self.session_id,
                    segment_id,
                    target_lang,
                    source_lang,
                    text,
                    backend,
                    model,
                    confidence,
                    int(segment_revision),
                    state,
                    error,
                    int(attempts),
                    now,
                    now,
                ),
            )

    def translation(self, segment_id: str, target_lang: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM segment_translations WHERE session_id=? AND segment_id=? AND target_lang=?",
            (self.session_id, segment_id, target_lang),
        )
        from .review_layers import translation_overlay
        if row is None:
            from .review_layers import head
            h=head(self,segment_id,'translation:'+target_lang)
            if h['value'] is not None:return dict(segment_id=segment_id,target_lang=target_lang,text=h['value'],human_confirmed=True,state='ok',review_revision=h['revision'])
        return translation_overlay(self,row)

    def translations(self, target_lang: str | None = None) -> list[dict[str, Any]]:
        if target_lang:
            rows = self.db.query(
                "SELECT * FROM segment_translations WHERE session_id=? AND target_lang=? ORDER BY segment_id",
                (self.session_id, target_lang),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM segment_translations WHERE session_id=? ORDER BY segment_id",
                (self.session_id,),
            )
        from .review_layers import translation_overlay
        result=[translation_overlay(self,r) for r in rows]
        existing={(r["segment_id"],r["target_lang"]) for r in result}
        for h in self.db.query("SELECT * FROM review_heads WHERE session_id=? AND field LIKE 'translation:%' AND value IS NOT NULL",(self.session_id,)):
            lang=h["field"].split(":",1)[1]
            if (not target_lang or target_lang==lang) and (h["segment_id"],lang) not in existing:
                result.append(dict(segment_id=h["segment_id"],target_lang=lang,text=h["value"],human_confirmed=True,state="ok",review_revision=h["revision"]))
        return result

    def translation_map(self, target_lang: str) -> dict[str, dict[str, Any]]:
        """``segment_id -> translation`` for the live view and the API."""
        return {row["segment_id"]: row for row in self.translations(target_lang)}

    def untranslated_segments(
        self, target_lang: str, *, limit: int = 20, retry_errors: bool = True, max_attempts: int = 3
    ) -> list[dict[str, Any]]:
        """Final, non-superseded segments that still need a translation.

        A segment is pending when it has no row for ``target_lang``, when the row
        predates a newer segment revision, or when an earlier attempt failed and
        the retry budget is not exhausted.
        """
        sql = (
            "SELECT s.* FROM segments s"
            " LEFT JOIN segment_translations t"
            "   ON t.session_id=s.session_id AND t.segment_id=s.segment_id AND t.target_lang=?"
            " LEFT JOIN review_heads rh ON rh.session_id=s.session_id AND rh.segment_id=s.segment_id AND rh.field='transcript'"
            " LEFT JOIN meta rm ON rm.key=('translation_review:' || ? || ':' || s.segment_id)"
            " WHERE s.session_id=? AND s.superseded_by IS NULL AND s.is_final=1"
            "   AND TRIM(s.text) <> ''"
            "   AND (t.segment_id IS NULL"
            "        OR t.segment_revision < s.revision"
            "        OR COALESCE(CAST(rm.value AS INTEGER),0) < COALESCE(rh.revision,0)"
            "        OR (t.state='error' AND t.attempts < ?))"
            " AND NOT EXISTS (SELECT 1 FROM review_heads hm WHERE hm.session_id=s.session_id AND hm.segment_id=s.segment_id AND hm.field=? AND hm.value IS NOT NULL)"
            " ORDER BY s.start_ms LIMIT ?"
        )
        params: list[Any] = [target_lang,target_lang,self.session_id,int(max_attempts),"translation:"+target_lang,int(limit)]
        if not retry_errors:
            sql = sql.replace("OR (t.state='error' AND t.attempts < ?)", "OR (t.state='error' AND t.attempts < 0)")
        ids=[r["segment_id"] for r in self.db.query(sql,params)]
        return [s for s in self.segments() if s["segment_id"] in ids and not (self.translation(s["segment_id"],target_lang) or {}).get("human_confirmed")]

    def translation_counts(self, target_lang: str | None = None) -> dict[str, int]:
        if target_lang:
            rows = self.db.query(
                "SELECT state, COUNT(*) AS n FROM segment_translations WHERE session_id=? AND target_lang=?"
                " GROUP BY state",
                (self.session_id, target_lang),
            )
        else:
            rows = self.db.query(
                "SELECT state, COUNT(*) AS n FROM segment_translations WHERE session_id=? GROUP BY state",
                (self.session_id,),
            )
        return {row["state"]: int(row["n"]) for row in rows}

    # ------------------------------------------------------------------
    # logging
    # ------------------------------------------------------------------
    def log(self, level: str, message: str, data: Any = None) -> None:
        self.db.log(self.session_id, level, message, data)

    def logs(self, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.logs(self.session_id, limit)]

    # ------------------------------------------------------------------
    # derived artefacts
    # ------------------------------------------------------------------
    def transcript_rows(self) -> list[dict[str, Any]]:
        rows = self.segments(include_superseded=True)
        out = []
        for r in rows:
            item = dict(r)
            item["is_final"] = bool(item["is_final"])
            try:
                item["flags"] = json.loads(item["flags"])
            except Exception:
                item["flags"] = []
            out.append(item)
        return out

    def export_transcript_jsonl(self) -> int:
        assert self.paths is not None
        return write_jsonl(self.paths.transcript_jsonl, self.transcript_rows())

    def export_events_jsonl(self) -> int:
        assert self.paths is not None
        rows = [json.loads(r["payload"]) for r in self.db.query(
            "SELECT payload FROM events WHERE session_id=? ORDER BY seq", (self.session_id,)
        )]
        return write_jsonl(self.paths.events_jsonl, rows)

    def write_manifest(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self.paths is not None
        session = self.session()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "skill_version": SKILL_VERSION,
            "session_id": self.session_id,
            "state": session["state"],
            "created_utc": session["created_utc"],
            "started_utc": session["started_utc"],
            "ended_utc": session["ended_utc"],
            "device": {
                "id": session["device_id"],
                "name": session["device_name"],
                "sample_rate": session["sample_rate"],
                "channels": session["channels"],
            },
            "backends": {
                "asr": session["asr_backend"],
                "vad": session["vad_backend"],
                "diarization": session["diarization"],
            },
            "asr_config_hash": session["config_hash"],
            "consent_recorded": bool(session["consent"]),
            "title": session["title"],
            "agenda": session["agenda"],
            "counts": {
                "chunks": self.audio_stats()["chunks"],
                "duration_ms": self.audio_stats()["duration_ms"],
                "segments": len(self.segments(include_superseded=True)),
                "turns": len(self.turns()),
                "talks": len(self.talks()),
                "events": self.db.query_one(
                    "SELECT COUNT(*) AS n FROM events WHERE session_id=?", (self.session_id,)
                )["n"],
                "notes": len(self.notes(active_only=False)),
            },
        }
        if extra:
            manifest.update(extra)
        self.paths.manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest


def _utc_plus(seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _utc_minus(seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (
        (datetime.now(timezone.utc) - timedelta(seconds=seconds))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def index_sessions(output_root: Path) -> list[dict[str, Any]]:
    """List meeting directories with their manifest summary."""
    root = Path(output_root)
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        manifest = child / "manifest.json"
        if not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            continue
        data["output_dir"] = str(child)
        out.append(data)
    return out
