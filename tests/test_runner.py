"""The scheduler: event -> task -> note, with de-duplication, retries and acks."""

from __future__ import annotations

import json

import pytest

from scientific_conference.agents import AgentAdapter, NoteRequest, OfflineExtractiveAgent
from scientific_conference.events import EventQueue
from scientific_conference.runner import Runner
from scientific_conference.store import MODE_TALK, MODE_TURN, Store


class FailingAgent(AgentAdapter):
    name = "failing"

    def __init__(self, fail_times: int = 99):
        self.fail_times = fail_times
        self.calls = 0

    def generate(self, request: NoteRequest):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("model unavailable")
        return {"summary": "恢复后的纪要", "findings": []}, "{}"


class BadJsonAgent(AgentAdapter):
    name = "bad"

    def generate(self, request: NoteRequest):
        return {"findings": []}, "{}"  # missing required 'summary'


def test_provisional_zero_end_events_are_distinct(session, cfg):
    seed_turn(session)
    runner = Runner(cfg, session, agent=OfflineExtractiveAgent())
    events = EventQueue(session.db)
    for n in (1, 2):
        events.append(session.session_id, 'turn.provisional',
            {'turn_id': 'turn_001', 'revision': 1, 'end_ms': 0}, dedupe_key=f'tick{n}')
    runner.poll_and_enqueue()
    rows = session.db.query("SELECT idempotency_key FROM tasks WHERE payload LIKE '%\"provisional\": true%'")
    assert len(rows) == 2
    assert rows[0]['idempotency_key'] != rows[1]['idempotency_key']


def test_recorder_provisional_uses_live_boundary(session, cfg):
    from scientific_conference.recorder import Recorder, RecorderConfig
    from scientific_conference.segmenter import Action
    session.open_talk('talk_001', 0)
    session.open_turn('turn_001', 0, talk_id='talk_001')
    recorder = Recorder(cfg, session, RecorderConfig(session.session_id, cfg.output_root))
    for end in (30000, 60000):
        recorder._emit_provisional(Action('provisional', turn_id='turn_001', end_ms=end))
    payloads = [json.loads(r['payload']) for r in session.db.query("SELECT payload FROM events WHERE type='turn.provisional' ORDER BY seq")]
    assert [p['end_ms'] for p in payloads] == [30000, 60000]


def test_correction_runs_without_summary_event(session, cfg):
    cfg.runner.agent = 'openai_compatible'
    cfg.privacy.allow_cloud_notes = True
    session.add_segment('independent', start_ms=0, end_ms=1000, text='We used CASE evolution.')
    class Proofreader:
        model = 'fake'
        timeout_s = 60
        temperature = 0
        calls = []
        def complete_json(self, *args):
            self.calls.append(args)
            return {'edits': []}, ''
    agent = Proofreader()
    runner = Runner(cfg, session, agent=agent)
    runner.run_cycle()
    assert len(agent.calls) == 2
    assert session.db.query_one("SELECT state FROM tasks WHERE type='transcript.review'")['state'] == 'done'
    runner.run_cycle()
    assert len(agent.calls) == 2


@pytest.fixture()
def session(cfg):
    store = Store.create(cfg.output_root, "sess_runner", cfg, device_id="file", consent=True)
    store.set_state("recording")
    return store


def seed_turn(store: Store, turn_id: str = "turn_001", *, text: str = "样本量为 482 名受试者，p 值小于 0.001。"):
    store.open_talk("talk_001", 0, "speaker_01")
    store.open_turn(turn_id, 0, talk_id="talk_001", speaker_id="speaker_01")
    store.add_segment(f"seg_{turn_id}", start_ms=0, end_ms=3000, text=text, turn_id=turn_id,
                      talk_id="talk_001", speaker_id="speaker_01")
    store.update_turn(turn_id, end_ms=3000, state="finalized", boundary_reason="silence")
    queue = EventQueue(store.db)
    event, _ = queue.append(
        store.session_id, "turn.finalized",
        {"turn_id": turn_id, "talk_id": "talk_001", "speaker_id": "speaker_01",
         "revision": 1, "start_ms": 0, "end_ms": 3000, "boundary_reason": "silence"},
        dedupe_key=f"turn.finalized:{turn_id}:r1",
    )
    return event


