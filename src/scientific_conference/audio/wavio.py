"""Minimal WAV input/output built on the standard library.

Only 16-bit PCM (the format produced by WASAPI shared-mode capture in this
project) and 32-bit float WAV files are supported.  Writing is atomic: data
goes to ``<name>.part`` first and is renamed on success, so an interrupted
recording never leaves a half-written file that looks complete.
"""

from __future__ import annotations

import os
import wave
from pathlib import Path

import numpy as np

__all__ = ["write_wav_int16", "read_wav", "wav_info", "WavInfo"]


class WavInfo:
    def __init__(self, path: Path, sample_rate: int, channels: int, frames: int, sampwidth: int):
        self.path = path
        self.sample_rate = sample_rate
        self.channels = channels
        self.frames = frames
        self.sampwidth = sampwidth

    @property
    def duration_ms(self) -> int:
        return int(round(self.frames / self.sample_rate * 1000)) if self.sample_rate else 0

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"WavInfo({self.path.name}, {self.sample_rate}Hz, {self.channels}ch, "
            f"{self.frames} frames, {self.sampwidth * 8}bit)"
        )


def write_wav_int16(path: str | os.PathLike[str], data: np.ndarray, sample_rate: int) -> int:
    """Write int16 PCM data (``(frames,)`` or ``(frames, channels)``) atomically."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    x = np.asarray(data)
    if x.dtype != np.int16:
        x = np.clip(x, -32768, 32767).astype(np.int16)
    if x.ndim == 1:
        x = x[:, None]
    channels = int(x.shape[1])
    tmp = p.with_suffix(p.suffix + ".part")
    with wave.open(str(tmp), "wb") as fh:
        fh.setnchannels(channels)
        fh.setsampwidth(2)
        fh.setframerate(int(sample_rate))
        fh.writeframes(x.tobytes())
    os.replace(tmp, p)
    return int(x.shape[0])


def wav_info(path: str | os.PathLike[str]) -> WavInfo:
    p = Path(path)
    with wave.open(str(p), "rb") as fh:
        return WavInfo(p, fh.getframerate(), fh.getnchannels(), fh.getnframes(), fh.getsampwidth())


def read_wav(path: str | os.PathLike[str]) -> tuple[np.ndarray, int]:
    """Read a WAV file, returning float32 samples and the sample rate.

    For 8/16/32-bit integer PCM the result is normalised to ``[-1, 1)``; for
    32-bit float files the samples are returned unchanged.  Shape is
    ``(frames,)`` for mono, ``(frames, channels)`` otherwise.
    """
    p = Path(path)
    with wave.open(str(p), "rb") as fh:
        channels = fh.getnchannels()
        width = fh.getsampwidth()
        rate = fh.getframerate()
        frames = fh.getnframes()
        raw = fh.readframes(frames)

    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"unsupported sample width: {width} bytes ({p})")

    if channels > 1:
        data = data.reshape(-1, channels)
    return data, rate
