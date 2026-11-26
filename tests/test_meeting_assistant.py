import json
import pytest
from scientific_conference.meeting_assistant import validate_map,enqueue,state
from scientific_conference.runner import Runner
from scientific_conference.store import Store
from test_runner import seed_turn

class Agent:
    max_tokens=2400
    def complete_json(self,system,user):
        data=json.loads(user)
        sid=data['transcript'][0]['segment_id']
        return {'answer':'会议提到成像探针。','source_segment_ids':[sid,'invented'],
                'mind_map':{'title':'成像探针','nodes':[{'id':'root','parent_id':None,'label':'成像探针'},
                    {'id':'cancer','parent_id':'root','label':'肿瘤成像','source_segment_ids':[sid,'invented']}] }}, '{}'


def test_durable_assistant_and_map(cfg):
    cfg.privacy.allow_cloud_notes=True
    s=Store.create(cfg.output_root,'assistant_test',cfg,consent=True);seed_turn(s)
    first=enqueue(s,'整理导图','request-0001',True)
    assert not enqueue(s,'整理导图','request-0001',True)['created']
    runner=Runner(cfg,s,agent=Agent());result=runner._execute(s.task(first['task_id']))
    assert result.state=='done'
    saved=state(s)
    assert saved['messages'][0]['result']['answer']=='会议提到成像探针。'
    assert saved['messages'][0]['result']['source_segment_ids']==['seg_turn_001']
    assert saved['map']['nodes'][1]['source_segment_ids']==['seg_turn_001']
    old=saved['map']
    second=enqueue(s,'只回答','request-0002',False)
    runner._execute(s.task(second['task_id']))
    assert state(s)['map']==old
    s.db.close()


@pytest.mark.parametrize('nodes',[
    [{'id':'a','parent_id':None,'label':'A'},{'id':'b','parent_id':'missing','label':'B'}],
    [{'id':'a','parent_id':None,'label':'A'},{'id':'b','parent_id':'c','label':'B'},{'id':'c','parent_id':'b','label':'C'}],
    [{'id':'a','parent_id':None,'label':'A'},{'id':'a','parent_id':'a','label':'B'}],
])
def test_invalid_graph_rejected(nodes):
    with pytest.raises(ValueError):validate_map({'nodes':nodes},set())


def test_failed_map_preserves_previous(cfg):
    cfg.privacy.allow_cloud_notes=True
    s=Store.create(cfg.output_root,'assistant_failure',cfg,consent=True);seed_turn(s)
    class Bad(Agent):
        def complete_json(self,*args):return {'answer':'回答','mind_map':{'nodes':[]}},'{}'
    task=enqueue(s,'更新','request-0003',True)
    result=Runner(cfg,s,agent=Bad())._execute(s.task(task['task_id']))
    assert result.state=='failed'
    assert state(s)['map'] is None
    assert state(s)['messages'][0]['result'] is None
    s.db.close()


def test_assistant_modes_and_research_sources(cfg, monkeypatch):
    from scientific_conference import meeting_assistant
    cfg.privacy.allow_cloud_notes=True
    s=Store.create(cfg.output_root,'assistant_research',cfg,consent=True);seed_turn(s)
    with pytest.raises(ValueError):enqueue(s,'问题','request-bad-mode',False,'unknown')
    literature=[{'id':'L1','title':'OrthoREP study','authors':'A. Author','journal':'Science',
        'year':'2025','abstract':'Study abstract','doi':'10.1/example','pmid':'123',
        'url':'https://europepmc.org/article/MED/123'}]
    monkeypatch.setattr(meeting_assistant,'search_literature',lambda query: literature)
    class ResearchAgent:
        max_tokens=2400
        calls=[]
        def complete_json(self,system,user):
            data=json.loads(user);self.calls.append(data)
            if 'meeting_terms' in data:return {'query':'OrthoREP evolution'},'{}'
            sid=data['transcript'][0]['segment_id']
            assert data['supplied_literature']==literature
            return {'answer':'会议内容与文献补充。','source_segment_ids':[sid],
                'literature_source_ids':['L1','invented'],'mind_map':None},'{}'
    task=enqueue(s,'结合文献解释','request-research',False,'research')
    result=Runner(cfg,s,agent=ResearchAgent())._execute(s.task(task['task_id']))
    assert result.state=='done'
    saved=state(s)['messages'][0]
    assert saved['mode']=='research'
    assert saved['result']['mode_label']=='联网研究'
    assert saved['result']['literature_source_ids']==['L1']
    assert saved['result']['literature_sources'][0]['pmid']=='123'
    s.db.close()
