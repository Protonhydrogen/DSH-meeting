from scientific_conference.correction import apply_edits, review
from scientific_conference.store import Store
from scientific_conference.reading import reading_blocks


def test_repetition_guard_keeps_normal_context_and_raw(cfg):
    from scientific_conference.correction import clean_repetitions, REPETITION_MARKER
    store = Store.create(cfg.output_root, 'repeat_test', cfg, consent=True)
    original = 'Vocabulary, MD, ' + 'PhD, ' * 90 + 'But genes share a genome.'
    store.add_segment('loop', start_ms=0, end_ms=920, text=original)
    store.add_segment('normal', start_ms=1000, end_ms=2000, text='No, no, no. The pH is 7.')
    clean_repetitions(store, store.segments())
    result = store.segments()
    assert result[0]['raw_text'] == original
    assert REPETITION_MARKER in result[0]['text']
    assert result[0]['text'].endswith('But genes share a genome.')
    assert result[1]['text'] == 'No, no, no. The pH is 7.'
    store.db.close()


def test_local_edits_protect_numbers_negation_and_unknown_ids():
    targets=[{'segment_id':'s','text':'CASE achieved 40% without selection.'}]
    def edit(old,new,**kw):
        return dict(segment_id='s',old=old,new=new,confidence=.99,reason='Context supports the term',**kw)
    result=apply_edits(targets,{'edits':[edit('40','90'),edit('without','with'),edit('CASE','PACE')]})
    assert result['s'][0]=='PACE achieved 40% without selection.'
    assert apply_edits(targets,{'edits':[dict(segment_id='x',old='CASE',new='PACE',confidence=1)]})['s'][0]==targets[0]['text']


def test_review_preserves_raw_and_deduplicates(cfg):
    store=Store.create(cfg.output_root,'correction_test',cfg,consent=True)
    store.add_segment('s',start_ms=0,end_ms=2000,text='We used CASE evolution.')
    class Fake:
        temperature=.2
        timeout_s=60
        model='fake'
        calls=[]
        def complete_json(self,system,user):
            self.calls.append(user)
            return {'edits':[dict(segment_id='s',old='CASE',new='PACE',confidence=.99,reason='Evolution context')]},''
    agent=Fake()
    review(store,agent,store.segments())
    corrected=store.segments()
    assert corrected[0]['text']=='We used PACE evolution.'
    assert corrected[0]['raw_text']=='We used CASE evolution.'
    assert store.segment('s')['text']=='We used CASE evolution.'
    assert reading_blocks(corrected)[0]['sources'][0]['raw_text']=='We used CASE evolution.'
    review(store,agent,corrected)
    assert len(agent.calls)==2
    store.add_segment('s',start_ms=0,end_ms=2000,text='An updated raw transcript.')
    assert store.segments()[0]['text']=='An updated raw transcript.'


def test_repeated_terms_and_word_boundaries():
    targets=[{'segment_id':'s','text':'plasma on the accessory plasma, not plasmas.'}]
    edit=dict(segment_id='s',old='plasma',new='plasmid',confidence=.95,reason='Vector context')
    assert apply_edits(targets,{'edits':[edit]})['s'][0]=='plasmid on the accessory plasmid, not plasmas.'


def test_units_and_low_confidence_are_rejected():
    targets=[{'segment_id':'s','text':'10 mg of CASE'}]
    edits=[dict(segment_id='s',old='mg',new='kg',confidence=.99,reason='Guess'),
           dict(segment_id='s',old='CASE',new='PACE',confidence=.8,reason='Guess')]
    assert apply_edits(targets,{'edits':edits})['s'][0]==targets[0]['text']


