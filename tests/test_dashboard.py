"""Live dashboard HTTP integration: real DB/events/runner; no microphone."""
import json
import os
import threading
import sqlite3
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

import pytest

from scientific_conference.dashboard import Dashboard, _request, snapshot
from scientific_conference.events import EventQueue
from scientific_conference.runner import Runner
from scientific_conference.store import Store


@pytest.fixture
def dashboard(cfg):
    server = Dashboard(cfg)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, {"origin": server.origin, "token": server.token}
    server.shutdown()
    server.server_close()
    thread.join(3)


def seed(cfg):
    store = Store.create(cfg.output_root, "sess_live_test", cfg, consent=True)
    store.set_state("recording")
    store.set_pid(os.getpid())
    store.open_talk("talk_001", 0)
    store.open_turn("turn_001", 0, talk_id="talk_001", speaker_id="speaker_01")
    store.add_segment("seg_one", start_ms=0, end_ms=3000,
                      text="研究结果显示，样本量为 482，p 值小于 0.001。<script>bad()</script>",
                      turn_id="turn_001", talk_id="talk_001", speaker_id="speaker_01")
    return store


def test_snapshot_updates_and_notes_after_stop(cfg, dashboard):
    _, info = dashboard
    store = seed(cfg)
    first = _request(info, "/api/snapshot?session_id=sess_live_test")
    assert len(first["segments"]) == 1
    assert first["notes"] == []
    stopped = _request(info, "/api/stop", {"session_id": store.session_id})
    assert stopped["requested"] is True
    assert store.pop_control()[0]["command"] == "stop"
    # A recorder may close before the notes runner has finished.
    store.update_turn("turn_001", end_ms=3000, state="finalized", boundary_reason="manual")
    EventQueue(store.db).append(store.session_id, "turn.finalized",
        {"turn_id":"turn_001", "talk_id":"talk_001", "revision":1}, dedupe_key="test")
    store.set_state("closed")
    Runner(cfg, store).run_cycle()
    final = _request(info, "/api/snapshot?session_id=sess_live_test")
    assert final["state"] == "closed"
    assert len(final["notes"]) == 1
    assert "482" in final["notes"][0]["content_md"]
    assert final["agent"] == "offline_extractive"
    store.db.close()


def test_auth_origin_and_path_guards(cfg, dashboard):
    _, info = dashboard
    for headers in ({}, {"X-Conference-Token": info["token"], "Origin":"https://untrusted.example"},
                    {"X-Conference-Token": info["token"], "Host":"untrusted.example"}):
        with pytest.raises(HTTPError) as err:
            build_opener(ProxyHandler({})).open(Request(info["origin"] + "/api/info", headers=headers))
        assert err.value.code == 403
    with pytest.raises(HTTPError) as err:
        _request(info, "/api/snapshot?session_id=../outside")
    assert err.value.code == 400


def test_history_delete_requires_confirmation_and_moves_to_trash(cfg, dashboard):
    _, info = dashboard
    store = seed(cfg)
    session_id = store.session_id
    store.set_state("closed")
    store.db.close()

    with pytest.raises(HTTPError) as err:
        _request(info, "/api/history/delete", {
            "session_id": session_id,
            "confirm_session_id": "another_session",
        })
    assert err.value.code == 400
    assert (cfg.output_root / session_id / "session.sqlite3").exists()

    result = _request(info, "/api/history/delete", {
        "session_id": session_id,
        "confirm_session_id": session_id,
    })
    assert result["deleted"] is True
    assert result["recoverable"] is True
    assert not (cfg.output_root / session_id).exists()
    archived = list((cfg.output_root / "_trash").glob(session_id + "--*"))
    assert len(archived) == 1
    marker = json.loads((archived[0] / "deleted.json").read_text(encoding="utf-8"))
    assert marker["session_id"] == session_id
    assert marker["recoverable"] is True
    assert all(s["session_id"] != session_id for s in _request(info, "/api/info")["sessions"])


def test_history_delete_rejects_active_meeting(cfg, dashboard):
    _, info = dashboard
    store = seed(cfg)
    with pytest.raises(HTTPError) as err:
        _request(info, "/api/history/delete", {
            "session_id": store.session_id,
            "confirm_session_id": store.session_id,
        })
    assert err.value.code == 400
    assert (cfg.output_root / store.session_id / "session.sqlite3").exists()
    store.db.close()


def test_history_delete_allows_stale_interrupted_meeting(cfg, dashboard):
    _, info = dashboard
    store = seed(cfg)
    session_id = store.session_id
    with store.db.tx() as conn:
        conn.execute(
            "UPDATE sessions SET state='recording', pid=?, updated_utc=? WHERE session_id=?",
            (99999999, "2000-01-01T00:00:00+00:00", session_id),
        )
    store.db.close()

    result = _request(info, "/api/history/delete", {
        "session_id": session_id,
        "confirm_session_id": session_id,
    })
    assert result["deleted"] is True
    assert not (cfg.output_root / session_id).exists()


