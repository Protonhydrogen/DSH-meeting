"""Lossless reading, clip timeline and Markdown placement contracts."""
import base64
import io
import wave
import pytest
from scientific_conference.reading import reading_blocks
from scientific_conference.store import Store
from scientific_conference import notebook
from test_dashboard import dashboard


def seg(i, text, start=0, end=4000, **kw):
    return dict(segment_id=i,text=text,start_ms=start,end_ms=end,speaker_id='s',talk_id='t',**kw)


def test_split_sentences_and_protect_scientific_notation():
    text='Dr. Chin used E. coli, e.g. at 2.5 mM. See Fig. 2 for U.S. data. 测定完成。结果可靠！'
    blocks=reading_blocks([seg('a',text)],closed=True)
    assert [b['text'] for b in blocks]==['Dr. Chin used E. coli, e.g. at 2.5 mM.','See Fig. 2 for U.S. data.','测定完成。','结果可靠！']
    assert all(b['timing_precision']=='source_window' for b in blocks)
    assert ''.join(text[s['char_start']:s['char_end']] for b in blocks for s in b['sources']).replace(' ','')==text.replace(' ','')


def test_cross_window_sentence_and_stable_prefix_ids():
    a=seg('a','We used an',0,4000)
    b=seg('b','orthogonal tRNA. Next',4000,8000)
    c=seg('c','experiment.',8000,10000)
    before=reading_blocks([a,b]); after=reading_blocks([a,b,c])
    assert after[0]['text']=='We used an orthogonal tRNA.'
    assert after[1]['text']=='Next experiment.'
    assert [x['block_id'] for x in before]==[x['block_id'] for x in after]
    assert [x['segment_id'] for x in after[0]['sources']]==['a','b']


def test_pause_report_and_anomaly_never_merge():
    blocks=reading_blocks([seg('a','unfinished',0,1000),seg('b','new context',5000,6000),seg('c','loop '*40,6000,6500,flags=['repetition_suspected']),seg('d','Safe.',6500,7000)])
    assert len(blocks)==4
    assert blocks[2]['anomalous'] is True
    assert blocks[2]['text']=='loop '*39+'loop'


def test_sentence_quotes_and_initials():
    data=[seg('a','J. K. Smith said "Done." Then used pH 7.4 and cf. Fig. 3. A second result.')]
    assert [b['text'] for b in reading_blocks(data)]==['J. K. Smith said "Done."','Then used pH 7.4 and cf. Fig. 3.','A second result.']


