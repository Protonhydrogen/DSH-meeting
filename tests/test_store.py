"""Storage semantics: revisions, leases, control channel and JSONL export."""

from __future__ import annotations

import json

import pytest

from scientific_conference.db import utc_now
from scientific_conference.store import (
    MODE_TALK,
    MODE_TURN,
    STATE_CLOSED,
    STATE_RECORDING,
    Store,
    index_sessions,
    session_paths,
)


@pytest.fixture()
def store(cfg):
    return Store.create(cfg.output_root, "sess_test", cfg, device_id="test", consent=True, title="T")


def test_session_lifecycle(store):
    assert store.session_state() == "starting"
    store.set_state(STATE_RECORDING)
    assert store.session_state() == STATE_RECORDING
    assert store.session()["started_utc"] is not None
    store.set_state(STATE_CLOSED)
    assert store.session()["ended_utc"] is not None
    assert store.paths.root.exists()


def test_paths_layout(cfg, tmp_path):
    paths = session_paths(cfg.output_root, "s1")
    assert paths.audio.parent == paths.root
    assert paths.notes.name == "notes"
    assert paths.db.name == "session.sqlite3"
    assert paths.transcript_jsonl.name == "transcript.jsonl"


def test_chunks_and_audio_stats(store):
    store.add_chunk(1, path="audio/c1.wav", converted_path=None, start_ms=0, end_ms=1000,
                    frames=1000, sample_rate=1000, channels=1, nbytes=2048)
    store.add_chunk(2, path="audio/c2.wav", converted_path=None, start_ms=1100, end_ms=2000,
                    frames=900, sample_rate=1000, channels=1, nbytes=2048, gap_before_ms=100)
    stats = store.audio_stats()
    assert stats["chunks"] == 2
    assert stats["duration_ms"] == 2000
    assert stats["gap_ms"] == 100


def test_segments_and_supersede(store):
    store.add_segment("seg_1", start_ms=0, end_ms=1000, text="a", turn_id="turn_001", revision=1)
    store.add_segment("seg_2", start_ms=1000, end_ms=2000, text="b", turn_id="turn_001", revision=2)
    store.supersede_segments(["seg_1"], "seg_2")
    assert [s["segment_id"] for s in store.segments(turn_id="turn_001")] == ["seg_2"]
    assert len(store.segments(turn_id="turn_001", include_superseded=True)) == 2
    assert store.max_segment_revision("turn_001") == 2


def test_speaker_map(store):
    store.set_speaker("speaker_01", "张三", "agenda")
    store.set_speaker("speaker_02", "李四", "manual")
    assert store.speaker_name("speaker_01") == "张三"
    store.set_speaker("speaker_01", "张三（教授）", "manual")
    assert store.speaker_name("speaker_01") == "张三（教授）"
    assert len(store.speakers()) == 2


def test_note_revision_guard(store):
    note_id, created, rejected = store.save_note(
        mode=MODE_TURN, target_id="turn_001", source_revision=2,
        content_json={"summary": "v2", "findings": []}, content_md="v2",
        agent="test", config_hash="h", idempotency_key="k2",
    )
    assert created and rejected is None
    stale_id, created2, rejected2 = store.save_note(
        mode=MODE_TURN, target_id="turn_001", source_revision=1,
        content_json={"summary": "v1", "findings": []}, content_md="v1",
        agent="test", config_hash="h", idempotency_key="k1",
    )
    assert not created2
    assert rejected2 and "stale" in rejected2
    assert store.active_note(MODE_TURN, "turn_001")["note_id"] == note_id

    newer_id, created3, _ = store.save_note(
        mode=MODE_TURN, target_id="turn_001", source_revision=3,
        content_json={"summary": "v3", "findings": []}, content_md="v3",
        agent="test", config_hash="h", idempotency_key="k3",
    )
    assert created3
    assert store.note(note_id)["state"] == "superseded"
    assert store.active_note(MODE_TURN, "turn_001")["note_id"] == newer_id


def test_note_idempotency(store):
    first, created, _ = store.save_note(
        mode=MODE_TALK, target_id="talk_001", source_revision=1,
        content_json={"summary": "s"}, content_md="m", agent="a", config_hash="h",
        idempotency_key="same",
    )
    second, created2, _ = store.save_note(
        mode=MODE_TALK, target_id="talk_001", source_revision=1,
        content_json={"summary": "s"}, content_md="m", agent="a", config_hash="h",
        idempotency_key="same",
    )
    assert created and not created2 and first == second
    assert len(store.notes(active_only=False)) == 1


