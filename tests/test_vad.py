"""Voice activity detection gate and the energy fallback."""

from __future__ import annotations

import numpy as np

from scientific_conference.vad import EnergyVad, NullVad, VadGate, load_vad


def noise(seconds: float, amp: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * 16000)) * amp).astype(np.float32)


def test_energy_gate_detects_a_burst():
    gate = VadGate(backend=EnergyVad(threshold=0.02), threshold=0.5, min_speech_ms=160, min_silence_ms=160, pad_ms=0)
    assert gate.push(np.zeros(16000, dtype=np.float32)) == []
    assert [e.kind for e in gate.push(noise(1.0, 0.3))] == ["start"]
    assert [e.kind for e in gate.push(np.zeros(16000, dtype=np.float32))] == ["end"]
    assert gate.spans and gate.spans[0].duration_ms > 500


def test_gate_ignores_a_too_short_burst():
    gate = VadGate(backend=EnergyVad(threshold=0.02), threshold=0.5, min_speech_ms=500, min_silence_ms=160, pad_ms=0)
    assert gate.push(noise(0.1, 0.5)) == []
    assert gate.push(np.zeros(16000, dtype=np.float32)) == []


def test_gate_merges_a_short_pause():
    gate = VadGate(backend=EnergyVad(threshold=0.02), threshold=0.5, min_speech_ms=160, min_silence_ms=400, pad_ms=0)
    events = gate.push(noise(0.6, 0.3))
    events += gate.push(np.zeros(int(0.2 * 16000), dtype=np.float32))
    events += gate.push(noise(0.6, 0.3))
    events += gate.push(np.zeros(16000, dtype=np.float32))
    assert [e.kind for e in events] == ["start", "end"]
    assert len(gate.spans) == 1


def test_null_gate_treats_everything_as_speech():
    gate = VadGate(backend=NullVad(), threshold=0.5, min_speech_ms=160, min_silence_ms=1000, pad_ms=0)
    assert [e.kind for e in gate.push(np.zeros(16000, dtype=np.float32))] == ["start"]
    # ``flush`` closes the span that is still open at the end of the stream.
    assert [e.kind for e in gate.flush()] == ["end"]


def test_flush_closes_an_open_span():
    gate = VadGate(backend=EnergyVad(threshold=0.02), threshold=0.5, min_speech_ms=160, min_silence_ms=160, pad_ms=0)
    gate.push(noise(1.0, 0.3))
    events = gate.flush()
    assert [e.kind for e in events] == ["end"]
    assert gate.spans


def test_padding_extends_the_span():
    gate = VadGate(backend=EnergyVad(threshold=0.02), threshold=0.5, min_speech_ms=160, min_silence_ms=160, pad_ms=200)
    events = gate.push(noise(1.0, 0.3))
    start = next(e for e in events if e.kind == "start")
    assert start.time_ms == 0  # clamped at zero, not negative


def test_load_vad_falls_back_to_energy(tmp_path):
    class Cfg:
        class vad:
            backend = "silero"
            model_path = str(tmp_path / "missing.onnx")
            threshold = 0.5
            min_speech_ms = 250
            min_silence_ms = 400
            pad_ms = 200
            energy_threshold = 0.012

    gate = load_vad(Cfg)
    assert gate.info()["backend"] == "energy"
    assert gate.fallback_notes and "silero" in gate.fallback_notes[0]


def test_load_vad_none_backend(tmp_path):
    class Cfg:
        class vad:
            backend = "none"
            model_path = ""
            threshold = 0.5
            min_speech_ms = 250
            min_silence_ms = 400
            pad_ms = 200
            energy_threshold = 0.012

    gate = load_vad(Cfg)
    assert gate.info()["backend"] == "none"
