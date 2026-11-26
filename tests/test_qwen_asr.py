from __future__ import annotations

import numpy as np

from scientific_conference.asr.qwen import QWEN_MODEL, QwenTranscriber
from scientific_conference.vad import VadEvent


class Gate:
    def __init__(self, events):
        self.events = iter(events)

    def push(self, audio):
        return next(self.events, [])

    def flush(self):
        return []


class Engine:
    def __init__(self):
        self.windows = []
        self.cancelled = False

    def begin(self):
        self.windows.append([])

    def push(self, audio):
        self.windows[-1].extend(audio.tolist())
        return {"text": "草稿", "language": "Chinese"}

    def finish(self):
        return {"text": "定稿", "language": "Chinese"}

    def cancel(self):
        self.cancelled = True


def audio(ms, value=0.25):
    return np.full(ms * 16, value, dtype=np.float32)


def test_qwen_draft_is_replaced_by_final_with_an_audio_range():
    engine = Engine()
    gate = Gate([[], [VadEvent("start", 20, 1)], [VadEvent("end", 100, 1)]])
    stream = QwenTranscriber(engine, gate, session_offset_ms=500, max_window_ms=800)
    assert stream.push(audio(40)) == []
    assert stream.push(audio(40)) == []
    assert stream.partial == "草稿"
    assert stream.span_open
    [final] = stream.push(audio(40))
    assert (final.start_ms, final.end_ms) == (520, 600)
    assert final.text == "定稿"
    assert final.is_final and final.words == []
    assert "timing:audio_range" in final.flags
    assert stream.partial == ""
    assert len(engine.windows[0]) == 80 * 16


def test_qwen_long_utterance_and_stop_do_not_duplicate_or_drop_samples():
    engine = Engine()
    gate = Gate([[VadEvent("start", 0, 1)]])
    stream = QwenTranscriber(engine, gate, max_window_ms=100)
    [first] = stream.push(audio(140))
    [tail] = stream.flush()
    assert [(s.start_ms, s.end_ms) for s in (first, tail)] == [(0, 100), (100, 140)]
    assert [len(window) for window in engine.windows] == [1600, 640]
    assert stream.flush() == []


def test_qwen_holds_delayed_vad_end_and_flushes_the_pending_tail():
    engine = Engine()
    gate = Gate([[VadEvent("start", 0, 1)], [], [VadEvent("end", 60, 1)],
                 [VadEvent("start", 120, 1)]])
    stream = QwenTranscriber(engine, gate, end_hold_ms=40, max_window_ms=800)
    stream.push(audio(40))
    stream.push(audio(40))
    [final] = stream.push(audio(40))
    assert (final.start_ms, final.end_ms) == (0, 60)
    assert len(engine.windows[0]) == 60 * 16
    assert stream.push(audio(20)) == []
    [tail] = stream.flush()
    assert (tail.start_ms, tail.end_ms) == (120, 140)
    assert len(engine.windows[1]) == 20 * 16


def test_qwen_engine_error_keeps_audio_clock_and_recovers_next_window():
    class FailingEngine(Engine):
        def push(self, audio):
            if len(self.windows) == 1:
                raise OSError("runtime disconnected")
            return super().push(audio)

    engine = FailingEngine()
    gate = Gate([
        [VadEvent("start", 0, 1)],
        [VadEvent("end", 50, 1)],
        [VadEvent("start", 100, 1), VadEvent("end", 140, 1)],
    ])
    stream = QwenTranscriber(engine, gate, max_window_ms=800)
    assert stream.push(audio(40)) == []
    assert "runtime disconnected" in stream.last_error
    assert engine.cancelled
    assert stream.push(audio(60)) == []
    [final] = stream.push(audio(40))
    assert (final.start_ms, final.end_ms, stream.stream_ms) == (100, 140, 140)
    assert stream.last_error is None


def test_config_keeps_whisper_model_separate_from_qwen(cfg):
    from scientific_conference.asr import select_recording_backend
    cfg.asr.model = "turbo"
    select_recording_backend(cfg, "qwen3_asr")
    assert cfg.asr.backend == "qwen3_asr"
    assert cfg.asr.model == "turbo"
    assert cfg.asr.qwen.model == QWEN_MODEL
    select_recording_backend(cfg, "faster_whisper")
    assert cfg.asr.model == "turbo"




def test_recorder_switch_waits_for_boundary_and_preserves_the_session_clock(cfg, monkeypatch):
    from scientific_conference import recorder as module
    from scientific_conference.store import Store

    cfg.asr.backend = "faster_whisper"
    store = Store.create(cfg.output_root, "sess_switch_test", cfg, consent=True)
    recorder = module.Recorder(cfg, store, module.RecorderConfig(store.session_id, cfg.output_root))
    old = type("Whisper", (), {"name": "faster_whisper", "close": lambda self: None})()
    old_stream = type("Stream", (), {"span_open": True, "stream_ms": 1250.0})()
    old_stream.gate = type("QuietGate", (), {"quiet_boundary": False,
                                              "unclassified_audio": audio(1)})()
    old_stream.flush = lambda: []
    recorder._backend = old
    recorder._transcriber = old_stream

    class Qwen(Engine):
        name = "qwen3_asr"

        def info(self):
            return {"backend": self.name, "model": QWEN_MODEL}

    target = Qwen()
    monkeypatch.setattr(module, "build_backend", lambda settings: target)
    control = store.push_control("asr_switch", {"backend": target.name})
    recorder._apply_control()
    recorder._pending_asr[2].result(timeout=3)
    recorder._apply_pending_asr()
    assert recorder._backend is old

    old_stream.span_open = False
    old_stream.gate.quiet_boundary = True
    recorder._apply_pending_asr()
    assert recorder._backend is target
    assert recorder._transcriber.stream_ms == 1250
    result = store.db.query_one("SELECT state FROM control WHERE id=?", (control,))
    assert result["state"] == "applied"
    [event] = recorder.events.poll(store.session_id, "test")["events"]
    assert event["type"] == "asr.switched" and event["effective_ms"] == 1249
    store.db.close()


