"""Voice activity detection.

Two backends are provided:

``silero``
    The official Silero VAD ONNX model (v4 or v5 signature, detected at load
    time).  This is the default; the model file is downloaded once into
    the configured model directory.

``energy``
    A small adaptive-RMS fallback used when the ONNX runtime or the model file
    is unavailable.  It is deliberately conservative and is reported as such in
    ``conference.status``.

Both feed :class:`VadGate`, which turns per-frame probabilities into speech
spans with hysteresis (threshold, minimum speech duration, minimum silence
duration, padding).  The gate never guesses a speaker - it only answers "is
somebody talking".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

__all__ = [
    "VadEvent",
    "VadGate",
    "SileroVad",
    "EnergyVad",
    "NullVad",
    "load_vad",
    "VAD_FRAME_SAMPLES",
]

#: Silero v5 operates on 512-sample frames at 16 kHz.
VAD_FRAME_SAMPLES = 512


@dataclass
class VadEvent:
    kind: str  # "start" | "end"
    time_ms: int
    prob: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "time_ms": self.time_ms, "prob": round(self.prob, 4)}


@dataclass
class _Span:
    start_ms: int
    end_ms: int | None = None
    peak_prob: float = 0.0

    @property
    def duration_ms(self) -> int:
        return (self.end_ms if self.end_ms is not None else self.start_ms) - self.start_ms


class _Backend:
    name = "none"
    frame_samples = VAD_FRAME_SAMPLES

    def probs(self, frames: np.ndarray) -> np.ndarray:  # pragma: no cover - interface
        raise NotImplementedError

    def reset(self) -> None:
        return None

    def info(self) -> dict[str, Any]:
        return {"backend": self.name, "frame_samples": self.frame_samples}


class NullVad(_Backend):
    """Treats everything as speech (used by ``vad.backend: none``)."""

    name = "none"

    def probs(self, frames: np.ndarray) -> np.ndarray:
        return np.ones(frames.shape[0], dtype=np.float32)


class EnergyVad(_Backend):
    """Adaptive RMS gate; a fallback, not a replacement for Silero."""

    name = "energy"

    def __init__(self, threshold: float = 0.012, frame_samples: int = VAD_FRAME_SAMPLES):
        self.threshold = float(threshold)
        self.frame_samples = int(frame_samples)

    def probs(self, frames: np.ndarray) -> np.ndarray:
        rms = np.sqrt(np.mean(np.square(frames.astype(np.float32)), axis=1) + 1e-12)
        # Map RMS to a pseudo-probability around the threshold (log scale).
        return np.clip(
            (np.log10(rms + 1e-8) - math.log10(self.threshold) + 0.35) / 0.7, 0.0, 1.0
        ).astype(np.float32)


class SileroVad(_Backend):
    """Silero VAD through onnxruntime."""

    name = "silero"

    def __init__(self, model_path: str | Path, *, threshold: float = 0.5):
        import onnxruntime as ort

        self.model_path = Path(model_path)
        if not self.model_path.exists():
            raise FileNotFoundError(f"Silero VAD model not found: {self.model_path}")
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 3
        self.session = ort.InferenceSession(
            str(self.model_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.threshold = float(threshold)
        names = {i.name for i in self.session.get_inputs()}
        self._inputs = [i.name for i in self.session.get_inputs()]
        self._has_sr = "sr" in names
        self._v5 = self._has_sr
        self.context_samples = 64
        self.frame_samples = 512 if self._v5 else 1536
        sr_in = [i for i in self.session.get_inputs() if i.name == "input"][0]
        self._state_shape = None
        for i in self.session.get_inputs():
            if i.name == "state":
                self._state_shape = [int(d) if isinstance(d, int) else 1 for d in i.shape]
            elif i.name == "h":
                self._state_shape = [int(d) if isinstance(d, int) else 1 for d in i.shape]
        if self._state_shape is None:
            self._state_shape = [2, 1, 64] if not self._v5 else [2, 1, 128]
        self.reset()

    def reset(self) -> None:
        shape = self._state_shape or [2, 1, 128]
        self._state = np.zeros(shape, dtype=np.float32)
        if not self._v5:
            self._h = np.zeros(shape, dtype=np.float32)
            self._c = np.zeros(shape, dtype=np.float32)
        # Both the v4 and the v5 ONNX graph expect ``context_samples`` of the
        # previous frame prepended to the new one (64 at 16 kHz).  Without it
        # the model outputs near-zero probability for real speech.
        self._context = np.zeros(self.context_samples, dtype=np.float32)

    def probs(self, frames: np.ndarray) -> np.ndarray:
        out = np.zeros(frames.shape[0], dtype=np.float32)
        for i in range(frames.shape[0]):
            x = frames[i].astype(np.float32)
            if not self._v5:
                # v4 takes a 1536-sample window and 64 samples of context.
                if x.size != 1536:
                    x = np.resize(x, 1536)
                inp = np.concatenate([self._context, x]).astype(np.float32)[None, :]
                o, self._h, self._c = self.session.run(
                    None, {"input": inp, "h": self._h, "c": self._c}
                )
            else:
                inp = np.concatenate([self._context, x]).astype(np.float32)[None, :]
                o, self._state = self.session.run(
                    None,
                    {
                        "input": inp,
                        "state": self._state,
                        "sr": np.array(16000, dtype=np.int64),
                    },
                )
            self._context = x[-self.context_samples :].copy()
            out[i] = float(np.asarray(o).reshape(-1)[0])
        return out

    def info(self) -> dict[str, Any]:
        return {
            "backend": "silero",
            "model": str(self.model_path),
            "signature": "v5" if self._v5 else "v4",
            "frame_samples": self.frame_samples,
        }


@dataclass
class VadGate:
    """Turn per-frame speech probabilities into speech spans.

    Parameters mirror the tuning table in the design document: short pauses are
    used only to stabilise sentences, longer silence closes an utterance.
    """

    backend: _Backend
    threshold: float = 0.5
    min_speech_ms: int = 250
    min_silence_ms: int = 400
    pad_ms: int = 200

    def __post_init__(self) -> None:
        self.frame_samples = int(self.backend.frame_samples)
        self._buf = np.zeros(0, dtype=np.float32)
        self._consumed_ms = 0.0  # ms of audio already consumed into frames
        self._in_speech = False
        self._pending_speech_frames = 0
        self._pending_start_ms: float | None = None
        self._silence_ms = 0.0
        self._silence_start_ms: float | None = None
        self._span_start_ms: float | None = None
        self._peak = 0.0
        self.events: list[VadEvent] = []
        self.spans: list[_Span] = []
        self._frame_ms = self.frame_samples / 16000.0 * 1000.0

    # -- public API -----------------------------------------------------
    def push(self, samples: np.ndarray) -> list[VadEvent]:
        """Feed 16 kHz mono float32 samples; return the events produced."""
        x = np.asarray(samples, dtype=np.float32).ravel()
        produced: list[VadEvent] = []
        if x.size:
            self._buf = np.concatenate([self._buf, x]) if self._buf.size else x

        while self._buf.size >= self.frame_samples:
            frame = self._buf[: self.frame_samples]
            self._buf = self._buf[self.frame_samples :]
            prob = float(self.backend.probs(frame[None, :])[0])
            frame_start = self._consumed_ms
            self._consumed_ms += self._frame_ms
            produced.extend(self._consume_frame(prob, frame_start, self._consumed_ms))
        return produced

    def flush(self) -> list[VadEvent]:
        """Close any open span (end of stream)."""
        produced: list[VadEvent] = []
        if self._buf.size:
            pad = np.zeros(self.frame_samples - self._buf.size, dtype=np.float32)
            self.push(np.concatenate([self._buf, pad]))
        if self._in_speech or self._pending_start_ms is not None:
            end = self._consumed_ms
            start = self._span_start_ms if self._span_start_ms is not None else self._pending_start_ms
            if start is not None and end - start >= self.min_speech_ms:
                produced.append(VadEvent("end", int(round(end)), self._peak))
                self.spans.append(_Span(int(round(start)), int(round(end)), self._peak))
            self._in_speech = False
            self._pending_start_ms = None
            self._span_start_ms = None
            self._silence_ms = 0.0
        self.events.extend(produced)
        return produced

    def reset(self) -> None:
        self.backend.reset()
        self.__post_init__()

    # -- internals ------------------------------------------------------
    def _consume_frame(self, prob: float, frame_start: float, frame_end: float) -> list[VadEvent]:
        out: list[VadEvent] = []
        if prob >= self.threshold:
            self._peak = max(self._peak, prob)
            self._silence_ms = 0.0
            self._silence_start_ms = None
            if self._in_speech:
                self._pending_speech_frames = 0
                return out
            if self._pending_start_ms is None:
                self._pending_start_ms = frame_start
            self._pending_speech_frames += 1
            if self._pending_speech_frames * self._frame_ms >= self.min_speech_ms:
                start = max(0.0, self._pending_start_ms - self.pad_ms)
                self._span_start_ms = start
                self._in_speech = True
                out.append(VadEvent("start", int(round(start)), prob))
        else:
            if self._in_speech:
                if self._silence_start_ms is None:
                    self._silence_start_ms = frame_start
                self._silence_ms = frame_end - self._silence_start_ms
                if self._silence_ms >= self.min_silence_ms:
                    end = min(self._consumed_ms, self._silence_start_ms + self.pad_ms)
                    end = max(end, self._span_start_ms or 0.0)
                    out.append(VadEvent("end", int(round(end)), self._peak))
                    self.spans.append(_Span(int(round(self._span_start_ms or 0.0)), int(round(end)), self._peak))
                    self._in_speech = False
                    self._pending_start_ms = None
                    self._span_start_ms = None
                    self._silence_ms = 0.0
                    self._silence_start_ms = None
                    self._pending_speech_frames = 0
                    self._peak = 0.0
            else:
                self._pending_speech_frames = 0
                self._pending_start_ms = None
                self._peak = 0.0
        return out

    @property
    def quiet_boundary(self) -> bool:
        return not self._in_speech and self._pending_start_ms is None

    @property
    def unclassified_audio(self) -> np.ndarray:
        return self._buf.copy()

    def info(self) -> dict[str, Any]:
        info = self.backend.info()
        info.update(
            {
                "threshold": self.threshold,
                "min_speech_ms": self.min_speech_ms,
                "min_silence_ms": self.min_silence_ms,
                "pad_ms": self.pad_ms,
            }
        )
        return info


def load_vad(cfg) -> VadGate:
    """Build a :class:`VadGate` from configuration, falling back to energy VAD."""
    backend_name = cfg.vad.backend
    problems: list[str] = []
    backend: _Backend
    if backend_name == "none":
        backend = NullVad()
    elif backend_name == "energy":
        backend = EnergyVad(threshold=cfg.vad.energy_threshold)
    else:
        try:
            backend = SileroVad(cfg.vad_model_path, threshold=cfg.vad.threshold)
        except Exception as exc:
            problems.append(f"silero unavailable ({type(exc).__name__}: {exc}); using energy VAD")
            backend = EnergyVad(threshold=cfg.vad.energy_threshold)
    gate = VadGate(
        backend=backend,
        threshold=cfg.vad.threshold,
        min_speech_ms=cfg.vad.min_speech_ms,
        min_silence_ms=cfg.vad.min_silence_ms,
        pad_ms=cfg.vad.pad_ms,
    )
    gate.fallback_notes = problems  # type: ignore[attr-defined]
    return gate
