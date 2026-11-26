from scientific_conference.runner import Runner
from scientific_conference.agents import OfflineExtractiveAgent,build_prompt
from scientific_conference.store import Store
from test_runner import seed_turn


def test_midmeeting_overview_snapshot_and_dedup(cfg):
    s=Store.create(cfg.output_root,'overview',cfg,consent=True)
    s.set_state('recording')
    seed_turn(s)
    r=Runner(cfg,s,agent=OfflineExtractiveAgent())
    first=r.request_overview()
    assert first['provisional'] and first['created']
    assert not r.request_overview()['created']
    s.add_segment('later',start_ms=4000,end_ms=5000,text='Later result.',turn_id='turn_001',talk_id='talk_001')
    req=r._build_request(s.task(first['task_id']))
    assert 'later' not in [x['segment_id'] for x in req.transcript]
    assert s.session_state()=='recording'
    r._execute(s.task(first['task_id']))
    second=r.request_overview()
    assert second['created']
    r._execute(s.task(second['task_id']))
    assert len(s.notes(mode='session',active_only=True))==1
    s.set_state('closed')
    final=r.request_overview()
    assert not final['provisional']
    r._execute(s.task(final['task_id']))
    assert len(s.notes(mode='session',active_only=True))==1
    assert not s.active_note('session',None)['provisional']
    # A late provisional result cannot replace the final summary.
    assert s.session_state()=='closed'
    s.db.close()


def test_chinese_note_instruction_preserves_english_transcript(cfg):
    cfg.notes.language='zh'
    s=Store.create(cfg.output_root,'zh_overview',cfg,consent=True)
    seed_turn(s,text='A protein imaging probe.')
    r=Runner(cfg,s,agent=OfflineExtractiveAgent())
    result=r.request_overview()
    request=r._build_request(s.task(result['task_id']))
    assert request.language=='zh'
    prompt=build_prompt(request)
    assert '简体中文' in prompt and 'A protein imaging probe.' in prompt
    assert s.segments()[0]['text']=='A protein imaging probe.'
    s.db.close()


def test_session_close_automatically_replaces_midmeeting_overview(cfg):
    from scientific_conference.events import EventQueue
    s=Store.create(cfg.output_root,'auto_final',cfg,consent=True)
    s.set_state('recording');seed_turn(s)
    r=Runner(cfg,s,agent=OfflineExtractiveAgent())
    interim=r.request_overview()
    r._execute(s.task(interim['task_id']))
    s.set_state('closed')
    EventQueue(s.db).append(s.session_id,'session.closed',{},dedupe_key='session.closed')
    r.run_cycle()
    finals=s.notes(mode='session',active_only=True)
    assert len(finals)==1 and not finals[0]['provisional']
    s.db.close()