def test_unavailable_qwen_switch_leaves_current_engine_active(cfg, monkeypatch):
    from scientific_conference import recorder as module
    from scientific_conference.asr.base import AsrError
    from scientific_conference.store import Store

    store = Store.create(cfg.output_root, "sess_switch_error", cfg, consent=True)
    recorder = module.Recorder(cfg, store, module.RecorderConfig(store.session_id, cfg.output_root))
    old = type("Whisper", (), {"name": "faster_whisper"})()
    recorder._backend = old

    def unavailable(settings):
        raise AsrError("local service unavailable")

    monkeypatch.setattr(module, "build_backend", unavailable)
    control = store.push_control("asr_switch", {"backend": "qwen3_asr"})
    recorder._apply_control()
    from concurrent.futures import wait
    wait([recorder._pending_asr[2]], timeout=3)
    recorder._apply_pending_asr()
    assert recorder._backend is old
    assert "local service unavailable" in recorder._asr_switch["error"]
    assert store.db.query_one("SELECT state FROM control WHERE id=?", (control,))["state"] == "rejected"
    store.db.close()




def test_sidecar_protocol_and_windows_client_interoperate(monkeypatch):
    import importlib.util
    import threading
    from http.server import ThreadingHTTPServer
    from pathlib import Path
    from types import SimpleNamespace
    from urllib.error import HTTPError
    from urllib.request import ProxyHandler, Request, build_opener

    from scientific_conference.asr.qwen import QwenBackend
    from scientific_conference.config import QwenAsrConfig

    script = Path(__file__).resolve().parents[1] / "scripts" / "qwen_asr_service.py"
    spec = importlib.util.spec_from_file_location("qwen_asr_service", script)
    service = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service)

    class Model:
        def init_streaming_state(self, *, language, chunk_size_sec):
            assert (language, chunk_size_sec) == ("Chinese", 2.0)
            return SimpleNamespace(text="", language=language, samples=0)

        def streaming_transcribe(self, samples, state):
            state.samples += samples.size
            state.text = "模型草稿"

        def finish_streaming_transcribe(self, state):
            assert state.samples == 4800
            state.text = "正式转录"

    with ThreadingHTTPServer(("127.0.0.1", 0), service.Handler) as server:
        server.streams = service.Streams(Model())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            backend = QwenBackend(QwenAsrConfig(port=server.server_port), language_hints=["zh"])
            backend.prepare()
            backend.begin()
            assert backend.push(audio(200))["text"] == "模型草稿"
            assert backend.push(audio(100)) is None
            assert backend.finish()["text"] == "正式转录"
            request = Request(f"http://127.0.0.1:{server.server_port}/health",
                              headers={"Origin": "https://example.com"})
            import pytest
            with pytest.raises(HTTPError) as error:
                build_opener(ProxyHandler({})).open(request, timeout=3)
            assert error.value.code == 403
        finally:
            server.shutdown()
            thread.join(3)




def test_qwen_backpressure_keeps_source_clock(cfg, monkeypatch):
    import queue
    from types import SimpleNamespace
    from scientific_conference import recorder as module
    from scientific_conference.store import Store

    store = Store.create(cfg.output_root, "sess_qwen_backpressure", cfg, consent=True)
    recorder = module.Recorder(cfg, store, module.RecorderConfig(store.session_id, cfg.output_root))
    recorder._backend = SimpleNamespace(name="qwen3_asr")
    recorder.asr_q = queue.Queue(maxsize=1)
    recorder._queue_asr_audio(0, 30, audio(30))
    recorder._queue_asr_audio(30, 60, audio(30))
    assert recorder._totals["dropped"] == 1
    assert recorder.asr_q.get_nowait()[:2] == (0, 30)

    recorder._transcriber = SimpleNamespace(stream_ms=29.375, flush=lambda: [])
    starts = []
    def make_transcriber(backend, offset):
        starts.append(offset)
        return SimpleNamespace(stream_ms=offset)
    monkeypatch.setattr(recorder, "_new_transcriber", make_transcriber)
    recorder._restore_asr_clock(30)
    assert not starts
    recorder._restore_asr_clock(60)
    assert starts == [60]
    [gap] = recorder.events.poll(store.session_id, "test")["events"]
    assert (gap["type"], gap["start_ms"], gap["end_ms"]) == ("asr.audio_gap", 29, 60)
    store.db.close()
