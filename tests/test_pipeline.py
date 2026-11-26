"""End-to-end pipeline over a synthetic WAV file (no microphone, no model)."""

from __future__ import annotations

import json

import pytest

from scientific_conference.agents import OfflineExtractiveAgent
from scientific_conference.asr import MockAsrBackend
from scientific_conference.audio import read_wav, wav_info
from scientific_conference.recorder import Recorder, RecorderConfig
from scientific_conference.runner import Runner
from scientific_conference.store import STATE_CLOSED, STATE_FAILED, Store, session_paths

SCRIPT = [
    {"start_ms": 500, "end_ms": 2500, "text": "我们纳入了 482 名受试者。"},
    {"start_ms": 2900, "end_ms": 5200, "text": "主要终点是 C 反应蛋白，p 值小于 0.001。"},
    {"start_ms": 10000, "end_ms": 12500, "text": "接下来看亚组分析的结果。"},
    {"start_ms": 12900, "end_ms": 15000, "text": "效应方向在两个亚组中一致。"},
    {"start_ms": 20000, "end_ms": 22000, "text": "谢谢大家，欢迎提问。"},
]


@pytest.fixture()
def recorded(cfg, meeting_wav):
    path, plan = meeting_wav()
    session_id = "sess_pipe"
    store = Store.create(cfg.output_root, session_id, cfg, device_id="file",
                         device_name="test wav", sample_rate=16000, channels=1, consent=True, title="组会")
    rec = Recorder(
        cfg,
        store,
        RecorderConfig(session_id=session_id, output_root=cfg.output_root, source="file",
                       wav_path=str(path), realtime=False, tick_ms=50),
        backend_factory=lambda: MockAsrBackend(script=SCRIPT),
    )
    exit_code = rec.run()
    return store, session_id, plan, exit_code


def test_session_closes_cleanly(recorded):
    store, session_id, plan, exit_code = recorded
    assert exit_code == 0
    assert store.session_state() == STATE_CLOSED
    assert store.session()["ended_utc"] is not None
    assert store.session()["last_error"] is None


def test_audio_is_recorded_in_readable_chunks(recorded):
    store, *_ = recorded
    chunks = store.chunks()
    assert chunks, "no audio chunks were written"
    total_ms = 0
    for chunk in chunks:
        path = store.paths.root / chunk["path"]
        assert path.exists(), chunk["path"]
        info = wav_info(path)
        assert info.frames == chunk["frames"]
        total_ms += info.duration_ms
    stats = store.audio_stats()
    assert abs(total_ms - stats["duration_ms"]) <= 50
    assert stats["gap_ms"] == 0


def test_transcript_covers_every_utterance(recorded):
    store, *_ = recorded
    texts = [s["text"] for s in store.segments()]
    assert len(texts) == len(SCRIPT)
    assert texts == [item["text"] for item in SCRIPT]


def test_turns_and_talk_are_derived(recorded):
    store, *_ = recorded
    turns = store.turns()
    assert len(turns) == 3
    assert all(t["state"] == "finalized" for t in turns)
    talks = store.talks()
    assert len(talks) == 1
    assert talks[0]["state"] == "closed"
    # every segment belongs to a turn inside the talk
    for segment in store.segments():
        assert segment["turn_id"] in {t["turn_id"] for t in turns}
        assert segment["talk_id"] == talks[0]["talk_id"]


def test_events_are_emitted_in_order(recorded):
    store, session_id, *_ = recorded
    from scientific_conference.events import EventQueue

    queue = EventQueue(store.db)
    seqs = [row["seq"] for row in store.db.query(
        "SELECT seq FROM events WHERE session_id=? ORDER BY seq", (session_id,))]
    assert seqs == list(range(1, len(seqs) + 1))
    types = queue.count_by_type(session_id)
    assert types["turn.finalized"] == 3
    assert types["talk.closed"] == 1
    assert types["session.closed"] == 1
    assert types["session.started"] == 1
    # turn.finalized payloads follow the documented contract
    payload = [row for row in store.db.query(
        "SELECT payload FROM events WHERE session_id=? AND type='turn.finalized' ORDER BY seq", (session_id,))][0]
    data = json.loads(payload["payload"])
    for key in ("schema_version", "session_id", "event_id", "seq", "turn_id", "talk_id",
                "speaker_id", "revision", "start_ms", "end_ms", "boundary_reason", "segments"):
        assert key in data, key
    assert data["segments"][0]["is_final"] is True


