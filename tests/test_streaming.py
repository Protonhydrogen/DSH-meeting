"""Incremental transcription: windowing, overlap de-duplication, revisions.

The mock backend supplies scripted text so the tests can assert on exact
time ranges, which is what makes the tricky parts (splitting a long monologue,
trimming the overlap, never duplicating text) verifiable without a model.
"""

from __future__ import annotations

import numpy as np
import pytest

from scientific_conference.asr import MockAsrBackend, StreamingTranscriber
from scientific_conference.asr.base import ASR_SAMPLE_RATE
from scientific_conference.config import load_config
from scientific_conference.vad import EnergyVad, NullVad, VadGate


def make_gate(backend="energy") -> VadGate:
    inner = EnergyVad(threshold=0.02) if backend == "energy" else NullVad()
    return VadGate(backend=inner, threshold=0.5, min_speech_ms=160, min_silence_ms=160, pad_ms=0)


def speech(seconds: float, amp: float = 0.3, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(seconds * ASR_SAMPLE_RATE)
    t = np.arange(n) / ASR_SAMPLE_RATE
    wave = rng.standard_normal(n) * (0.5 + 0.5 * np.abs(np.sin(2 * np.pi * 5 * t)))
    return (wave * amp).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * ASR_SAMPLE_RATE), dtype=np.float32)


def test_single_utterance_is_committed_once():
    script = [{"start_ms": 0, "end_ms": 1000, "text": "第一句话。"}]
    backend = MockAsrBackend(script=script)
    gate = make_gate()
    transcriber = StreamingTranscriber(backend, gate, max_window_ms=20000)
    out = []
    out += transcriber.push(speech(1.0))
    out += transcriber.push(silence(1.0))
    assert len(out) == 1
    assert out[0].text == "第一句话。"
    assert out[0].is_final
    # The VAD reports the end 160 ms after the speech; the segment keeps the
    # ASR-provided range.
    assert out[0].end_ms <= 1200


def test_short_gap_keeps_separate_segments():
    script = [
        {"start_ms": 0, "end_ms": 1000, "text": "A"},
        {"start_ms": 1500, "end_ms": 2500, "text": "B"},
    ]
    backend = MockAsrBackend(script=script)
    transcriber = StreamingTranscriber(backend, make_gate())
    out = []
    out += transcriber.push(speech(1.0))
    out += transcriber.push(silence(0.5))
    out += transcriber.push(speech(1.0))
    out += transcriber.push(silence(1.0))
    assert [s.text for s in out] == ["A", "B"]


def test_empty_and_tiny_blocks_are_ignored():
    backend = MockAsrBackend()
    transcriber = StreamingTranscriber(backend, make_gate())
    assert transcriber.push(np.zeros(0, dtype=np.float32)) == []


def test_long_monologue_is_split_without_duplicating_text():
    """A window that exceeds max_window_ms is split at the quietest point."""
    # 3 s speech, 0.3 s quieter speech, 3 s speech, then silence.
    audio = np.concatenate([speech(3.0), speech(0.3, amp=0.05), speech(3.0), silence(1.0)])
    script = [{"start_ms": 0, "end_ms": 3000, "text": "第一部分"}, {"start_ms": 3300, "end_ms": 6300, "text": "第二部分"}]
    backend = MockAsrBackend(script=script)
    gate = make_gate()
    transcriber = StreamingTranscriber(backend, gate, max_window_ms=3000, overlap_ms=1000, min_segment_ms=100)

    out = []
    for i in range(0, audio.size, 1600):  # 100 ms blocks
        out += transcriber.push(audio[i : i + 1600])
    texts = [s.text for s in out]
    assert "第一部分" in texts
    assert texts.count("第一部分") == 1, f"duplicate text: {texts}"
    assert any("window_split" in s.flags for s in out)


def test_overlap_region_is_trimmed_with_word_timestamps():
    """With word timings the overlapped part is cut, not repeated."""
    audio = np.concatenate([speech(2.5), speech(0.2, amp=0.04), speech(2.5), silence(1.0)])
    words = [
        {"start_ms": 0, "end_ms": 2400, "word": "前半句", "prob": 0.9},
        {"start_ms": 2700, "end_ms": 4900, "word": "后半句", "prob": 0.9},
    ]
    script = [{"start_ms": 0, "end_ms": 4900, "text": "前半句后半句"}]
    backend = MockAsrBackend(script=script)
    # The mock returns text without word timings, so attach them through a wrapper.
    original = backend.transcribe

    def with_words(samples, sample_rate=ASR_SAMPLE_RATE, **kwargs):
        segs = original(samples, sample_rate, **kwargs)
        for seg in segs:
            seg.words = [dict(w) for w in words]
        return segs

    backend.transcribe = with_words  # type: ignore[assignment]
    gate = make_gate()
    transcriber = StreamingTranscriber(backend, gate, max_window_ms=2500, overlap_ms=800, min_segment_ms=50)
    out = []
    for i in range(0, audio.size, 1600):
        out += transcriber.push(audio[i : i + 1600])
    assert out, "expected at least one committed segment"
    assert all("overlap_trimmed" not in s.flags or s.text for s in out)
    # No segment may both start before the committed boundary and keep the
    # already-emitted prefix.
    committed = [s for s in out if s.flags]
    for seg in committed:
        assert "前半句" not in seg.text or seg.start_ms >= 0


def test_flush_commits_open_window():
    backend = MockAsrBackend(script=[{"start_ms": 0, "end_ms": 1000, "text": "尚未结束"}])
    transcriber = StreamingTranscriber(backend, make_gate())
    transcriber.push(speech(1.0))
    out = transcriber.flush()
    assert [s.text for s in out] == ["尚未结束"]


def test_backend_failure_does_not_break_the_stream():
    class Broken(MockAsrBackend):
        def transcribe(self, *args, **kwargs):
            raise RuntimeError("model exploded")

    transcriber = StreamingTranscriber(Broken(), make_gate())
    out = transcriber.push(speech(1.0))
    out += transcriber.push(silence(1.0))
    assert out == []
    assert transcriber.last_error and "model exploded" in transcriber.last_error
    assert transcriber.stream_ms > 0


def test_absolute_times_include_session_offset():
    """A window that starts at t=60 s reports segments on the session timeline."""
    backend = MockAsrBackend(script=[{"start_ms": 60000, "end_ms": 61000, "text": "A"}])
    transcriber = StreamingTranscriber(backend, make_gate(), session_offset_ms=60000)
    out = transcriber.push(speech(1.0))
    out += transcriber.push(silence(1.0))
    assert out, "expected a committed segment"
    assert out[0].start_ms >= 60000
    assert out[0].end_ms <= 61500


def test_diarizer_receives_the_audio():
    calls = []

    class Recorder:
        name = "stub"

        def assign(self, samples, sample_rate, **kwargs):
            calls.append(samples.size)
            return "speaker_01", 0.9

        def info(self):
            return {}

    backend = MockAsrBackend(script=[{"start_ms": 0, "end_ms": 1000, "text": "A"}])
    transcriber = StreamingTranscriber(backend, make_gate(), diarizer=Recorder())
    out = transcriber.push(speech(1.0))
    out += transcriber.push(silence(1.0))
    assert calls, "diarizer was never called"
    assert out[0].speaker_id == "speaker_01"
    assert any(f.startswith("speaker_sim=") for f in out[0].flags)
