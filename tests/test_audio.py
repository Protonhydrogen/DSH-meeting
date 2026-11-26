"""Audio helpers: PCM conversion, WAV round-trip and streaming resampling."""

from __future__ import annotations

import numpy as np
import pytest

from scientific_conference.audio import (
    StreamingResampler,
    float_to_pcm16,
    pcm16_to_float,
    read_wav,
    resample_fixed,
    wav_info,
    write_wav_int16,
)


def sine(freq: float, seconds: float, rate: int, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def dominant_frequency(x: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    return float(np.fft.rfftfreq(x.size, 1 / rate)[int(np.argmax(spectrum))])


def test_pcm_roundtrip():
    x = np.array([-1.0, -0.5, 0.0, 0.5, 1.0], dtype=np.float32)
    pcm = float_to_pcm16(x)
    assert pcm.dtype == np.int16
    assert pcm[0] == -32767 and pcm[-1] == 32767
    back = pcm16_to_float(pcm)
    assert np.allclose(back, x, atol=1e-3)


def test_wav_roundtrip(tmp_path):
    data = (np.arange(1000) % 100).astype(np.int16)
    path = tmp_path / "a.wav"
    write_wav_int16(path, data, 16000)
    info = wav_info(path)
    assert (info.sample_rate, info.channels, info.frames) == (16000, 1, 1000)
    back, rate = read_wav(path)
    assert rate == 16000
    # read_wav normalises integer PCM to float32 in [-1, 1)
    assert np.allclose(back, data.astype(np.float32) / 32768.0, atol=1e-6)
    assert not list(tmp_path.glob("*.part"))


def test_wav_stereo(tmp_path):
    data = np.zeros((500, 2), dtype=np.int16)
    data[:, 1] = 1000
    path = tmp_path / "s.wav"
    write_wav_int16(path, data, 8000)
    back, rate = read_wav(path)
    assert back.shape == (500, 2)
    assert rate == 8000


def test_resample_preserves_frequency():
    rate_in, rate_out = 48000, 16000
    x = sine(440.0, 0.5, rate_in)
    y = resample_fixed(x, rate_in, rate_out)
    assert abs(y.size - 0.5 * rate_out) < rate_out * 0.05
    assert abs(dominant_frequency(y, rate_out) - 440.0) < 15


def test_resample_removes_aliasing():
    """A 7 kHz tone must not survive decimation from 48 kHz to 16 kHz."""
    x = sine(7000.0, 0.4, 48000)
    y = resample_fixed(x, 48000, 16000)
    # The anti-alias filter cuts everything above 0.45 * 8 kHz = 3.6 kHz, so the
    # tone is strongly attenuated rather than folded back into the band.
    assert np.max(np.abs(y)) < np.max(np.abs(x)) * 0.25


def test_streaming_equals_oneshot():
    rate_in, rate_out = 48000, 16000
    x = sine(300.0, 1.0, rate_in) + sine(1200.0, 1.0, rate_in, amp=0.2)
    one = resample_fixed(x, rate_in, rate_out)
    rs = StreamingResampler(rate_in, rate_out)
    chunks = [rs.process(x[i : i + 1440]) for i in range(0, x.size, 1440)]
    streamed = np.concatenate(chunks)
    n = min(one.size, streamed.size)
    # Compare the overlapping region (the streaming path starts after the
    # filter warm-up and ends before its tail).
    a, b = one[200 : n - 200], streamed[200 : n - 200]
    assert a.size > 1000
    assert np.max(np.abs(a - b)) < 1e-3


def test_streaming_boundaries_are_continuous():
    """No click at a chunk boundary: the derivative must stay bounded."""
    rate = 16000
    rs = StreamingResampler(rate, rate)  # passthrough path
    x = sine(500.0, 0.2, rate)
    out = np.concatenate([rs.process(x[i : i + 512]) for i in range(0, x.size, 512)])
    assert out.size == x.size
    assert np.max(np.abs(np.diff(out))) < 0.2


def test_odd_sample_rates():
    x = sine(440.0, 0.3, 44100)
    y = resample_fixed(x, 44100, 16000)
    assert abs(y.size - 0.3 * 16000) < 200
    assert abs(dominant_frequency(y, 16000) - 440.0) < 20


def test_empty_block():
    rs = StreamingResampler(48000, 16000)
    assert rs.process(np.zeros(0, dtype=np.float32)).size == 0


def test_latency_is_small():
    rs = StreamingResampler(48000, 16000)
    assert rs.latency_ms < 2.0

def test_default_microphone_prefers_same_device_wasapi(monkeypatch):
    from types import SimpleNamespace
    from scientific_conference.audio import devices
    monkeypatch.setattr(devices, "list_devices", lambda **kwargs: [
        SimpleNamespace(index=1, name="Microphone",hostapi="MME",is_default_input=True),
        SimpleNamespace(index=9, name="Microphone",hostapi="Windows WASAPI",is_default_input=False),
        SimpleNamespace(index=10,name="Other",hostapi="Windows WASAPI",is_default_input=False)])
    assert devices.resolve_device("default") == (9,"Microphone")
