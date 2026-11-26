"""Personal notebook persistence, conflicts and authenticated attachments."""
import base64
import pytest
from urllib.error import HTTPError
from scientific_conference import notebook
from scientific_conference.store import Store
from scientific_conference.dashboard import _request
from test_dashboard import dashboard

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQ0AAAAASUVORK5CYII=')

def test_notebook_roundtrip_and_conflict(cfg):
    store=Store.create(cfg.output_root,'personal_test',cfg,consent=True)
    assert notebook.read(store)['revision']==0
    result=notebook.save(store,'[02:30] Question about tRNA',0)
    assert result['revision']==1
    with pytest.raises(notebook.NotebookConflict):notebook.save(store,'stale tab',0)
    assert notebook.read(store)['text']=='[02:30] Question about tRNA'
    result=notebook.add_image(store,dict(content=base64.b64encode(PNG).decode(),name='../../slide.png',time_ms=150000))
    image=result['images'][0]
    assert notebook.image(store,image['id'])==(PNG,'image/png')
    assert '../../slide.png' not in image['id']
    with pytest.raises(ValueError):notebook.image(store,'../../session.sqlite3')
    with pytest.raises(ValueError):notebook.add_image(store,dict(content=base64.b64encode(b'<svg/>').decode(),time_ms=0))
    assert 'attachments/' in (store.paths.root/'personal_notes.md').read_text(encoding='utf-8')
    store.db.close()
    reopened=Store.open(cfg.output_root,'personal_test')
    assert notebook.read(reopened)['text'].startswith('[02:30]')
    assert len(notebook.read(reopened)['images'])==1
    reopened.db.close()

def test_notebook_http_and_session_isolation(cfg,dashboard):
    _,info=dashboard
    a=Store.create(cfg.output_root,'book_a',cfg,consent=True)
    b=Store.create(cfg.output_root,'book_b',cfg,consent=True)
    data=_request(info,'/api/notebook',dict(session_id='book_a',text='A private note',revision=0))
    assert data['text']=='A private note'
    assert _request(info,'/api/notebook?session_id=book_b')['text']==''
    with pytest.raises(HTTPError) as err:_request(info,'/api/notebook',dict(session_id='book_a',text='stale',revision=0))
    assert err.value.code==409
    result=_request(info,'/api/notebook/image',dict(session_id='book_a',content=base64.b64encode(PNG).decode(),name='slide.png',time_ms=0))
    image_id=result['images'][0]['id']
    with pytest.raises(HTTPError):_request(info,'/api/notebook/image?session_id=book_b&id='+image_id)
    with pytest.raises(HTTPError) as err:_request(dict(info,token='invalid'),'/api/notebook?session_id=book_a')
    assert err.value.code==403
    a.db.close();b.db.close()

def test_general_vocabulary_is_author_independent():
    from scientific_conference.glossary import profile,prompt,contextual_terms
    assert all(t in profile()['terms'] for t in ['HPLC','tRNA','NMR','PACE','metabolomics'])
    assert len(prompt())<=700
    assert 'accessory plasmid' in contextual_terms('bacteriophage mutagenesis directed evolution tRNA')