def test_provisional_then_final_supersedes(store):
    provisional, _, _ = store.save_note(
        mode=MODE_TURN, target_id="turn_009", source_revision=1,
        content_json={"summary": "prov"}, content_md="p", agent="a", config_hash="h",
        idempotency_key="prov", provisional=True,
    )
    final, _, _ = store.save_note(
        mode=MODE_TURN, target_id="turn_009", source_revision=1,
        content_json={"summary": "final"}, content_md="f", agent="a", config_hash="h",
        idempotency_key="final",
    )
    assert store.note(provisional)["state"] == "superseded"
    assert store.active_note(MODE_TURN, "turn_009")["note_id"] == final


def test_final_then_provisional_keeps_one_active(store):
    """Out-of-order delivery must not leave two active notes for one target."""
    final, _, _ = store.save_note(
        mode=MODE_TURN, target_id="turn_010", source_revision=1,
        content_json={"summary": "final"}, content_md="f", agent="a", config_hash="h",
        idempotency_key="final",
    )
    late, _, _ = store.save_note(
        mode=MODE_TURN, target_id="turn_010", source_revision=1,
        content_json={"summary": "prov"}, content_md="p", agent="a", config_hash="h",
        idempotency_key="prov", provisional=True,
    )
    assert store.note(late)["state"] == "superseded"
    assert store.active_note(MODE_TURN, "turn_010")["note_id"] == final
    assert len(store.notes(active_only=True)) == 1


def test_task_lease_and_complete(store):
    task_id, created = store.enqueue_task(
        type="note.turn", mode=MODE_TURN, target_id="turn_001", source_revision=1,
        payload={"a": 1}, idempotency_key="x", event_seq=2,
    )
    assert created
    again, created2 = store.enqueue_task(
        type="note.turn", mode=MODE_TURN, target_id="turn_001", source_revision=1,
        payload={"a": 1}, idempotency_key="x", event_seq=2,
    )
    assert not created2 and again == task_id

    leased = store.lease_tasks("worker-1", 10, 60)
    assert len(leased) == 1 and leased[0]["state"] == "leased"
    # A second worker must not get the same task while the lease is valid.
    assert store.lease_tasks("worker-2", 10, 60) == []
    store.complete_task(task_id, "note_1")
    assert store.task(task_id)["state"] == "done"
    assert store.task_counts() == {"done": 1}


def test_expired_lease_is_reclaimed(store):
    store.enqueue_task(type="note.turn", mode=MODE_TURN, target_id="t", source_revision=1,
                       payload={}, idempotency_key="x", event_seq=1)
    store.lease_tasks("worker-1", 1, lease_seconds=-1)  # already expired
    reclaimed = store.lease_tasks("worker-2", 1, 60)
    assert len(reclaimed) == 1
    assert reclaimed[0]["lease_owner"] == "worker-2"


def test_task_failure_and_dead_letter(store):
    task_id, _ = store.enqueue_task(type="note.turn", mode=MODE_TURN, target_id="t",
                                    source_revision=1, payload={}, idempotency_key="y", event_seq=1)
    store.fail_task(task_id, "boom", retry_at=utc_now(), dead=False)
    assert store.task(task_id)["state"] == "failed"
    assert store.task(task_id)["attempts"] == 1
    store.fail_task(task_id, "boom again", retry_at=None, dead=True)
    assert store.task(task_id)["state"] == "dead"
    assert store.task_counts()["dead"] == 1


def test_control_channel(store):
    control_id = store.push_control("boundary", {"kind": "talk"})
    pending = store.pop_control()
    assert len(pending) == 1 and pending[0]["command"] == "boundary"
    assert json.loads(pending[0]["args"])["kind"] == "talk"
    # pop_control returns only pending rows
    assert store.pop_control() == []
    store.resolve_control(control_id, "applied", {"ok": True})
    assert store.db.query_one("SELECT state FROM control WHERE id=?", (control_id,))["state"] == "applied"


def test_exports_and_manifest(store):
    store.add_segment("seg_1", start_ms=0, end_ms=1000, text="你好", turn_id="turn_001", revision=1)
    store.add_chunk(1, path="audio/c1.wav", converted_path=None, start_ms=0, end_ms=1000,
                    frames=1000, sample_rate=1000, channels=1)
    assert store.export_transcript_jsonl() == 1
    assert store.export_events_jsonl() == 0
    manifest = store.write_manifest()
    assert manifest["counts"]["segments"] == 1
    assert manifest["session_id"] == "sess_test"
    line = json.loads(store.paths.transcript_jsonl.read_text(encoding="utf-8").strip())
    assert line["text"] == "你好"
    assert line["is_final"] is True


def test_index_sessions(cfg, store):
    sessions = index_sessions(cfg.output_root)
    assert [s["session_id"] for s in sessions] == ["sess_test"]