def test_history_delete_retries_transient_windows_file_lock(cfg, dashboard, monkeypatch):
    _, info = dashboard
    store = seed(cfg)
    session_id = store.session_id
    store.set_state("closed")
    store.db.close()
    original_rename = Path.rename
    attempts = 0

    def flaky_rename(path, destination):
        nonlocal attempts
        if path == cfg.output_root / session_id and attempts < 2:
            attempts += 1
            raise PermissionError(5, "file is temporarily in use")
        return original_rename(path, destination)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    result = _request(info, "/api/history/delete", {
        "session_id": session_id,
        "confirm_session_id": session_id,
    })
    assert result["deleted"] is True
    assert attempts == 2


def test_snapshot_closes_translation_connection_before_delete(cfg, dashboard, monkeypatch):
    from scientific_conference import service
    _, info = dashboard
    store = seed(cfg)
    store.set_state('closed')
    store.db.close()
    connections = []
    original = service.open_store

    def tracked_open(*args, **kwargs):
        opened = original(*args, **kwargs)
        connections.append(opened.db.conn)
        return opened

    monkeypatch.setattr(service, 'open_store', tracked_open)
    _request(info, '/api/snapshot?session_id=sess_live_test')
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')
    result = _request(info, '/api/history/delete', {
        'session_id': 'sess_live_test', 'confirm_session_id': 'sess_live_test',
    })
    assert result['deleted']


def test_start_reattaches_and_boundary(cfg, dashboard, monkeypatch):
    server, info = dashboard
    store = seed(cfg)
    monkeypatch.setattr("scientific_conference.dashboard.start_session", lambda *a, **k: pytest.fail("duplicate recording"))
    result = _request(info, "/api/start", {"title":"second click", "consent":True})
    assert result["reused"] and result["session_id"] == store.session_id
    _request(info, "/api/boundary", {"session_id":store.session_id,"kind":"talk"})
    assert store.pop_control()[0]["command"] == "boundary"
    store.db.close()


def test_start_requires_consent_before_microphone(cfg, dashboard):
    cfg.privacy.require_consent = True
    _, info = dashboard
    with pytest.raises(HTTPError) as err:
        _request(info, "/api/start", {"consent":False})
    assert err.value.code == 400
    assert not list(cfg.output_root.glob("*/session.sqlite3"))


def test_new_revision_replaces_old_in_snapshot(cfg):
    store = seed(cfg)
    store.add_segment("seg_two",start_ms=0,end_ms=3000,text="修订后的样本量为 480。",turn_id="turn_001",revision=2)
    store.supersede_segments(["seg_one"], "seg_two")
    current = snapshot(cfg,store.session_id)
    assert [s["text"] for s in current["segments"]] == ["修订后的样本量为 480。"]
    store.db.close()


def test_spawn_runner_uses_snapshot(cfg, monkeypatch):
    from scientific_conference.service import spawn_runner, _write_config_snapshot
    store = seed(cfg)
    _write_config_snapshot(cfg, store.paths.root / "config.snapshot.yaml")
    captured = {}
    def popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return type("Process", (), {"pid":123})()
    monkeypatch.setattr("scientific_conference.service.subprocess.Popen", popen)
    assert spawn_runner(cfg, store.session_id) == 123
    cmd = captured["cmd"]
    assert cmd[cmd.index("--config")+1] == str(store.paths.root / "config.snapshot.yaml")
    store.db.close()

def test_live_start_runs_file_pipeline_and_background_notes(cfg, dashboard, meeting_wav, monkeypatch):
    import time
    from scientific_conference.service import start_session as real_start
    wav, _ = meeting_wav()
    cfg.runner.poll_wait_ms = 100
    def file_start(config, **kwargs):
        return real_start(config, source="file", wav_path=str(wav), **kwargs)
    monkeypatch.setattr("scientific_conference.dashboard.start_session", file_start)
    _, info = dashboard
    result = _request(info, "/api/start", {"title":"录音链路测试（合成音频）", "consent":True})
    sid = result["session_id"]
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        current = _request(info, "/api/snapshot?session_id="+sid)
        if current["state"] == "failed":
            pytest.fail(str(current))
        if current["state"] == "closed" and current["notes"]:
            break
        time.sleep(.2)
    assert current["state"] == "closed", current
    assert current["segments"]
    assert current["notes"]

