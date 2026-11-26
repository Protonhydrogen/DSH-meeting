"""DSH adapter contracts: bounded evidence and immutable, epoch-checked notes."""
import base64
import io
import json
from pathlib import Path

import pytest

from scientific_conference import dsh_plugin, notebook, review_layers
from scientific_conference.notes import validate_note
from scientific_conference.store import Store
from test_notebook import PNG


@pytest.fixture
def meeting(cfg):
    cfg.privacy.allow_cloud_notes = True
    store = Store.create(cfg.output_root, "dsh_fixture", cfg, consent=True, title="Test conference")
    store.add_segment("a", start_ms=0, end_ms=3000, text="Original ASR.")
    store.set_state("closed")
    yield store
    store.db.close()


def raw_rows(store):
    return [dict(r) for r in store.db.query("SELECT * FROM segments")]


def payload(epoch=0, run_id="run_1", markdown="# Scientific summary\n\nObserved result.", evidence=None):
    return dict(markdown=markdown, review_epoch=epoch, run_id=run_id, evidence=evidence)


def test_bootstrap_reuses_existing_dashboard_without_starting_recording(cfg, monkeypatch):
    ready = dict(origin="http://127.0.0.1:12345", token="private", pid=100, instance="fixture")
    monkeypatch.setattr(dsh_plugin, "ensure_dashboard", lambda actual: ready)
    assert dsh_plugin.bootstrap(cfg) == dict(ok=True, **ready)


def test_context_human_evidence_notebook_images_and_current_summaries(cfg, meeting):
    before = raw_rows(meeting)
    review_layers.save_batch(meeting, [dict(segment_id="a", field="transcript", value="Human-confirmed result.", revision=0), dict(segment_id="a", field="translation:zh", value="人工译文", revision=0)])
    notebook.save(meeting, "Personal observation [00:01]", 0)
    notebook.add_image(meeting, dict(content=base64.b64encode(PNG).decode(), name="Slide 1", time_ms=1000))
    meeting.save_note(mode="session", target_id=None, source_revision=1, content_json={"summary":"Fresh summary", "findings":[], "_review_epoch":1,"_source_signatures":dsh_plugin._request(cfg,meeting).source_signatures}, content_md="Fresh summary", agent="test", config_hash="x", idempotency_key="fresh")
    result = dsh_plugin.context(cfg, meeting.session_id)
    assert result["ok"] and result["review_epoch"] == 1
    assert all(text in result["prompt"] for text in ["Human-confirmed result.", "人工译文", "Personal observation", "Fresh summary"])
    assert "Original ASR." not in result["prompt"]
    assert result["images"][0]["mime"] == "image/png"
    assert base64.b64decode(result["images"][0]["data"]) == PNG
    assert raw_rows(meeting) == before


def test_context_excludes_stale_summaries(cfg, meeting):
    meeting.save_note(mode="session", target_id=None, source_revision=1, content_json={"summary":"STALE CLAIM", "_review_epoch":0}, content_md="STALE CLAIM", agent="test", config_hash="x", idempotency_key="old")
    review_layers.save_batch(meeting, [dict(segment_id="a", field="transcript", value="Corrected result.", revision=0)])
    assert "STALE CLAIM" not in dsh_plugin.context(cfg, meeting.session_id)["prompt"]


def test_context_respects_meeting_privacy(cfg):
    store = Store.create(cfg.output_root, "private_meeting", cfg, consent=True)
    store.add_segment("a", start_ms=0, end_ms=1000, text="Local only.")
    store.db.close()
    with pytest.raises(PermissionError):
        dsh_plugin.context(cfg, "private_meeting")


@pytest.mark.parametrize("session", ["../escape", "x/y", "", "x"*101])
def test_session_ids_cannot_escape_output_root(cfg, session):
    with pytest.raises(ValueError):
        dsh_plugin.context(cfg, session)
    with pytest.raises(ValueError):
        dsh_plugin.save_summary(cfg, session, payload())


