import json
import os
from types import SimpleNamespace
import pytest
from scientific_conference.reading import reading_blocks
from scientific_conference.credentials import crypt, save, load_into_environment
from scientific_conference.notes import render_markdown
from scientific_conference.agents import AgentAdapter
from scientific_conference.store import Store
from scientific_conference.events import EventQueue
from scientific_conference.runner import Runner






def seg(i,text,start,end,speaker='s',talk='t'):
    return dict(segment_id=i,text=text,start_ms=start,end_ms=end,speaker_id=speaker,talk_id=talk)


def test_reading_preserves_words_and_sources():
    data=[seg('a','We engineered an',0,8000),seg('b','orthogonal tRNA.',8000,12000)]
    blocks=reading_blocks(data)
    assert len(blocks)==1
    assert blocks[0]['text']=='We engineered an orthogonal tRNA.'
    assert blocks[0]['state']=='Stable'
    assert [s['segment_id'] for s in blocks[0]['sources']]==['a','b']


def test_reading_drafts_and_report_boundary():
    data=[seg('a','Dr.',0,1000),seg('b','Example reported',1000,2000),seg('c','New report',2100,3000,talk='other')]
    blocks=reading_blocks(data)
    assert len(blocks)==2
    assert blocks[0]['state']=='Incomplete sentence'
    assert blocks[1]['state']=='Draft'
    assert reading_blocks(data,closed=True)[-1]['state']=='Incomplete sentence'


@pytest.mark.skipif(os.name!='nt',reason='DPAPI is Windows-only')
def test_credentials_encrypted_roundtrip(tmp_path,monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path))
    monkeypatch.delenv('TEST_CONFERENCE_KEY',raising=False)
    value='synthetic-test-value-not-a-real-key'
    save('test',value)
    assert value.encode() not in next(tmp_path.rglob('*.dpapi')).read_bytes()
    load_into_environment('test','TEST_CONFERENCE_KEY')
    assert os.environ['TEST_CONFERENCE_KEY']==value
    monkeypatch.delenv('TEST_CONFERENCE_KEY')


def test_english_note_has_no_empty_template_sections():
    text=render_markdown({'language':'en','summary':'The enzyme retained activity.','findings':[]})
    assert 'The enzyme retained activity.' in text
    assert 'Methods' not in text and '未提及' not in text


class Capture(AgentAdapter):
    name='test'
    def __init__(self): self.requests=[]
    def generate(self,r):
        self.requests.append(r)
        return {'summary':'A supported evolving note.','findings':[]},'{}'


def test_incremental_notes_skip_idle_and_only_send_new_uncited_text(cfg):
    cfg.runner.agent='openai_compatible'
    cfg.notes.language='en'
    store=Store.create(cfg.output_root,'incremental',cfg,consent=True)
    store.set_state('recording')
    store.open_talk('talk_001',0,'glossary_demo-second')
    store.open_turn('turn_001',0,talk_id='talk_001',speaker_id='glossary_demo-second')
    store.add_segment('one',start_ms=0,end_ms=1000,text='The first method.',turn_id='turn_001',talk_id='talk_001',speaker_id='glossary_demo-second')
    events=EventQueue(store.db)
    agent=Capture();runner=Runner(cfg,store,agent=agent)
    def tick(end):
        events.append(store.session_id,'turn.provisional',{'turn_id':'turn_001','revision':1,'end_ms':end,'provisional':True},dedupe_key=str(end))
        runner.run_cycle()
    tick(30000)
    tick(60000)
    assert len(agent.requests)==1
    store.add_segment('two',start_ms=60000,end_ms=61000,text='A new result.',turn_id='turn_001',talk_id='talk_001',speaker_id='glossary_demo-second')
    tick(90000)
    assert len(agent.requests)==2
    assert [s['segment_id'] for s in agent.requests[-1].transcript]==['two']
    assert agent.requests[-1].context['previous_note']['summary']=='A supported evolving note.'
    hints=agent.requests[-1].context['vocabulary_hints_not_evidence']
    assert hints['scope']=='General chemistry and life sciences'
    assert 'protein' in hints['terms']
    store.db.close()