def test_translation_disabled_does_not_autostart(cfg, monkeypatch):
    cfg.translation.enabled = False
    cfg.translation.backend = 'whisper_translate'
    server = Dashboard(cfg)
    monkeypatch.setattr('scientific_conference.dashboard.spawn_translator', lambda *a, **k: pytest.fail('translation started while disabled'))
    try:
        assert server.start_translation('unused') is None
    finally:
        server.server_close()


def test_active_old_language_cannot_be_reused(cfg):
    store = seed(cfg)
    cfg.asr.language_hints = ['en']
    cfg.asr.initial_prompt = 'new english settings'
    server = Dashboard(cfg)
    try:
        with pytest.raises(ValueError, match='different ASR settings'):
            server.start_recording(consent=True)
    finally:
        server.server_close()
        store.db.close()


def test_external_notes_recording_keeps_capture_and_translation_without_runner(cfg, monkeypatch):
    from scientific_conference.config import load_config
    from scientific_conference import service

    cfg.runner.agent = "external"
    cfg.translation.enabled = True
    cfg.translation.backend = "whisper_translate"
    monkeypatch.setattr(service, "spawn_service", lambda *a, **k: os.getpid())
    monkeypatch.setattr("scientific_conference.agents.build_agent", lambda *a, **k: pytest.fail("native notes opened a model adapter"))
    monkeypatch.setattr("scientific_conference.dashboard.spawn_runner", lambda *a, **k: pytest.fail("native notes started the legacy notes runner"))
    translation_sessions = []

    def translate(actual, sid, **kwargs):
        translation_sessions.append(sid)
        return 456

    monkeypatch.setattr("scientific_conference.dashboard.spawn_translator", translate)
    server = Dashboard(cfg)
    try:
        result = server.start_recording(title="Native notes", consent=True)
        assert result["ok"] and result["runner_pid"] == 0
        assert result["translation_pid"] == 456
        assert translation_sessions == [result["session_id"]]
        stored = Store.open(cfg.output_root, result["session_id"])
        try:
            assert json.loads(stored.session()["config_json"])["runner"]["agent"] == "external"
            snapshot_cfg = load_config(stored.paths.root / "config.snapshot.yaml", env=False)
            assert snapshot_cfg.runner.agent == "external"
            assert snapshot_cfg.asr == cfg.asr
            assert snapshot_cfg.audio == cfg.audio
            assert not (stored.paths.root / "runner.pid").exists()
            assert not stored.notes()
        finally:
            stored.db.close()
    finally:
        server.server_close()


@pytest.mark.parametrize("detached", [True, False])
def test_dashboard_detached_snapshot_keeps_effective_notes_mode(cfg, tmp_path, monkeypatch, detached):
    from scientific_conference import dashboard as module
    from scientific_conference.service import _write_config_snapshot

    cfg.runner.agent = "external"
    cfg.privacy.allow_cloud_notes = True
    config_path = tmp_path / "effective.yaml"
    _write_config_snapshot(cfg, config_path)
    monkeypatch.setenv("SC_RUNNER__AGENT", "openai_compatible")
    monkeypatch.setattr(module, "restricted_windows_process", lambda: False)
    servers = []

    class FakeServer:
        origin = "http://127.0.0.1:12345"
        token = "fixture"

        def __init__(self, actual):
            self.cfg = actual
            servers.append(self)

        def serve_forever(self):
            pass

    monkeypatch.setattr(module, "Dashboard", FakeServer)
    argv = ["dashboard", "--config", str(config_path), "--ready", str(tmp_path / "ready.json")]
    if detached:
        argv += ["--instance", "fixture"]
    monkeypatch.setattr("sys.argv", argv)
    module.main()
    assert servers[0].cfg.runner.agent == ("external" if detached else "openai_compatible")


@pytest.mark.parametrize("asset", ["markdown.js", "workspace.js", "meeting-template.js"])
def test_dashboard_serves_workspace_script_assets(dashboard, asset):
    _, info = dashboard
    with build_opener(ProxyHandler({})).open(info["origin"] + "/" + asset) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "text/javascript"
        assert response.read() == (Path(__file__).resolve().parents[1] / "src" / "scientific_conference" / asset).read_bytes()


def test_panel_csp_allows_desktop_application_shell(cfg, dashboard, monkeypatch):
    """DSH NEXT loads its window from the registered ``dsh-app://app`` scheme.

    No loopback source can match that ancestor, and without it every panel iframe
    was blocked ("Framing ... violates frame-ancestors") so Meetings stayed blank.
    """
    from scientific_conference.dashboard import frame_ancestors

    sources = frame_ancestors(cfg)
    assert "'self'" in sources
    assert "http://127.0.0.1:*" in sources
    assert "dsh-app:" in sources
    monkeypatch.setenv("DSH_FRAME_ANCESTOR", "dsh-shell://app")
    assert "dsh-shell://app" in frame_ancestors(cfg)

    _, info = dashboard
    with build_opener(ProxyHandler({})).open(info["origin"] + "/") as response:
        policy = response.headers["Content-Security-Policy"]
    ancestors = policy.split("frame-ancestors", 1)[1]
    assert "dsh-app:" in ancestors and "'self'" in ancestors
    assert "script-src 'self'" in policy


