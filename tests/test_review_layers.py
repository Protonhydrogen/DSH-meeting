import json
from pathlib import Path
import pytest
from scientific_conference.store import Store
from scientific_conference import review_layers as review
from test_dashboard import dashboard
from scientific_conference.reading import reading_blocks

@pytest.fixture
def store(cfg):
    s=Store.create(cfg.output_root,'review_test',cfg,consent=True)
    s.add_segment('a',start_ms=0,end_ms=3000,text='We used CASE evolution.',speaker_id='speaker_unknown')
    s.add_segment('b',start_ms=3000,end_ms=6000,text='Next experiment.',speaker_id='speaker_unknown')
    s.set_state('closed')
    yield s
    s.db.close()

def edit(store,value,revision=0,field='transcript',segment_id='a'):
    return review.save_batch(store,[dict(segment_id=segment_id,field=field,value=value,revision=revision)])

def test_manual_beats_ai_and_survives_reopen(store,cfg):
    with store.db.tx() as c:c.execute('INSERT INTO transcript_edits VALUES (?,?,?,?,?,?)',(store.session_id,'a','We used CASE evolution.','We used PACE evolution.','terms','ai'))
    edit(store,'Human confirmed experiment.')
    assert store.segment('a')['text']=='We used CASE evolution.'
    assert store.segments()[0]['text']=='Human confirmed experiment.'
    with store.db.tx() as c:c.execute('UPDATE transcript_edits SET corrected=?',('Later AI output.',))
    reopened=Store.open(cfg.output_root,store.session_id)
    assert reopened.segments()[0]['text']=='Human confirmed experiment.'
    detail=review.details(reopened,['a'])['segments'][0]
    assert detail['asr_text']=='We used CASE evolution.' and detail['ai_text']=='Later AI output.'
    assert detail['human_confirmed']
    reopened.db.close()

def test_cancel_is_client_only_and_conflicts_are_atomic(store):
    edit(store,'First.')
    with pytest.raises(review.ReviewConflict):review.save_batch(store,[dict(segment_id='b',field='transcript',value='B edit',revision=0),dict(segment_id='a',field='transcript',value='stale',revision=0)])
    assert store.segments()[1]['text']=='Next experiment.'
    assert store.segments()[0]['text']=='First.'

def test_undo_multiple_steps_restores_latest_ai_without_deleting_history(store):
    edit(store,'One.');edit(store,'Two.',1)
    review.undo(store,'a','transcript',2)
    assert store.segments()[0]['text']=='One.'
    review.undo(store,'a','transcript',3)
    assert store.segments()[0]['text']=='We used CASE evolution.'
    assert len(review.details(store,['a'])['segments'][0]['history'])==4

def test_translation_is_independent_and_auto_retranslation_cannot_override(store):
    store.save_translation('a',target_lang='zh',text='自动译文',backend='mock')
    edit(store,'人工译文',field='translation:zh')
    store.save_translation('a',target_lang='zh',text='后来自动译文',backend='mock')
    assert store.translation('a','zh')['text']=='人工译文'
    assert store.segments()[0]['text']=='We used CASE evolution.'
    review.undo(store,'a','translation:zh',1)
    assert store.translation_map('zh')['a']['text']=='后来自动译文'

def test_anomalous_asr_can_be_confirmed_by_human(store):
    from scientific_conference.quality import suspect_segment
    with store.db.tx() as c:c.execute('UPDATE segments SET flags=? WHERE segment_id=?',(json.dumps(['repetition_suspected']),'a'))
    assert suspect_segment(store.segments()[0])
    edit(store,'This is the corrected observation.')
    assert not suspect_segment(store.segments()[0])



def test_summary_uses_confirmed_text_translation_and_epoch(store,cfg):
    from scientific_conference.runner import Runner
    from scientific_conference.agents import OfflineExtractiveAgent
    edit(store,'Human science.');edit(store,'人工科研',field='translation:zh')
    runner=Runner(cfg,store,agent=OfflineExtractiveAgent())
    req=runner._build_request(dict(mode='session',target_id=None,source_revision=1,payload='{}'))
    assert req.transcript[0]['text']=='Human science.'
    assert req.as_payload()['segments'][0]['confirmed_translations']['zh']=='人工科研'
    assert req.context['review_epoch']==2


def test_manual_translation_without_auto_backend(store):
    edit(store,'Manual translation',field='translation:zh')
    assert store.translation('a','zh')['text']=='Manual translation'

def test_stale_summary_result_rejected_atomically_after_manual_change(store):
    before=review.epoch(store)
    edit(store,'Human correction.')
    note,created,error=store.save_note(mode='session',target_id=None,source_revision=1,content_json={'summary':'Old ASR answer','_review_epoch':before},content_md='Old ASR answer',agent='test',config_hash='x',idempotency_key='stale-review')
    assert not created and error and not store.notes()

