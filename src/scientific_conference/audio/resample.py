"""Audio conversion helpers: PCM formats, streaming sample-rate conversion."""

from __future__ import annotations

import math

import numpy as np

__all__ = [
    "pcm16_to_float",
    "float_to_pcm16",
    "to_mono",
    "downmix",
    "StreamingResampler",
    "resample_fixed",
    "rms_dbfs",
]


def pcm16_to_float(data: np.ndarray) -> np.ndarray:
    """int16 PCM -> float32 in [-1, 1)."""
    return (np.asarray(data, dtype=np.float32) / 32768.0).astype(np.float32)


def float_to_pcm16(data: np.ndarray) -> np.ndarray:
    """float32 in [-1, 1] -> int16 PCM with clipping."""
    x = np.clip(np.asarray(data, dtype=np.float32), -1.0, 1.0)
    return (x * 32767.0).astype(np.int16)


def to_mono(data: np.ndarray) -> np.ndarray:
    """Average channels of a ``(frames, channels)`` block."""
    x = np.asarray(data)
    if x.ndim == 1:
        return x
    if x.shape[1] == 1:
        return x[:, 0]
    return x.mean(axis=1)


def downmix(data: np.ndarray, channels: int) -> np.ndarray:
    x = np.asarray(data)
    if channels <= 1:
        return to_mono(x)
    if x.ndim == 1:
        return np.repeat(x[:, None], channels, axis=1)
    if x.shape[1] == channels:
        return x
    if x.shape[1] == 1:
        return np.repeat(x, channels, axis=1)
    return x[:, :channels]


def rms_dbfs(block: np.ndarray) -> float:
    """RMS level of a float block in dBFS (``-inf`` for pure silence)."""
    x = np.asarray(block, dtype=np.float32).ravel()
    if x.size == 0:
        return float("-inf")
    rms = float(np.sqrt(np.mean(np.square(x))))
    if rms <= 1e-12:
        return float("-inf")
    return 20.0 * math.log10(rms)


def _design_lowpass(taps: int, cutoff: float) -> np.ndarray:
    """Windowed-sinc low-pass.

    ``cutoff`` is the -6 dB corner **relative to the sample rate** (so 0..0.5,
    not 0..1): ``h[n] = 2B·sinc(2Bn)`` has its corner at ``B * sample_rate``.
    Passing a Nyquist-relative value here once cost this project its anti-alias
    filtering entirely - a 7 kHz tone survived a 48 kHz -> 16 kHz decimation at
    only -3.9 dB.
    """
    taps = int(taps) | 1  # force odd
    n = np.arange(taps, dtype=np.float64) - (taps - 1) / 2.0
    h = 2.0 * cutoff * np.sinc(2.0 * cutoff * n)
    h *= np.hamming(taps)
    total = h.sum()
    if total == 0:
        raise ValueError("degenerate filter")
    return (h / total).astype(np.float64)


class StreamingResampler:
    """Continuous sample-rate conversion for a chunked audio stream.

    The resampler band-limits the signal at the lower of the two Nyquist
    frequencies and then samples the filtered signal at fractional positions.
    Because the filter tail and the fractional read position are carried across
    calls, concatenating the outputs of successive :meth:`process` calls gives
    the same result as resampling the whole stream at once (up to floating
    point noise), so no clicks are introduced at chunk boundaries.

    Latency is ``(taps - 1) / 2`` input samples (well under a millisecond for
    the default filter), which is negligible for the ASR time stamps recorded
    by this project.
    """

    def __init__(self, sr_in: int, sr_out: int, taps: int | None = None):
        if sr_in <= 0 or sr_out <= 0:
            raise ValueError("sample rates must be positive")
        self.sr_in = int(sr_in)
        self.sr_out = int(sr_out)
        self.ratio = self.sr_out / float(self.sr_in)
        if taps is None:
            taps = 63 if sr_out < sr_in else 127
        self.taps = int(taps) | 1
        # 0.45 of the *lower* Nyquist, expressed relative to the input rate.
        cutoff = 0.45 * min(1.0, self.ratio) / 2.0
        self.h = _design_lowpass(self.taps, cutoff)
        self._tail = np.zeros(self.taps - 1, dtype=np.float64)
        self._tail_start = 0
        self._next_j = 0

    @property
    def passthrough(self) -> bool:
        return self.sr_in == self.sr_out

    @property
    def latency_ms(self) -> float:
        return (self.taps - 1) / 2.0 / self.sr_in * 1000.0

    def process(self, block: np.ndarray) -> np.ndarray:
        x = np.asarray(block, dtype=np.float64).ravel()
        if self.passthrough:
            return x.astype(np.float32)
        if x.size == 0:
            return np.zeros(0, dtype=np.float32)

        buf = np.concatenate([self._tail, x]) if self._tail.size else x
        buf_start = self._tail_start
        y = np.convolve(buf, self.h, mode="valid")
        # global input-stream index of the filter centre for y[i]
        y_global = buf_start + np.arange(y.size) + (self.taps - 1) / 2.0

        j_start = max(self._next_j, int(math.ceil(y_global[0] * self.ratio)))
        j_end = int(math.floor(y_global[-1] * self.ratio))
        if j_end < j_start:
            out = np.zeros(0, dtype=np.float32)
        else:
            t = np.arange(j_start, j_end + 1, dtype=np.float64) / self.ratio
            out = np.interp(t, y_global, y).astype(np.float32)
            self._next_j = j_end + 1

        self._tail = buf[-(self.taps - 1):].copy()
        self._tail_start = buf_start + buf.size - (self.taps - 1)
        return out

    def reset(self) -> None:
        self._tail = np.zeros(self.taps - 1, dtype=np.float64)
        self._tail_start = 0
        self._next_j = 0


def resample_fixed(block: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """One-shot resampling of a complete signal."""
    if sr_in == sr_out:
        return np.asarray(block, dtype=np.float32)
    rs = StreamingResampler(sr_in, sr_out)
    return rs.process(block)