def test_runner_produces_notes_for_turns_talk_and_session(recorded, cfg):
    store, session_id, *_ = recorded
    runner = Runner(cfg, store, consumer_id="test", agent=OfflineExtractiveAgent())
    runner.run_forever(follow=True, max_idle_cycles=3)
    notes = store.notes(active_only=True)
    modes = sorted({n["mode"] for n in notes})
    assert modes == ["session", "talk", "turn"]
    assert len([n for n in notes if n["mode"] == "turn"]) == 3
    for note in notes:
        path = store.paths.root / note["path"]
        assert path.exists()
        assert "## 核心摘要" in path.read_text(encoding="utf-8")
    # the event cursor advanced to the end
    from scientific_conference.events import EventQueue

    queue = EventQueue(store.db)
    assert queue.cursor(session_id, "test") == queue.max_seq(session_id)


def test_note_is_regenerated_on_a_new_revision(recorded, cfg):
    store, session_id, *_ = recorded
    runner = Runner(cfg, store, consumer_id="test", agent=OfflineExtractiveAgent())
    runner.run_forever(follow=True, max_idle_cycles=3)
    turn = store.turns()[0]
    first = store.active_note("turn", turn["turn_id"])
    assert first is not None

    from scientific_conference.events import TRANSCRIPT_REVISED, EventQueue

    revision = store.bump_turn_revision(turn["turn_id"])
    EventQueue(store.db).append(
        session_id,
        TRANSCRIPT_REVISED,
        {"turn_id": turn["turn_id"], "talk_id": turn["talk_id"], "revision": revision,
         "revision_reason": "speaker_mapping"},
        dedupe_key=f"transcript.revised:{turn['turn_id']}:r{revision}",
    )
    runner.run_cycle()
    latest = store.active_note("turn", turn["turn_id"])
    assert latest["note_id"] != first["note_id"]
    assert latest["source_revision"] == revision
    assert store.note(first["note_id"])["state"] == "superseded"
    # exactly one active note per target
    active = [n for n in store.notes(active_only=True) if n["target_id"] == turn["turn_id"]]
    assert len(active) == 1


def test_exports_are_written(recorded):
    store, session_id, *_ = recorded
    assert store.export_transcript_jsonl() == len(SCRIPT)
    assert store.export_events_jsonl() >= 5
    lines = store.paths.transcript_jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert all(json.loads(line)["segment_id"] for line in lines)
    manifest = store.write_manifest()
    assert manifest["counts"]["segments"] == len(SCRIPT)
    assert manifest["counts"]["talks"] == 1
    assert manifest["consent_recorded"] is True


def test_session_can_be_reopened(cfg, recorded):
    store, session_id, *_ = recorded
    reopened = Store.open(cfg.output_root, session_id)
    assert reopened.session_state() == STATE_CLOSED
    assert len(reopened.segments()) == len(SCRIPT)
    assert reopened.paths.root == session_paths(cfg.output_root, session_id).root