def test_new_following_context_triggers_recheck(cfg):
    store=Store.create(cfg.output_root,'lookahead_test',cfg,consent=True)
    store.add_segment('s',start_ms=0,end_ms=1000,text='Called CASE.')
    class Fake:
        temperature=.2
        timeout_s=60
        model='fake'
        calls=[]
        def complete_json(self,system,user):
            self.calls.append(user)
            import json
            if any('continuous evolution' in segment['text'] for segment in json.loads(user)['context']):
                return {'edits':[dict(segment_id='s',old='CASE',new='PACE',confidence=.95,reason='Following definition')]},''
            return {'edits':[]},''
    agent=Fake()
    review(store,agent,store.segments())
    assert store.segments()[0]['text']=='Called CASE.'
    store.add_segment('s2',start_ms=1000,end_ms=2000,text='Phage-assisted continuous evolution.')
    review(store,agent,store.segments())
    assert store.segments()[0]['text']=='Called PACE.'
    calls=len(agent.calls)
    review(store,agent,store.segments())
    assert len(agent.calls)==calls


def test_sentence_proposal_reduces_to_terminology_without_numbers():
    from scientific_conference.correction import local_edits
    original='We used CASE for 10 minutes with 40% efficiency.'
    proposal=dict(segment_id='s',old=original,new='We used PACE for 20 minutes with 90% efficiency.',confidence=.95,reason='Term context')
    result=apply_edits([dict(segment_id='s',text=original)],{'edits':local_edits({'edits':[proposal]})})
    assert result['s'][0]=='We used PACE for 10 minutes with 40% efficiency.'


def test_polarity_is_not_reversed():
    targets=[dict(segment_id='s',text='canonical amino acids are infectious')]
    edits=[dict(segment_id='s',old='canonical',new='noncanonical',confidence=.99,reason='Guess'),
           dict(segment_id='s',old='infectious',new='non-infectious',confidence=.99,reason='Guess')]
    assert apply_edits(targets,{'edits':edits})['s'][0]==targets[0]['text']


def test_model_quote_typo_alignment_is_unique_and_preserves_numbers():
    from scientific_conference.correction import align_proposals,local_edits
    targets=[dict(segment_id='s',text='Read this quadruclic codon at 40%.')]
    edit=dict(segment_id='s',old='quadroclic codon',new='quadruplet codon',confidence=.95,reason='Codon context')
    payload=align_proposals(targets,{'edits':[edit]})
    assert payload['edits'][0]['old']=='quadruclic codon'
    assert apply_edits(targets,{'edits':local_edits(payload)})['s'][0]=='Read this quadruplet codon at 40%.'


def test_deepseek_final_review_reuses_first_request_prefix(cfg, monkeypatch):
    import json
    from scientific_conference.agents import OpenAiCompatibleAgent

    store = Store.create(cfg.output_root, 'cache_prefix_test', cfg, consent=True)
    store.add_segment('s', start_ms=0, end_ms=2000, text='We used CASE evolution.')
    first_raw = json.dumps({'edits': [dict(segment_id='s', old='CASE', new='PACE',
                                          confidence=.99, reason='Evolution context')]})
    captured = []
    usage = []

    class Response:
        def __init__(self, content):
            self.content = content
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self):
            return json.dumps({'choices': [{'finish_reason': 'stop',
                                           'message': {'content': self.content}}],
                               'usage': {'prompt_tokens': 100, 'prompt_cache_hit_tokens': 80,
                                         'prompt_cache_miss_tokens': 20, 'completion_tokens': 10,
                                         'total_tokens': 110}}).encode()

    def open_request(req, **kwargs):
        captured.append(json.loads(req.data))
        return Response(first_raw)

    monkeypatch.setattr('scientific_conference.agents.urllib.request.urlopen', open_request)
    agent = OpenAiCompatibleAgent(base_url='https://api.deepseek.com/v1', model='deepseek-flash')
    agent.usage_callback = usage.append
    review(store, agent, store.segments())

    assert len(captured) == 2
    first, final = (call['messages'] for call in captured)
    assert final[:2] == first
    assert final[2] == {'role': 'assistant', 'content': first_raw}
    assert final[3]['role'] == 'user'
    assert 'Proposed edits are fallible suggestions' in final[3]['content']
    material = json.loads(first[1]['content'])
    assert list(material) == ['standard_definitions_not_claims', 'context',
                              'vocabulary_hints', 'target_ids']
    assert store.segments()[0]['text'] == 'We used PACE evolution.'
    assert [record['purpose'] for record in usage] == [
        'transcript.review.propose', 'transcript.review.final']
    assert [record['message_count'] for record in usage] == [2, 4]
    store.db.close()
