import numpy as np
import pytest

from scientific_conference.audio import sources
from scientific_conference.config import Config
from scientific_conference.dsh_plugin import context, meeting_language, pending_summaries, save_summary
from scientific_conference.events import EventQueue, SESSION_CLOSED
from scientific_conference.store import Store


@pytest.mark.parametrize('language', ['zh', 'en'])
def test_meeting_language_is_frozen_in_the_session(language):
    assert meeting_language({'notes': {'language': language}}, 'en') == language
    assert meeting_language({}, language) == language


def test_system_source_does_not_open_a_microphone(monkeypatch):
    from scientific_conference.audio import wasapi

    expected = object()
    monkeypatch.setattr(wasapi, 'SystemAudioSource', lambda: expected)
    assert sources.open_source(Config(), source='system') is expected


def test_wasapi_float_and_silence_blocks():
    from scientific_conference.audio.wasapi import pcm_block

    raw = np.array([-1, 0.5, 1, 0], dtype='<f4').tobytes()
    assert pcm_block(raw, 2, 2, 'float32').tolist() == [[-32768, 16384], [32767, 0]]
    assert pcm_block(None, 2, 2, 'float32').tolist() == [[0, 0], [0, 0]]


@pytest.mark.parametrize('language', ['zh', 'en'])
def test_auto_summary_waits_for_final_transcript_and_stops_after_save(cfg, language):
    cfg.notes.language = language
    cfg.privacy.allow_cloud_notes = True
    sid = 'fixture'
    store = Store.create(cfg.output_root, sid, cfg, consent=True, state='recording')
    try:
        store.db.execute("INSERT INTO meta(key,value) VALUES ('dsh_auto_summary','1')")
        assert pending_summaries(cfg)['sessions'] == []
        EventQueue(store.db).append(sid, SESSION_CLOSED, {}, dedupe_key='session.closed')
        store.set_state('closed')
        assert pending_summaries(cfg)['sessions'] == []
        store.add_segment('a', start_ms=0, end_ms=2300, text='会议发言。' if language == 'zh' else 'A meeting statement.')
        assert pending_summaries(cfg)['sessions'] == [sid]
        cfg.notes.language = 'en' if language == 'zh' else 'zh'
        prepared = context(cfg, sid)
        assert prepared['language'] == language and not prepared['evidence']['provisional']
        assert save_summary(cfg, sid, dict(markdown='# 纪要' if language == 'zh' else '# Minutes',
            review_epoch=prepared['review_epoch'], run_id='auto-fixture', evidence=prepared['evidence'],
            expected_note_id=None))['created']
        assert pending_summaries(cfg)['sessions'] == []
    finally:
        store.db.close()


def test_auto_summary_requires_permission_from_saved_meeting(cfg):
    cfg.privacy.allow_cloud_notes = False
    sid = 'no_permission'
    store = Store.create(cfg.output_root, sid, cfg, consent=True, state='closed')
    try:
        store.db.execute("INSERT INTO meta(key,value) VALUES ('dsh_auto_summary','1')")
        EventQueue(store.db).append(sid, SESSION_CLOSED, {}, dedupe_key='session.closed')
        store.add_segment('a', start_ms=0, end_ms=2300, text='Local-only meeting.')
        cfg.privacy.allow_cloud_notes = True
        assert pending_summaries(cfg)['sessions'] == []
    finally:
        store.db.close()