def test_context_images_are_bounded_and_bad_ids_are_not_opened(cfg, meeting, monkeypatch):
    notebook.add_image(meeting, dict(content=base64.b64encode(PNG).decode(), name="Large slide", time_ms=0))
    with meeting.db.tx() as conn:
        conn.execute("INSERT INTO notebook_images VALUES (?,?,?,?,?)", ("../../secret.png", "bad", "image/png", 0, "0"))
    monkeypatch.setattr(dsh_plugin, "MAX_IMAGE_BYTES", len(PNG)-1)
    result = dsh_plugin.context(cfg, meeting.session_id)
    assert result["images"] == []
    assert len(result["warnings"]) == 2


def test_context_never_silently_truncates_transcript(cfg, meeting, monkeypatch):
    monkeypatch.setattr(dsh_plugin, "MAX_PROMPT_CHARS", 100)
    with pytest.raises(ValueError, match="limit"):
        dsh_plugin.context(cfg, meeting.session_id)


def test_discussion_context_contains_meeting_data_without_academic_instructions(cfg, meeting):
    regular = dsh_plugin.context(cfg, meeting.session_id, discussion=True)
    academic = dsh_plugin.context(cfg, meeting.session_id)
    data = json.loads(regular['prompt'])
    assert data['segments'][0]['text'] == 'Original ASR.'
    assert data['context']['existing_summaries'] == []
    assert regular['evidence'] == academic['evidence']
    assert '无科研内容' in academic['prompt']


def test_long_context_uses_compact_rows_without_losing_fields(cfg, meeting, monkeypatch):
    for index in range(30):
        meeting.add_segment(f'continuation-{index}', start_ms=4000 + index * 1000,
                            end_ms=4800 + index * 1000, text=f'Independent detail {index}.')
    full = dsh_plugin.context(cfg, meeting.session_id)
    monkeypatch.setattr(dsh_plugin, 'MAX_PROMPT_CHARS', len(full['prompt']) - 1)
    compact = dsh_plugin.context(cfg, meeting.session_id)
    def input_data(result):
        return json.loads(result['prompt'].split('===== 输入 JSON =====\n```json\n')[1].split('\n```')[0])
    data = input_data(compact)
    assert [dict(zip(data['segment_columns'], row)) for row in data['segments']] == input_data(full)['segments']
    assert data['context'] == input_data(full)['context']
    assert compact['evidence'] == full['evidence']


def test_save_preserves_raw_history_and_is_visible_in_existing_export(cfg, meeting):
    before = raw_rows(meeting)
    one = dsh_plugin.save_summary(cfg, meeting.session_id, payload(evidence=dsh_plugin.context(cfg,meeting.session_id)["evidence"], ))
    old_bytes = Path(one["path"]).read_bytes()
    two = dsh_plugin.save_summary(cfg, meeting.session_id, payload(evidence=dsh_plugin.context(cfg,meeting.session_id)["evidence"], run_id="run_2", markdown="# New summary\nUpdated observation."))
    assert one["created"] and two["created"] and one["path"] != two["path"]
    assert Path(one["path"]).read_bytes() == old_bytes
    assert len(meeting.notes(active_only=False)) == 2
    note = meeting.note(two["note_id"])
    data = json.loads(note["content_json"])
    assert validate_note(data) == []
    assert data["_review_epoch"] == 0 and data["agent"] == "dsh_native"
    assert "Updated observation." in notebook.combined_markdown(meeting)
    assert raw_rows(meeting) == before


def test_save_stale_epoch_is_rejected_without_new_artifact(cfg, meeting):
    review_layers.save_batch(meeting, [dict(segment_id="a", field="transcript", value="Changed.", revision=0)])
    with pytest.raises(review_layers.ReviewConflict):
        dsh_plugin.save_summary(cfg, meeting.session_id, payload(evidence=dsh_plugin.context(cfg,meeting.session_id)["evidence"], ))
    assert not meeting.notes(active_only=False)
    assert not list(meeting.paths.notes.glob("dsh_*.md"))


