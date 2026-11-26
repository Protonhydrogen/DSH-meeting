from scientific_conference.service import spawn_runner
from scientific_conference.asr.faster_whisper_backend import resolve_local_model

def test_external_does_not_spawn(monkeypatch, cfg):
    cfg.runner.agent = 'external'
    monkeypatch.setattr('scientific_conference.service.subprocess.Popen', lambda *a, **k: (_ for _ in ()).throw(AssertionError('unexpected process')))
    assert spawn_runner(cfg, 'not-created') == 0

def test_turbo_repository_resolution(tmp_path):
    snapshot = tmp_path / 'models--mobiuslabsgmbh--faster-whisper-large-v3-turbo' / 'snapshots' / 'test'
    snapshot.mkdir(parents=True)
    assert resolve_local_model('turbo', None, tmp_path) == snapshot