def test_external_links_open_in_the_default_browser(cfg, dashboard, monkeypatch):
    """Sandboxed panel iframes cannot open popups, so embedded links ask this service."""
    import webbrowser

    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *args, **kwargs: opened.append(url) or True)
    _, info = dashboard
    target = "https://doi.org/10.1016/j.cell.2018.10.021"
    result = _request(info, "/api/open-external", {"url": target})
    assert result == {"ok": True, "url": target}
    assert opened == [target]

    rejected = ["javascript:alert(1)", "file:///C:/Windows/win.ini", "http://127.0.0.1:61040/api/info",
                "http://localhost/panel", "https://user:pw@example.org/x", "https://example.org/a b", "", 42]
    for bad in rejected:
        with pytest.raises(HTTPError) as error:
            _request(info, "/api/open-external", {"url": bad})
        assert error.value.code == 400
    with pytest.raises(HTTPError) as error:
        _request(dict(info, token="wrong"), "/api/open-external", {"url": target})
    assert error.value.code == 403
    assert opened == [target]

def test_qwen_start_persists_backend_specific_settings(cfg, dashboard, monkeypatch):
    import scientific_conference.dashboard as module
    server, info = dashboard
    calls = []
    monkeypatch.setattr(module.QwenBackend, "prepare", lambda self: calls.append(self.settings))
    def fake_start(settings, **kwargs):
        store = Store.create(settings.output_root, "sess_qwen_api", settings, consent=True)
        store.db.close()
        return {"session_id": "sess_qwen_api"}
    monkeypatch.setattr(module, "start_session", fake_start)
    monkeypatch.setattr(module, "spawn_runner", lambda *args: 0)
    result = _request(info, "/api/start", {"consent": True, "asr_backend": "qwen3_asr",
                      "language": "zh", "qwen_language": "auto", "qwen_chunk_seconds": 1.5})
    assert result["session_id"] == "sess_qwen_api"
    assert len(calls) == 1 and calls[0].chunk_seconds == 1.5
    assert calls[0].language == "auto"
    store = module.checked_store(cfg, result["session_id"])
    try:
        row = store.session()
        assert row["asr_backend"] == "qwen3_asr"
        assert json.loads(row["config_json"])["notes"]["language"] == "zh"
        assert cfg.asr.backend == "mock"
    finally:
        store.db.close()
    rendered = _request(info, f"/api/snapshot?session_id={result['session_id']}")
    assert rendered["asr_backend"] == "qwen3_asr"
    assert rendered["segments"] == []


def test_switch_route_only_queues_supported_models_during_recording(cfg, dashboard):
    _, info = dashboard
    cfg.asr.backend = "faster_whisper"
    store = seed(cfg)
    try:
        with pytest.raises(HTTPError) as bad:
            _request(info, "/api/asr/switch", {"session_id": store.session_id, "backend": "azure"})
        assert bad.value.code == 400
        result = _request(info, "/api/asr/switch", {"session_id": store.session_id, "backend": "qwen3_asr"})
        assert result["queued"]
        [control] = store.pop_control()
        assert control["command"] == "asr_switch"
        assert json.loads(control["args"]) == {"backend": "qwen3_asr"}
        store.set_state("closed")
        with pytest.raises(HTTPError) as inactive:
            _request(info, "/api/asr/switch", {"session_id": store.session_id, "backend": "qwen3_asr"})
        assert inactive.value.code == 400
    finally:
        store.db.close()

def test_public_dashboard_omits_speaker_controls_and_routes(cfg, dashboard):
    _, info = dashboard
    store = seed(cfg)
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(info['origin'] + '/', timeout=3) as response:
            html = response.read().decode('utf-8')
        assert 'id="transcript"' in html and 'id="asr-draft"' in html
        assert all(f'id="{id}"' not in html for id in ('diarize-run', 'speaker-merge', 'speaker-bulk', 'transcript-speaker', 'presenter-tab'))
        for route in ('/api/speakers/run', '/api/speakers/rename', '/api/meeting/speaker-profile'):
            with pytest.raises(HTTPError) as rejected:
                _request(info, route, {'session_id': store.session_id})
            assert rejected.value.code == 404
    finally:
        store.db.close()