def test_save_epoch_is_rechecked_inside_store_transaction(cfg, meeting, monkeypatch):
    original = Store.save_note
    def racing_save(store, **kwargs):
        with store.db.tx() as conn:
            review_layers.bump(conn)
        return original(store, **kwargs)
    monkeypatch.setattr(Store, "save_note", racing_save)
    with pytest.raises(review_layers.ReviewConflict):
        dsh_plugin.save_summary(cfg, meeting.session_id, payload(evidence=dsh_plugin.context(cfg,meeting.session_id)["evidence"], ))
    assert not meeting.notes(active_only=False)
    assert not list(meeting.paths.notes.glob("dsh_*.md"))


def test_save_run_is_idempotent_and_cannot_change_existing_content(cfg, meeting):
    one = dsh_plugin.save_summary(cfg, meeting.session_id, payload(evidence=dsh_plugin.context(cfg,meeting.session_id)["evidence"], ))
    repeated = dsh_plugin.save_summary(cfg, meeting.session_id, payload(evidence=dsh_plugin.context(cfg,meeting.session_id)["evidence"], ))
    assert repeated["note_id"] == one["note_id"] and not repeated["created"]
    with pytest.raises(ValueError, match="run_id"):
        dsh_plugin.save_summary(cfg, meeting.session_id, payload(evidence=dsh_plugin.context(cfg,meeting.session_id)["evidence"], markdown="Different content"))
    assert len(meeting.notes(active_only=False)) == 1


def test_cli_uses_single_json_stdout_and_bounded_stdin(cfg, meeting, monkeypatch):
    monkeypatch.setattr(dsh_plugin, "load_config", lambda path: cfg)
    output = io.StringIO()
    status = dsh_plugin.main(["save", "--config", "fixture", "--session", meeting.session_id], stdin=io.StringIO(json.dumps(payload(evidence=dsh_plugin.context(cfg,meeting.session_id)["evidence"]))), stdout=output)
    result = json.loads(output.getvalue())
    assert status == 0 and result["ok"]
    output = io.StringIO()
    status = dsh_plugin.main(["save", "--config", "fixture", "--session", meeting.session_id], stdin=io.StringIO("x"*(dsh_plugin.MAX_STDIN_BYTES+1)), stdout=output)
    assert status == 1 and not json.loads(output.getvalue())["ok"]