def test_audio_clip_preserves_gap_and_rejects_escape(cfg):
    from scientific_conference.playback import audio_clip
    store=Store.create(cfg.output_root,'clip_test',cfg,consent=True)
    p=store.paths.root/'a.wav'
    with wave.open(str(p),'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b'\x01\x00'*16000)
    store.add_chunk(chunk_index=1,path='a.wav',converted_path='a.wav',start_ms=500,end_ms=1500,frames=16000,sample_rate=16000,channels=1)
    body=audio_clip(store,0,2000)
    with wave.open(io.BytesIO(body)) as w:
        assert w.getnframes()==32000
        pcm=w.readframes(32000)
    assert pcm[:16000]==bytes(16000) and pcm[16000:48000]==b'\x01\x00'*16000
    with pytest.raises(ValueError):audio_clip(store,0,61000)
    with store.db.tx() as c:c.execute("UPDATE audio_chunks SET converted_path='../outside.wav'")
    with pytest.raises(ValueError):audio_clip(store,0,2000)
    store.db.close()


def test_image_marker_placement_and_combined_export(cfg):
    from test_notebook import PNG
    store=Store.create(cfg.output_root,'placement',cfg,consent=True)
    book=notebook.add_image(store,dict(content=base64.b64encode(PNG).decode(),name='Slide',time_ms=12000))
    identifier=book['images'][0]['id']
    marker=f'![Slide](attachments/{identifier})'
    notebook.save(store,'Before\n\n'+marker+'\n\nAfter',0)
    md=(store.paths.root/'personal_notes.md').read_text(encoding='utf8')
    assert md.count(marker)==1
    assert md.index('Before')<md.index(marker)<md.index('After')
    combined=notebook.combined_markdown(store)
    assert combined.count(marker)==1 and 'Personal meeting notes' in combined
    store.db.close()



@pytest.mark.parametrize('mode,target',[('turn','turn_1'),('talk','talk_1'),('session',None)])
def test_suspected_loop_is_excluded_from_new_summary(cfg,mode,target):
    from scientific_conference.runner import Runner
    from scientific_conference.agents import OfflineExtractiveAgent
    store=Store.create(cfg.output_root,'guard_'+mode,cfg,consent=True)
    store.open_talk('talk_1',0,'s');store.open_turn('turn_1',0,talk_id='talk_1',speaker_id='s')
    for identifier,text,flags in [('good','The assay retained activity.',[]),('bad',"and you are ready to go, "*31,['repetition_suspected'])]:
        store.add_segment(identifier,start_ms=0,end_ms=1120,text=text,flags=flags,talk_id='talk_1',turn_id='turn_1',speaker_id='s')
    runner=Runner(cfg,store,agent=OfflineExtractiveAgent())
    req=runner._build_request(dict(mode=mode,type='note.'+mode,target_id=target,source_revision=1,payload='{}'))
    assert [s['segment_id'] for s in req.transcript]==['good']
    assert req.context['excluded_anomalies'][0]['segment_id']=='bad'
    assert len(store.segments())==2
    store.db.close()


def test_audio_and_export_require_existing_authentication(cfg,dashboard):
    from scientific_conference.dashboard import _request
    from urllib.error import HTTPError
    from urllib.request import ProxyHandler,Request,build_opener
    _,info=dashboard
    store=Store.create(cfg.output_root,'api_clip',cfg,consent=True)
    notebook.save(store,'[00:12] User observation',0)
    with pytest.raises(HTTPError) as err:_request(dict(info,token='bad'),'/api/notebook/export',{'session_id':'api_clip'})
    assert err.value.code==403
    data=_request(info,'/api/notebook/export',{'session_id':'api_clip'})
    assert 'User observation' in data['markdown']
    with pytest.raises(HTTPError) as err:_request(dict(info,token='bad'),'/api/audio?session_id=api_clip&start_ms=0&end_ms=1000')
    assert err.value.code==403
    req=Request(info['origin']+'/workbench.css')
    with build_opener(ProxyHandler({})).open(req) as response:assert response.headers['Content-Type'].startswith('text/css')
    store.db.close()


def test_talk_request_drops_old_turn_summaries_when_audio_is_suspect(cfg,monkeypatch):
    from scientific_conference.runner import Runner
    from scientific_conference.agents import OfflineExtractiveAgent
    store=Store.create(cfg.output_root,'stale_turn_summary',cfg,consent=True)
    store.open_talk('talk_1',0,'s');store.open_turn('turn_1',0,talk_id='talk_1',speaker_id='s')
    store.add_segment('bad',start_ms=0,end_ms=1120,text='loop',flags=['repetition_suspected'],talk_id='talk_1',turn_id='turn_1',speaker_id='s')
    # Existing notes are a distinct derived evidence channel, not the ASR rows.
    monkeypatch.setattr(store,'active_note',lambda *args:dict(content_json='{"summary":"Fabricated old claim"}',source_revision=1))
    req=Runner(cfg,store,agent=OfflineExtractiveAgent())._build_request(dict(mode='talk',target_id='talk_1',source_revision=1,payload='{}'))
    assert 'turn_notes' not in req.context
    store.db.close()


def test_combined_export_does_not_hide_later_notes(cfg,monkeypatch):
    store=Store.create(cfg.output_root,'export_coverage',cfg,consent=True)
    monkeypatch.setattr(store,'notes',lambda:[dict(mode='session',content_md='EARLY OVERVIEW'),dict(mode='talk',content_md='LATER REPORT'),dict(mode='turn',content_md='OPEN REPORT TURN')])
    text=notebook.combined_markdown(store)
    assert all(part in text for part in ['EARLY OVERVIEW','LATER REPORT','OPEN REPORT TURN'])
    store.db.close()


def test_presenter_and_report_title_edit_preserves_session_links(cfg,dashboard):
    from scientific_conference.dashboard import _request
    from urllib.error import HTTPError
    _,info=dashboard
    store=Store.create(cfg.output_root,'rename_report',cfg,consent=True,title='Original historical title')
    store.add_segment('source_a',start_ms=1000,end_ms=3000,text='Unchanged evidence.')
    notebook.save(store,'[00:01] Private observation',0)
    before=_request(info,'/api/snapshot?session_id=rename_report')
    assert before['presenter']=='none' and before['report_title']=='Original historical title'
    renamed=_request(info,'/api/meeting/metadata',dict(session_id='rename_report',presenter='Demo Presenter',report_title='Roof garden irrigation'))
    assert renamed['presenter']=='Demo Presenter'
    after=_request(info,'/api/snapshot?session_id=rename_report')
    assert after['session_id']==before['session_id'] and after['segments']==before['segments']
    assert after['report_title']=='Roof garden irrigation'
    assert after['title']=='Demo Presenter · Roof garden irrigation'
    assert notebook.read(store)['text']=='[00:01] Private observation'
    item=next(x for x in _request(info,'/api/info')['sessions'] if x['session_id']=='rename_report')
    assert item['presenter']=='Demo Presenter' and item['report_title']=='Roof garden irrigation'
    with pytest.raises(HTTPError):_request(info,'/api/meeting/metadata',dict(session_id='rename_report',presenter='x'*121,report_title='Bad'))
    assert _request(info,'/api/snapshot?session_id=rename_report')['report_title']=='Roof garden irrigation'
    store.db.close()


def test_original_labels_split_presenter_without_inventing_report_title():
    from scientific_conference.meeting_metadata import infer
    assert infer('DemoHOST,SampleInstitute')==('Demo HOST','none')
    assert infer('20300101-Sample Presenter')==('Sample Presenter','none')
    assert infer('Demo L. Presenter, a trial workflow')==('Demo L. Presenter','a trial workflow')


def test_new_recording_stores_separate_report_fields(cfg,dashboard,monkeypatch):
    server,info=dashboard
    def start_without_microphone(config,**kwargs):
        store=Store.create(config.output_root,'new_report',config,consent=True,title=kwargs['title'])
        store.db.close();return dict(session_id='new_report')
    monkeypatch.setattr('scientific_conference.dashboard.start_session',start_without_microphone)
    monkeypatch.setattr('scientific_conference.dashboard.spawn_runner',lambda *args:123)
    result=server.start_recording(title='New experiment',presenter='Dr. Example',report_title='New experiment',consent=True)
    assert result['session_id']=='new_report'
    reopened=Store.open(cfg.output_root,'new_report')
    from scientific_conference.meeting_metadata import read
    assert read(reopened)['presenter']=='Dr. Example' and read(reopened)['report_title']=='New experiment'
    reopened.db.close()


def test_truncated_historical_affiliation_is_not_a_report_title():
    from scientific_conference.meeting_metadata import infer
    assert infer('SampleHOST,ExampleUniversit')==('Sample HOST','none')