def test_event_creates_task_and_note(session, cfg):
    seed_turn(session)
    runner = Runner(cfg, session, consumer_id="test", agent=OfflineExtractiveAgent())
    summary = runner.run_cycle()
    assert summary["enqueued"] == 1
    assert summary["processed"] == 1
    notes = session.notes(active_only=True)
    assert len(notes) == 1
    assert notes[0]["mode"] == MODE_TURN
    assert notes[0]["target_id"] == "turn_001"
    from pathlib import Path

    md_path = Path(notes[0]["path"])
    assert md_path.exists()
    assert "## 核心摘要" in md_path.read_text(encoding="utf-8")
    assert session.task_counts()["done"] == 1
    assert runner.events.cursor(session.session_id, "test") == 1


def test_repeated_cycles_are_idempotent(session, cfg):
    seed_turn(session)
    runner = Runner(cfg, session, consumer_id="test", agent=OfflineExtractiveAgent())
    runner.run_cycle()
    before = len(session.notes(active_only=False))
    runner.run_cycle()
    runner.run_cycle()
    assert len(session.notes(active_only=False)) == before
    # one note task, plus the no-op tasks for the bookkeeping events
    assert session.task_counts()["done"] >= 1
    note_tasks = [t for t in session.tasks() if t["type"].startswith("note.")]
    assert len(note_tasks) == 1


def test_worker_crash_redelivers_but_does_not_duplicate(session, cfg):
    """Simulate a worker that leased a task and died before acking."""
    seed_turn(session)
    first = Runner(cfg, session, consumer_id="test", agent=OfflineExtractiveAgent())
    first.poll_and_enqueue()
    task = first.process_tasks()[0]
    assert task.state == "done"
    # The event was never acked: a new consumer must not produce a second note.
    second = Runner(cfg, session, consumer_id="test", agent=OfflineExtractiveAgent())
    second.run_cycle()
    assert len(session.notes(active_only=True)) == 1


def test_ack_stops_at_unfinished_task(session, cfg):
    seed_turn(session, "turn_001")
    seed_turn(session, "turn_002")
    runner = Runner(cfg, session, consumer_id="test", agent=OfflineExtractiveAgent())
    created = runner.poll_and_enqueue()
    assert len(created) == 2
    # Complete only the first task; the cursor must not step over the second.
    runner.process_tasks(limit=1)
    cursor = runner.ack_contiguous()
    assert cursor == 1
    assert runner.events.cursor(session.session_id, "test") == 1
    runner.process_tasks()
    assert runner.ack_contiguous() == 2


def test_unpolled_event_blocks_the_cursor(session, cfg):
    seed_turn(session, "turn_001")
    seed_turn(session, "turn_002")
    runner = Runner(cfg, session, consumer_id="test", agent=OfflineExtractiveAgent())
    runner.poll_and_enqueue(limit=1)
    runner.process_tasks()
    # Event 2 has not been polled yet: the cursor must stay at 1 rather than
    # skipping work that was never delivered.
    assert runner.ack_contiguous() == 1


def test_failing_agent_retries_then_dead_letters(session, cfg):
    seed_turn(session)
    cfg.runner.max_attempts = 2
    cfg.runner.backoff_base_s = 0.0
    agent = FailingAgent(fail_times=99)
    runner = Runner(cfg, session, consumer_id="test", agent=agent)
    runner.poll_and_enqueue()
    result = runner.process_tasks()[0]
    assert result.state == "failed"
    task = session.tasks()[0]
    assert task["attempts"] == 1
    assert "model unavailable" in task["last_error"]
    # second attempt reaches max_attempts -> dead letter, never silently lost
    session.db.execute("UPDATE tasks SET available_at_utc='2000-01-01T00:00:00.000Z'")
    result = runner.process_tasks()[0]
    assert result.state == "dead"
    assert session.task_counts()["dead"] == 1
    assert session.notes(active_only=True) == []


def test_agent_recovers_after_transient_failure(session, cfg):
    seed_turn(session)
    cfg.runner.max_attempts = 3
    cfg.runner.backoff_base_s = 0.0
    runner = Runner(cfg, session, consumer_id="test", agent=FailingAgent(fail_times=1))
    runner.poll_and_enqueue()
    assert runner.process_tasks()[0].state == "failed"
    session.db.execute("UPDATE tasks SET available_at_utc='2000-01-01T00:00:00.000Z'")
    assert runner.process_tasks()[0].state == "done"
    assert len(session.notes(active_only=True)) == 1


def test_invalid_note_is_rejected_not_stored(session, cfg):
    seed_turn(session)
    runner = Runner(cfg, session, consumer_id="test", agent=BadJsonAgent())
    runner.poll_and_enqueue()
    result = runner.process_tasks()[0]
    assert result.state in ("failed", "dead")
    assert session.notes(active_only=False) == []