def test_cli_subprocess_utf8_context_and_save(cfg, meeting):
    import os
    import subprocess
    import sys
    import yaml
    from scientific_conference.config import to_plain
    configuration = Path(cfg.output_root).parent / "dsh_adapter.yaml"
    configuration.write_text(yaml.safe_dump(to_plain(cfg), allow_unicode=True), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"), PYTHONDONTWRITEBYTECODE="1")
    command = [sys.executable, "-B", "-m", "scientific_conference.dsh_plugin"]
    prepared = subprocess.run(command + ["context", "--config", str(configuration), "--session", meeting.session_id], env=env, capture_output=True, encoding="utf-8", timeout=30)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    result = json.loads(prepared.stdout)
    assert result["session_id"] == meeting.session_id and "Original ASR." in result["prompt"]
    saved = subprocess.run(command + ["save", "--config", str(configuration), "--session", meeting.session_id], input=json.dumps(payload(evidence=result["evidence"],markdown="# Native summary\n\nEvidence retained.")), env=env, capture_output=True, encoding="utf-8", timeout=30)
    assert saved.returncode == 0, saved.stdout + saved.stderr
    assert json.loads(saved.stdout)["created"] is True


def test_evidence_snapshot_allows_append_without_claiming_new_content(cfg, meeting):
    meeting.set_state("recording")
    prepared=dsh_plugin.context(cfg,meeting.session_id)
    meeting.add_segment("b",start_ms=3000,end_ms=6000,text="Later finding.")
    result=dsh_plugin.save_summary(cfg,meeting.session_id,{**payload(),"evidence":prepared["evidence"]})
    content=json.loads(meeting.note(result["note_id"])["content_json"])
    assert content["_source_signatures"]==prepared["evidence"]["source_signatures"]
    assert "b" not in content["_source_signatures"]
    assert content["summary_through_ms"]==3000 and content["provisional"] is True


def test_evidence_missing_or_existing_source_changed_is_rejected(cfg,meeting):
    with pytest.raises(ValueError,match="evidence"):
        dsh_plugin.save_summary(cfg,meeting.session_id,payload())
    prepared=dsh_plugin.context(cfg,meeting.session_id)
    meeting.db.execute("UPDATE segments SET text='AI changed' WHERE segment_id='a'")
    with pytest.raises(review_layers.ReviewConflict):
        dsh_plugin.save_summary(cfg,meeting.session_id,{**payload(),"evidence":prepared["evidence"]})


def test_notebook_change_inside_save_transaction_rejects_old_evidence(cfg,meeting,monkeypatch):
    prepared=dsh_plugin.context(cfg,meeting.session_id)
    original=Store.save_note
    def race(store,**kwargs):
        notebook.save(store,"New user observation",0)
        return original(store,**kwargs)
    monkeypatch.setattr(Store,"save_note",race)
    with pytest.raises(review_layers.ReviewConflict):
        dsh_plugin.save_summary(cfg,meeting.session_id,{**payload(),"evidence":prepared["evidence"]})
    assert not meeting.notes(active_only=False)


def test_context_omits_same_epoch_summary_after_ai_correction(cfg,meeting):
    source=dsh_plugin.context(cfg,meeting.session_id)
    dsh_plugin.save_summary(cfg,meeting.session_id,{**payload(markdown="OLD_EVIDENCE_TEXT"),"evidence":source["evidence"]})
    meeting.db.execute("UPDATE segments SET text='Updated scientific statement.' WHERE segment_id='a'")
    result=dsh_plugin.context(cfg,meeting.session_id)
    assert "OLD_EVIDENCE_TEXT" not in result["prompt"]


def test_context_fingerprint_uses_exact_translation_snapshot_sent_to_agent(cfg,meeting,monkeypatch):
    language=cfg.translation.target_language
    meeting.save_translation("a",target_lang=language,text="OLD_TRANSLATION",backend="test")
    original=dsh_plugin._evidence
    def race(*args,**kwargs):
        meeting.save_translation("a",target_lang=language,text="NEW_TRANSLATION",backend="test")
        return original(*args,**kwargs)
    monkeypatch.setattr(dsh_plugin,"_evidence",race)
    prepared=dsh_plugin.context(cfg,meeting.session_id)
    assert "OLD_TRANSLATION" in prepared["prompt"]
    assert "NEW_TRANSLATION" not in prepared["prompt"]
    monkeypatch.setattr(dsh_plugin,"_evidence",original)
    with pytest.raises(review_layers.ReviewConflict):
        dsh_plugin.save_summary(cfg,meeting.session_id,{**payload(),"evidence":prepared["evidence"]})


def test_bootstrap_isolates_native_notes_mode_from_web_configuration(cfg, monkeypatch, tmp_path):
    from scientific_conference.config import load_config, to_plain
    from scientific_conference.service import _write_config_snapshot

    cfg.runner.agent = "openai_compatible"
    cfg.privacy.allow_cloud_notes = True
    before = to_plain(cfg)
    ready = dict(origin="http://127.0.0.1:12345", token="fixture", pid=100)
    snapshots = []

    def detached_dashboard(actual):
        path = tmp_path / "dashboard.snapshot.yaml"
        _write_config_snapshot(actual, path)
        snapshots.append(load_config(path, env=False))
        return ready

    monkeypatch.setattr(dsh_plugin, "ensure_dashboard", detached_dashboard)
    assert dsh_plugin.bootstrap(cfg) == dict(ok=True, **ready)
    assert snapshots[0].runner.agent == "external"
    assert snapshots[0].asr == cfg.asr
    assert snapshots[0].audio == cfg.audio
    assert snapshots[0].translation == cfg.translation
    assert snapshots[0].output_root == cfg.output_root
    assert to_plain(cfg) == before


def test_manual_save_rejects_second_window_with_same_summary_head(cfg, meeting):
    notebook.save(meeting, "Keep personal notes.", 0)
    before = raw_rows(meeting)
    prepared = dsh_plugin.context(cfg, meeting.session_id)
    first_data = {**payload(run_id="window_1", markdown="# First manual draft", evidence=prepared["evidence"]), "expected_note_id": None}
    first = dsh_plugin.save_summary(cfg, meeting.session_id, first_data)
    original_bytes = Path(first["path"]).read_bytes()
    assert dsh_plugin.save_summary(cfg, meeting.session_id, first_data)["note_id"] == first["note_id"]
    with pytest.raises(review_layers.ReviewConflict, match="summary"):
        dsh_plugin.save_summary(cfg, meeting.session_id, {**payload(run_id="window_2", markdown="# Unsaved second draft", evidence=prepared["evidence"]), "expected_note_id": None})
    assert [n["note_id"] for n in meeting.notes(mode="session")] == [first["note_id"]]
    assert Path(first["path"]).read_bytes() == original_bytes
    assert list(meeting.paths.notes.glob("dsh_*.md")) == [Path(first["path"])]
    assert raw_rows(meeting) == before
    assert notebook.read(meeting)["text"] == "Keep personal notes."


def test_manual_save_rechecks_head_after_competing_save_before_transaction(cfg, meeting, monkeypatch):
    prepared = dsh_plugin.context(cfg, meeting.session_id)
    initial = dsh_plugin.save_summary(cfg, meeting.session_id, payload(run_id="initial", evidence=prepared["evidence"]))
    old_bytes = Path(initial["path"]).read_bytes()
    original = Store.save_note
    competing = []

    def race(store, **kwargs):
        monkeypatch.setattr(Store, "save_note", original)
        competing.append(dsh_plugin.save_summary(cfg, meeting.session_id, payload(run_id="competing_agent", markdown="# Competing summary", evidence=prepared["evidence"])))
        validate_source = kwargs["validate_source"]

        def inside_transaction():
            assert store.db.conn.in_transaction
            return validate_source()

        kwargs["validate_source"] = inside_transaction
        return original(store, **kwargs)

    monkeypatch.setattr(Store, "save_note", race)
    with pytest.raises(review_layers.ReviewConflict, match="summary"):
        dsh_plugin.save_summary(cfg, meeting.session_id, {**payload(run_id="manual_draft", markdown="# Unsaved manual changes", evidence=prepared["evidence"]), "expected_note_id": initial["note_id"]})
    assert [n["note_id"] for n in meeting.notes(mode="session")] == [competing[0]["note_id"]]
    assert Path(initial["path"]).read_bytes() == old_bytes
    assert Path(competing[0]["path"]).read_text(encoding="utf-8") == "# Competing summary"
    assert len(list(meeting.paths.notes.glob("dsh_*.md"))) == 2


def test_context_returns_current_native_summary_head_for_manual_save(cfg, meeting):
    prepared = dsh_plugin.context(cfg, meeting.session_id)
    assert prepared["summary_note_id"] is None
    first = dsh_plugin.save_summary(cfg, meeting.session_id, payload(evidence=prepared["evidence"]))
    current = dsh_plugin.context(cfg, meeting.session_id)
    assert current["summary_note_id"] == first["note_id"]
    saved = dsh_plugin.save_summary(cfg, meeting.session_id, {**payload(run_id="manual", markdown="# Edited summary", evidence=current["evidence"]), "expected_note_id": current["summary_note_id"]})
    assert dsh_plugin.context(cfg, meeting.session_id)["summary_note_id"] == saved["note_id"]


@pytest.mark.parametrize("head", [0, False, [], {}])
def test_manual_save_requires_string_or_null_summary_head(cfg, meeting, head):
    prepared = dsh_plugin.context(cfg, meeting.session_id)
    with pytest.raises(ValueError, match="expected_note_id"):
        dsh_plugin.save_summary(cfg, meeting.session_id, {**payload(evidence=prepared["evidence"]), "expected_note_id": head})
    assert not meeting.notes(active_only=False)
    assert not list(meeting.paths.notes.glob("dsh_*.md"))
