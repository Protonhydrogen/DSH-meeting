import os
from types import SimpleNamespace
import pytest
from urllib.error import HTTPError
from test_dashboard import dashboard
from scientific_conference.dashboard import _request,summary_interval,snapshot
from scientific_conference.store import Store
from scientific_conference.recorder import Recorder,RecorderConfig

def test_interval_persist_validation_and_live_application(cfg,dashboard):
    _,info=dashboard
    assert summary_interval(cfg)==30
    assert _request(info,'/api/summary/interval',{'seconds':45})['seconds']==45
    assert summary_interval(cfg)==45
    for value in (0,9,301,True,15.5,'15'):
        with pytest.raises(HTTPError):_request(info,'/api/summary/interval',{'seconds':value})
    store=Store.create(cfg.output_root,'interval_test',cfg,consent=True)
    store.set_state('recording');store.set_pid(os.getpid())
    recorder=Recorder(cfg,store,RecorderConfig(session_id=store.session_id,output_root=cfg.output_root))
    recorder._segmenter=SimpleNamespace(cfg=cfg.segmenter)
    result=_request(info,'/api/summary/interval',{'seconds':15,'session_id':store.session_id})
    assert result['queued']
    recorder._apply_control()
    assert recorder._segmenter.cfg.provisional_interval_ms==15000
    assert snapshot(cfg,store.session_id)['summary_interval_ms']==15000
    assert store.db.query_one('SELECT state FROM control WHERE id=?',(result['control_id'],))['state']=='applied'
    store.set_state('closed')
    result=_request(info,'/api/summary/interval',{'seconds':30,'session_id':store.session_id})
    assert not result['queued']
    assert summary_interval(cfg)==30
    store.db.close()