def test_http_review_conflict_and_original_history(cfg,dashboard):
    from scientific_conference.dashboard import _request
    from urllib.error import HTTPError
    s=Store.create(cfg.output_root,'http_review',cfg,consent=True);s.add_segment('a',start_ms=0,end_ms=1000,text='Original.')
    _,info=dashboard
    data=_request(info,'/api/review/read',dict(session_id='http_review',segment_ids=['a']))
    assert data['segments'][0]['asr_text']=='Original.'
    payload=dict(session_id='http_review',edits=[dict(segment_id='a',field='transcript',value='Human.',revision=0)])
    _request(info,'/api/review/save',payload)
    with pytest.raises(HTTPError) as err:_request(info,'/api/review/save',payload)
    assert err.value.code==409
    assert _request(info,'/api/snapshot?session_id=http_review')['segments'][0]['text']=='Human.'
    s.db.close()






def test_export_does_not_present_pre_edit_summary_as_current(store):
    from scientific_conference.notebook import combined_markdown
    store.save_note(mode='session',target_id=None,source_revision=1,content_json={'summary':'OLD CLAIM','_review_epoch':0},content_md='OLD CLAIM',agent='test',config_hash='x',idempotency_key='old')
    edit(store,'Human corrected words.')
    assert 'OLD CLAIM' not in combined_markdown(store)
    assert store.notes()[0]['content_md']=='OLD CLAIM'


def test_manual_source_change_requeues_auto_translation_and_manual_translation_does_not_starve_queue(store):
    store.save_translation('a',target_lang='zh',text='Old automatic',backend='mock')
    edit(store,'Updated human source.')
    assert [s['segment_id'] for s in store.untranslated_segments('zh')]==['a','b']
    store.save_translation('a',target_lang='zh',text='Updated automatic',backend='mock',source_review_revision=1)
    assert [s['segment_id'] for s in store.untranslated_segments('zh')]==['b']
    edit(store,'Manual only',field='translation:fr')
    assert [s['segment_id'] for s in store.untranslated_segments('fr',limit=1)]==['b']


def test_superseded_save_and_undo_do_not_commit_invisible_edits(store):
    edit(store,'Before superseding.')
    store.supersede_segments(['a'],'b')
    before=review.epoch(store)
    with pytest.raises(ValueError):edit(store,'Invisible',1)
    with pytest.raises(ValueError):review.undo(store,'a','transcript',1)
    assert review.head(store,'a','transcript')['value']=='Before superseding.'
    assert review.epoch(store)==before




def test_confirmed_translation_survives_summary_anomaly_filter_without_raw_loop(store,cfg):
    from scientific_conference.runner import Runner
    from scientific_conference.agents import OfflineExtractiveAgent
    with store.db.tx() as c:c.execute('UPDATE segments SET flags=? WHERE segment_id=?',(json.dumps(['repetition_suspected']),'a'))
    edit(store,'Human verified translation.',field='translation:en')
    request=Runner(cfg,store,agent=OfflineExtractiveAgent())._build_request(dict(mode='session',target_id=None,source_revision=1,payload='{}'))
    segment=next(s for s in request.transcript if s['segment_id']=='a')
    assert segment['text']=='Human verified translation.' and segment['summary_text_source']=='manual_translation:en'
    assert store.segments()[0]['text']=='We used CASE evolution.'






def test_automatic_translation_marked_stale_until_regenerated(store):
    store.save_translation('a',target_lang='zh',text='Old',backend='mock')
    edit(store,'Changed source')
    assert store.translation('a','zh')['stale_source']
    edit(store,'Human translation',field='translation:zh')
    assert not store.translation('a','zh')['stale_source']


def test_regenerated_summary_after_review_keeps_previous_markdown(store,cfg):
    from scientific_conference.runner import Runner
    from scientific_conference.agents import OfflineExtractiveAgent
    runner=Runner(cfg,store,agent=OfflineExtractiveAgent())
    runner.request_overview();runner.process_tasks()
    old=store.notes()[0];path=Path(old['path']);original=path.read_text(encoding='utf8')
    edit(store,'Human corrected experiment with a different result.')
    runner.request_overview();runner.process_tasks()
    new=store.active_note('session',None)
    assert new['path']!=old['path']
    assert path.read_text(encoding='utf8')==original
    assert Path(new['path']).is_file()






def test_unauthorized_post_with_delayed_body_returns_http_403(dashboard):
    import socket,time,http.client
    from urllib.parse import urlsplit
    _,info=dashboard;url=urlsplit(info['origin']);body=b'{"session_id":"no_such_session"}'
    with socket.create_connection((url.hostname,url.port),timeout=3) as sock:
        headers=f'POST /api/notebook/export HTTP/1.0\r\nHost: {url.netloc}\r\nContent-Length: {len(body)}\r\nX-Conference-Token: bad\r\n\r\n'.encode()
        sock.sendall(headers);time.sleep(.02);sock.sendall(body)
        response=http.client.HTTPResponse(sock);response.begin()
        assert response.status==403 and b'Invalid origin or token' in response.read()
