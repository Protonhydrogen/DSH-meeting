import json
import os
import time
from scientific_conference.runner import Runner,is_transient_error
from scientific_conference.agents import OfflineExtractiveAgent
from scientific_conference.store import Store
from scientific_conference.dashboard import _request
from test_dashboard import dashboard


def test_closed_session_does_not_reuse_stale_runner_pid(cfg, monkeypatch):
    from scientific_conference.dashboard import runner_alive

    s = Store.create(cfg.output_root, 'stale_runner', cfg, consent=True)
    s.set_state('closed')
    pid_path = s.paths.root / 'runner.pid'
    pid_path.write_text('123', encoding='ascii')
    monkeypatch.setattr('scientific_conference.dashboard.process_alive', lambda pid: True)

    old = time.time() - 16 * 60
    os.utime(pid_path, (old, old))
    assert runner_alive(s) is False

    now = time.time()
    os.utime(pid_path, (now, now))
    assert runner_alive(s) is True
    s.db.close()

def test_transient_errors_survive_attempt_limit(cfg,monkeypatch):
    s=Store.create(cfg.output_root,'retry_test',cfg,consent=True)
    runner=Runner(cfg,s,agent=OfflineExtractiveAgent())
    task,_=s.enqueue_task(type='note.turn',mode='turn',target_id='t',source_revision=1,payload={},idempotency_key='retry')
    with s.db.tx() as conn:conn.execute('UPDATE tasks SET attempts=99 WHERE task_id=?',(task,))
    def fail(_):raise RuntimeError('model endpoint unreachable: TLS connection reset')
    monkeypatch.setattr(runner,'_build_request',fail)
    result=runner._execute(s.task(task))
    assert result.state=='failed'
    assert s.task(task)['attempts']==100
    assert is_transient_error(RuntimeError('model endpoint returned HTTP 429'))
    assert not is_transient_error(RuntimeError('model endpoint returned HTTP 401'))
    s.db.close()

def test_manual_retry_requeues_only_failed_notes(cfg,dashboard,monkeypatch):
    server,info=dashboard
    cfg.runner.agent='openai_compatible'
    cfg.privacy.allow_cloud_notes=True
    s=Store.create(cfg.output_root,'manual_retry',cfg,consent=True)
    from scientific_conference.service import _write_config_snapshot
    _write_config_snapshot(cfg, s.paths.root/'config.snapshot.yaml')
    ids=[]
    for i in range(3):
        task,_=s.enqueue_task(type='note.turn',mode='turn',target_id='t',source_revision=1,payload={},idempotency_key=str(i));ids.append(task)
    s.fail_task(ids[0],'network',retry_at=None,dead=True)
    s.complete_task(ids[1])
    with s.db.tx() as conn:conn.execute("UPDATE tasks SET state='leased' WHERE task_id=?",(ids[2],))
    (s.paths.root/'runner.pid').write_text('123')
    monkeypatch.setattr('scientific_conference.dashboard.process_alive',lambda pid:True)
    monkeypatch.setattr('scientific_conference.dashboard.spawn_runner',lambda *a:(_ for _ in ()).throw(AssertionError('duplicate runner')))
    result=_request(info,'/api/summary/retry',{'session_id':s.session_id})
    assert result['requeued']==1 and result['runner_reused']
    assert [s.task(t)['state'] for t in ids]==['pending','done','leased']
    assert _request(info,'/api/summary/retry',{'session_id':s.session_id})['requeued']==0
    s.db.close()


def test_closed_recovery_skips_obsolete_and_enqueues_missing(cfg):
    from test_runner import seed_turn
    from scientific_conference.events import EventQueue
    s=Store.create(cfg.output_root,'closed_recovery',cfg,consent=True)
    q=EventQueue(s.db)
    provisional,_=q.append(s.session_id,'turn.provisional',{'turn_id':'turn_001','revision':1,'end_ms':1000})
    r=Runner(cfg,s,agent=OfflineExtractiveAgent())
    old,_=r._enqueue_for_event(provisional)
    s.fail_task(old,'output length',retry_at=None,dead=True)
    seed_turn(s)
    s.set_state('closed')
    r.poll_and_enqueue()
    assert s.task(old)['state']=='done'
    finals=[t for t in s.tasks() if t['type']=='note.turn' and t['state']=='pending']
    assert len(finals)==1
    count=len(s.tasks())
    r.poll_and_enqueue()
    assert len(s.tasks())==count
    r.process_tasks()
    assert len(s.notes(active_only=True))==1
    s.db.close()



def test_summary_budget_does_not_change_correction_budget(monkeypatch):
    from scientific_conference.agents import OpenAiCompatibleAgent,NoteRequest,OutputLimitError
    agent=OpenAiCompatibleAgent(base_url='https://example.invalid/v1',model='test',max_tokens=2400,require_api_key=False)
    seen=[]
    def complete(self,system,user):
        seen.append((self.max_tokens,user))
        if len(seen)==1: raise OutputLimitError('length')
        return {'summary':'Test','findings':[]},'{}'
    monkeypatch.setattr(OpenAiCompatibleAgent,'complete_json',complete)
    agent.generate(NoteRequest(mode='turn',session_id='s',target_id='t',source_revision=1,transcript=[],language='en'))
    assert [x[0] for x in seen]==[2400,6000]
    assert 'Omit evidence_index' in seen[0][1]
    assert agent.max_tokens==2400