def test_noop_events_are_acked_without_notes(session, cfg):
    queue = EventQueue(session.db)
    queue.append(session.session_id, "session.started", {}, dedupe_key="session.started")
    queue.append(session.session_id, "audio.gap", {"gap_ms": 100}, dedupe_key="audio.gap:100")
    runner = Runner(cfg, session, consumer_id="test", agent=OfflineExtractiveAgent())
    summary = runner.run_cycle()
    assert summary["processed"] == 2
    assert session.notes(active_only=False) == []
    assert runner.events.cursor(session.session_id, "test") == 2


def test_talk_note_merges_turn_notes(session, cfg):
    event = seed_turn(session, "turn_001")
    seed_turn(session, "turn_002")
    queue = EventQueue(session.db)
    queue.append(
        session.session_id, "talk.closed",
        {"talk_id": "talk_001", "speaker_id": "speaker_01", "revision": 1,
         "start_ms": 0, "end_ms": 6000, "boundary_reason": "silence"},
        dedupe_key="talk.closed:talk_001:r1",
    )
    session.close_talk("talk_001", 6000, speaker_id="speaker_01")
    runner = Runner(cfg, session, consumer_id="test", agent=OfflineExtractiveAgent())
    runner.run_cycle()
    talk_notes = session.notes(mode=MODE_TALK)
    assert len(talk_notes) == 1
    content = json.loads(talk_notes[0]["content_json"])
    assert content["mode"] == MODE_TALK
    assert "turn_notes" in json.dumps(content, ensure_ascii=False) or content["findings"] is not None
    assert talk_notes[0]["source_revision"] == 1
    assert event["seq"] >= 1


def test_session_note_covers_everything(session, cfg):
    seed_turn(session, "turn_001")
    queue = EventQueue(session.db)
    queue.append(session.session_id, "session.closed", {"reason": "manual"}, dedupe_key="session.closed")
    session.set_state("closed")
    runner = Runner(cfg, session, consumer_id="test", agent=OfflineExtractiveAgent())
    runner.run_cycle()
    session_notes = session.notes(mode="session")
    assert len(session_notes) == 1
    assert session_notes[0]["source_revision"] == 1


def test_offline_agent_marks_uncertainty(session, cfg):
    seed_turn(session, "turn_001", text="本次研究纳入了 482 名受试者，结果显示 C 反应蛋白升高 12%。")
    runner = Runner(cfg, session, consumer_id="test", agent=OfflineExtractiveAgent())
    runner.run_cycle()
    content = json.loads(session.notes(active_only=True)[0]["content_json"])
    assert content["findings"], "expected at least one extracted finding"
    finding = content["findings"][0]
    assert finding["source_segment_ids"] == ["seg_turn_001"]
    assert finding["uncertainty"]
    assert content["summary"]


def test_follow_streams_transcript_events_and_notes(session, cfg, capsys):
    """``conference follow`` is the live view: it must show segments, boundary
    events and the paths of notes as they are saved - in JSON-lines form so it
    can also be piped."""
    from scientific_conference import cli

    seed_turn(session)
    runner = Runner(cfg, session, consumer_id="test", agent=OfflineExtractiveAgent())
    runner.run_cycle()
    session.set_state("closed")

    code = cli.main(
        [
            "--output-root", str(cfg.output_root),
            "--json", "follow",
            "--session-id", session.session_id,
            "--interval", "0.1", "--max-cycles", "2",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert lines, f"no streamed output: {out!r}"

    segments = [item for item in lines if item["kind"] == "segment"]
    assert segments and segments[0]["text"]
    assert segments[0]["speaker_id"] == "speaker_01"

    events = [item for item in lines if item["kind"] == "event"]
    types = {item["type"] for item in events}
    assert "turn.finalized" in types
    assert "note.saved" in types
    # the bulky segment list is not repeated in the live event stream
    assert all("segments" not in item for item in events)


def test_follow_does_not_touch_the_cursor(session, cfg, capsys):
    """Following is read-only: it must not acknowledge events for other consumers."""
    from scientific_conference import cli

    seed_turn(session)
    session.set_state("closed")
    before = EventQueue(session.db).cursor(session.session_id, "local-runner")
    cli.main(
        [
            "--output-root", str(cfg.output_root), "--json", "follow",
            "--session-id", session.session_id, "--interval", "0.1", "--max-cycles", "2",
        ]
    )
    capsys.readouterr()
    after = EventQueue(session.db).cursor(session.session_id, "local-runner")
    assert after == before