def test_stop_request_drains_and_closes(cfg, meeting_wav):
    """``stop`` must finalise the open turn even in the middle of the file."""
    path, _ = meeting_wav()
    session_id = "sess_stop"
    store = Store.create(cfg.output_root, session_id, cfg, device_id="file", consent=True)
    rec = Recorder(
        cfg,
        store,
        RecorderConfig(session_id=session_id, output_root=cfg.output_root, source="file",
                       wav_path=str(path), realtime=True, tick_ms=20),
        backend_factory=lambda: MockAsrBackend(script=SCRIPT),
    )

    import threading
    import time

    def stopper():
        time.sleep(1.0)
        rec.request_stop("test")

    thread = threading.Thread(target=stopper, daemon=True)
    thread.start()
    exit_code = rec.run()
    thread.join(timeout=5)
    assert exit_code == 0
    assert store.session_state() == STATE_CLOSED
    from scientific_conference.events import EventQueue

    queue = EventQueue(store.db)
    assert queue.count_by_type(session_id).get("session.closed") == 1
    # audio recorded so far is preserved and readable
    stats = store.audio_stats()
    assert stats["chunks"] >= 1
    assert 500 < stats["duration_ms"] < 20000


def test_failure_is_recorded_not_hidden(cfg, meeting_wav, tmp_path):
    path, _ = meeting_wav()
    session_id = "sess_fail"
    store = Store.create(cfg.output_root, session_id, cfg, device_id="file", consent=True)

    class BrokenBackend:
        name = "broken"

        def transcribe(self, *args, **kwargs):
            raise RuntimeError("boom")

        def info(self):
            return {"backend": "broken"}

        def close(self):
            return None

    rec = Recorder(
        cfg,
        store,
        RecorderConfig(session_id=session_id, output_root=cfg.output_root, source="file",
                       wav_path=str(path), realtime=False, tick_ms=50),
        backend_factory=lambda: BrokenBackend(),
    )
    rec.run()
    # The recorder keeps the audio even when every recogniser call fails, and
    # the failure is written to the session log instead of disappearing.
    assert store.audio_stats()["duration_ms"] > 1000
    logged = " ".join(str(row["message"] or "") + str(row["data"] or "") for row in store.logs(limit=100))
    assert "boom" in logged
    assert store.session_state() in (STATE_CLOSED, STATE_FAILED)

def test_microphone_open_failure_is_immediate_and_never_records(cfg, monkeypatch):
    from scientific_conference.audio.sources import AudioSource
    from scientific_conference.events import EventQueue
    class DeniedInput(AudioSource):
        sample_rate = 48000
        stopped = False
        def prepare(self):
            raise RuntimeError('Invalid device: restricted audio access')
        def stop(self):
            self.stopped = True
    source = DeniedInput()
    monkeypatch.setattr('scientific_conference.recorder.open_source', lambda *a, **k: source)
    store = Store.create(cfg.output_root,'denied_audio',cfg,consent=True)
    def model_factory():
        pytest.fail('ASR must not load after input open failed')
    recorder = Recorder(cfg,store,RecorderConfig(session_id=store.session_id,output_root=cfg.output_root),backend_factory=model_factory)
    assert recorder.run() == 1
    assert source.stopped
    assert store.session_state() == 'failed'
    status = json.loads((store.paths.root/'status.json').read_text(encoding='utf-8'))
    assert status['state'] == 'failed'
    assert 'Invalid device' in status['last_error']
    events = EventQueue(store.db).poll(store.session_id,'test',after_seq=0)['events']
    assert not any(e['type']=='session.started' for e in events)


def test_recording_state_follows_successful_source_start(cfg, monkeypatch, meeting_wav):
    from scientific_conference.audio.sources import WavFileSource
    wav,_ = meeting_wav()
    store = Store.create(cfg.output_root,'start_order',cfg,consent=True)
    source = WavFileSource(wav)
    original = source.start
    def start():
        assert store.session_state() == 'starting'
        original()
    monkeypatch.setattr(source,'start',start)
    monkeypatch.setattr('scientific_conference.recorder.open_source',lambda *a,**k:source)
    recorder=Recorder(cfg,store,RecorderConfig(session_id=store.session_id,output_root=cfg.output_root),backend_factory=lambda:MockAsrBackend(script=SCRIPT))
    assert recorder.run()==0
