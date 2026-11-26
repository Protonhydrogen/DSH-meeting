"""Whisper repetition loops: decode retry, marking, and the review guard.

The synthetic fixture repeats a short phrase within a 1.2 second window.
This represents a decoder repetition loop with an implausible text rate.

Three layers are asserted here:

* the backend must hand faster-whisper a *temperature list*, because upstream
  only retries a bad decode when the list has more than one entry;
* a loop that survives every retry must be *marked* on the segment, with the
  decoded text left byte-for-byte intact;
* the local review guard must recognise a repeated multi-word phrase that the
  segment's own duration proves impossible, and skip it instead of asking a
  language model to "correct" it.

Everything here is offline and hardware-free: the faster-whisper model is
stubbed, so no model file is loaded and no audio device is touched.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import numpy as np
import pytest

from scientific_conference.asr import MockAsrBackend, StreamingTranscriber
from scientific_conference.asr.base import ASR_SAMPLE_RATE, AsrSegment
from scientific_conference.correction import (
    REPETITION_MARKER,
    MAX_PLAUSIBLE_CHARS_PER_SECOND,
    clean_repetitions,
    repetition_loop,
)
from scientific_conference.store import Store
from scientific_conference.vad import EnergyVad, VadGate

#: Synthetic repetition text (766 characters).
PROD_LOOP = ("the demo can begin now, " * 31) + "the demo can begin now"
#: The production segment's own timing: 1100 ms.
PROD_START_MS = 3682040
PROD_END_MS = 3683140


# --------------------------------------------------------------------------
# decode configuration
# --------------------------------------------------------------------------
def test_temperature_ladder_covers_whispers_own_defaults():
    from scientific_conference.asr.faster_whisper_backend import FasterWhisperBackend

    assert FasterWhisperBackend._temperature_ladder(0.0, True) == [
        0.0,
        0.2,
        0.4,
        0.6,
        0.8,
        1.0,
    ]
    # The configured temperature stays the first attempt; only higher rungs are
    # appended, so a deliberately warmer start is not cooled back down.
    assert FasterWhisperBackend._temperature_ladder(0.4, True) == [0.4, 0.6, 0.8, 1.0]
    # The rollback switch reproduces the old single-temperature behaviour.
    assert FasterWhisperBackend._temperature_ladder(0.0, False) == [0.0]


def test_temperature_ladder_matches_the_installed_faster_whisper_default():
    """If upstream changes its ladder, this test is the place that notices."""
    faster_whisper = pytest.importorskip("faster_whisper")
    import inspect

    from scientific_conference.asr.faster_whisper_backend import FasterWhisperBackend

    default = inspect.signature(faster_whisper.WhisperModel.transcribe).parameters[
        "temperature"
    ].default
    assert tuple(default) == FasterWhisperBackend.FALLBACK_TEMPERATURES


def _install_fake_faster_whisper(monkeypatch, segments):
    """Replace ``faster_whisper`` with a recorder that returns fixed segments."""
    created: list[SimpleNamespace] = []

    class FakeWhisperModel:
        def __init__(self, name, **kwargs):
            self.name = name
            self.kwargs = kwargs
            self.calls = []
            created.append(self)

        def transcribe(self, audio, **kwargs):
            self.calls.append(kwargs)
            info = SimpleNamespace(
                language="en",
                language_probability=1.0,
                duration=float(np.asarray(audio).size) / ASR_SAMPLE_RATE,
            )
            return iter([SimpleNamespace(**dict(s)) for s in segments]), info

    module = SimpleNamespace(WhisperModel=FakeWhisperModel, utils=SimpleNamespace(_MODELS={}))
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", module.utils)
    monkeypatch.setattr(
        "scientific_conference.asr.faster_whisper_backend.resolve_local_model",
        lambda *a, **k: None,
    )
    return created


def _fake_segment(**overrides):
    segment = {
        "text": " a normal sentence. ",
        "start": 0.0,
        "end": 1.0,
        "words": [],
        "temperature": 0.0,
        "no_speech_prob": 0.01,
        "compression_ratio": 1.2,
        "avg_logprob": -0.2,
    }
    segment.update(overrides)
    return segment


def test_backend_passes_a_temperature_list_and_the_thresholds(monkeypatch, cfg):
    from scientific_conference.asr.faster_whisper_backend import FasterWhisperBackend

    created = _install_fake_faster_whisper(monkeypatch, [_fake_segment()])
    backend = FasterWhisperBackend(
        "turbo",
        device="cpu",
        compute_type="int8",
        temperature=0.0,
        temperature_fallback=True,
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
    )
    backend.transcribe(np.zeros(ASR_SAMPLE_RATE, dtype=np.float32))

    kwargs = created[0].calls[-1]
    # A list, not a float: this is what turns the retry on.
    assert kwargs["temperature"] == [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    assert kwargs["compression_ratio_threshold"] == 2.4
    assert kwargs["log_prob_threshold"] == -1.0
    assert kwargs["no_speech_threshold"] == 0.6
    assert kwargs["condition_on_previous_text"] is False
    assert backend.info()["temperatures"] == [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]


def test_backend_can_still_disable_the_fallback(monkeypatch, cfg):
    from scientific_conference.asr.faster_whisper_backend import FasterWhisperBackend

    created = _install_fake_faster_whisper(monkeypatch, [_fake_segment()])
    backend = FasterWhisperBackend(
        "turbo", device="cpu", compute_type="int8", temperature=0.0, temperature_fallback=False
    )
    backend.transcribe(np.zeros(ASR_SAMPLE_RATE, dtype=np.float32))
    assert created[0].calls[-1]["temperature"] == [0.0]


def test_backend_marks_a_loop_that_survived_every_retry(monkeypatch, cfg):
    from scientific_conference.asr.faster_whisper_backend import FasterWhisperBackend

    looped = _fake_segment(
        text=PROD_LOOP,
        start=0.0,
        end=1.1,
        temperature=1.0,
        compression_ratio=20.5,
    )
    _install_fake_faster_whisper(monkeypatch, [looped])
    backend = FasterWhisperBackend("turbo", device="cpu", compute_type="int8")
    segments = backend.transcribe(np.zeros(ASR_SAMPLE_RATE, dtype=np.float32))

    assert segments[0].text == PROD_LOOP, "the decoded text must not be rewritten"
    assert segments[0].compression_ratio == 20.5
    flags = backend.anomaly_flags(segments[0])
    assert "repetition_suspected" in flags
    assert "compression_ratio=20.50" in flags
    assert "temperature=1.0" in flags  # which rung of the ladder finally produced it
    assert backend.info()["windows_decoded"] == 1
    assert backend.info()["windows_with_fallback"] == 1
    assert backend.info()["repetition_segments"] == 1


def test_routine_retry_is_counted_but_not_flagged(monkeypatch, cfg):
    """A fallback that fixed the decode must not look like an anomaly."""
    from scientific_conference.asr.faster_whisper_backend import FasterWhisperBackend

    _install_fake_faster_whisper(monkeypatch, [_fake_segment(temperature=0.6, compression_ratio=1.9)])
    backend = FasterWhisperBackend("turbo", device="cpu", compute_type="int8")
    segments = backend.transcribe(np.zeros(ASR_SAMPLE_RATE, dtype=np.float32))

    assert backend.anomaly_flags(segments[0]) == []
    assert backend.info()["windows_with_fallback"] == 1
    assert backend.info()["repetition_segments"] == 0


def test_clean_decode_is_not_flagged(monkeypatch, cfg):
    from scientific_conference.asr.faster_whisper_backend import FasterWhisperBackend

    _install_fake_faster_whisper(monkeypatch, [_fake_segment()])
    backend = FasterWhisperBackend("turbo", device="cpu", compute_type="int8")
    segments = backend.transcribe(np.zeros(ASR_SAMPLE_RATE, dtype=np.float32))
    assert backend.anomaly_flags(segments[0]) == []


# --------------------------------------------------------------------------
# the streaming layer only marks, never rewrites
# --------------------------------------------------------------------------
def _gate() -> VadGate:
    return VadGate(backend=EnergyVad(threshold=0.02), threshold=0.5, min_speech_ms=160,
                   min_silence_ms=160, pad_ms=0)


def _speech(seconds: float, amp: float = 0.3) -> np.ndarray:
    rng = np.random.default_rng(0)
    n = int(seconds * ASR_SAMPLE_RATE)
    t = np.arange(n) / ASR_SAMPLE_RATE
    wave = rng.standard_normal(n) * (0.5 + 0.5 * np.abs(np.sin(2 * np.pi * 5 * t)))
    return (wave * amp).astype(np.float32)


def test_streaming_marks_the_loop_and_keeps_the_text():
    class LoopBackend(MockAsrBackend):
        def transcribe(self, samples, sample_rate=ASR_SAMPLE_RATE, **kwargs):
            segments = super().transcribe(samples, sample_rate, **kwargs)
            for segment in segments:
                segment.compression_ratio = 20.5
            return segments

        def anomaly_flags(self, segment):
            return ["repetition_suspected", f"compression_ratio={segment.compression_ratio:.2f}"]

    backend = LoopBackend(script=[{"start_ms": 0, "end_ms": 1000, "text": PROD_LOOP}])
    transcriber = StreamingTranscriber(backend, _gate(), max_window_ms=20000)
    out = transcriber.push(_speech(1.0)) + transcriber.push(np.zeros(ASR_SAMPLE_RATE, dtype=np.float32))

    assert out, "expected a committed segment"
    segment = out[0]
    assert segment.text == PROD_LOOP, "marking must not alter the decoded text"
    assert "repetition_suspected" in segment.flags
    assert "compression_ratio=20.50" in segment.flags


# --------------------------------------------------------------------------
# the local review guard
# --------------------------------------------------------------------------
def test_production_loop_is_recognised():
    span = repetition_loop(PROD_LOOP, start_ms=PROD_START_MS, end_ms=PROD_END_MS)
    assert span is not None, "the synthetic 766-character loop must be detected"
    start, first_end, end = span
    assert PROD_LOOP[start:first_end].strip(" ,") == "the demo can begin now"
    assert end - start > 700


def test_the_same_text_over_a_long_recording_is_not_a_loop():
    """A phrase repeated across minutes is speech, not a decoder loop."""
    assert repetition_loop(PROD_LOOP, start_ms=0, end_ms=120_000) is None


def test_normal_emphasis_is_left_alone():
    assert repetition_loop("No, no, no. The pH is 7.", start_ms=0, end_ms=2000) is None
    assert repetition_loop("Very good, very good, very good.", start_ms=0, end_ms=3000) is None
    # Three repetitions of a short phrase in three seconds is ordinary speech.
    assert repetition_loop("that's right, that's right, that's right", start_ms=0, end_ms=3000) is None


def test_without_timings_only_a_long_repeated_phrase_qualifies():
    long_loop = "this is a test " * 12
    assert repetition_loop(long_loop) is not None
    assert repetition_loop("yes yes yes yes") is None


def test_clean_repetitions_keeps_one_occurrence_and_the_raw_text(cfg):
    store = Store.create(cfg.output_root, "phrase_loop_test", cfg, consent=True)
    store.add_segment("loop", start_ms=PROD_START_MS, end_ms=PROD_END_MS, text=PROD_LOOP)
    store.add_segment("normal", start_ms=PROD_END_MS + 1000, end_ms=PROD_END_MS + 3000,
                      text="No, no, no. The pH is 7.")

    clean_repetitions(store, store.segments())
    result = store.segments()

    assert result[0]["raw_text"] == PROD_LOOP
    assert result[0]["text"].count("the demo can begin now") == 1
    assert REPETITION_MARKER in result[0]["text"]
    assert len(result[0]["text"]) < 120, result[0]["text"]
    # The untouched decode is preserved as evidence, tagged with the local guard.
    edits = store.db.query("SELECT * FROM transcript_edits WHERE segment_id='loop'")
    assert edits and edits[0]["original"] == PROD_LOOP
    assert edits[0]["model"] == "local-repetition-guard"
    # Ordinary speech is untouched.
    assert result[1]["text"] == "No, no, no. The pH is 7."
    assert "raw_text" not in result[1]
    store.db.close()


def test_loop_guard_does_not_fire_on_a_normal_long_segment(cfg):
    store = Store.create(cfg.output_root, "no_false_positive", cfg, consent=True)
    text = (
        "So the first thing we do is take the library and grow it in E. coli, "
        "then we pick single colonies and move them into the next plate."
    )
    store.add_segment("normal", start_ms=0, end_ms=12000, text=text)
    clean_repetitions(store, store.segments())
    assert store.segments()[0]["text"] == text
    assert store.db.query("SELECT * FROM transcript_edits") == []
    store.db.close()


def test_plausible_rate_boundary():
    """The rate test, not the repetition count, is what protects real speech."""
    unit = "the demo can begin now "
    text = unit * 12
    duration_ms = int(len(text) / MAX_PLAUSIBLE_CHARS_PER_SECOND * 1000) + 500
    assert repetition_loop(text, start_ms=0, end_ms=duration_ms) is None
    assert repetition_loop(text, start_ms=0, end_ms=1000) is not None


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
def test_config_defaults_enable_the_fallback(tmp_path):
    from scientific_conference.config import load_config

    path = tmp_path / "fallback.yaml"
    path.write_text("asr:\n  temperature: 0.0\n", encoding="utf-8")
    loaded = load_config(path, env=False)
    assert loaded.asr.temperature_fallback is True
    assert loaded.asr.compression_ratio_threshold == 2.4
    assert loaded.asr.log_prob_threshold == -1.0
    assert loaded.asr.no_speech_threshold == 0.6


def test_config_can_switch_the_fallback_off(tmp_path):
    from scientific_conference.config import load_config

    path = tmp_path / "rollback.yaml"
    path.write_text("asr:\n  temperature_fallback: false\n", encoding="utf-8")
    assert load_config(path, env=False).asr.temperature_fallback is False


def test_config_rejects_an_out_of_range_temperature(tmp_path):
    from scientific_conference.config import ConfigError, load_config

    path = tmp_path / "bad.yaml"
    path.write_text("asr:\n  temperature: 3.0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path, env=False)
