"""ASR backend interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

__all__ = ["AsrSegment", "AsrBackend", "AsrError", "ASR_SAMPLE_RATE"]

#: All backends receive 16 kHz mono float32 audio.
ASR_SAMPLE_RATE = 16000


class AsrError(RuntimeError):
    pass


@dataclass
class AsrSegment:
    """One transcribed span, with times relative to the supplied audio buffer."""

    start_ms: int
    end_ms: int
    text: str
    confidence: float | None = None
    words: list[dict[str, Any]] = field(default_factory=list)
    temperature: float | None = None
    no_speech_prob: float | None = None
    #: Set only by backends that produce speaker labels themselves (Azure).
    speaker_id: str | None = None
    #: Decoder quality signals, when the backend exposes them.  They are stored
    #: so that a pathological decode can be recognised after the fact; the
    #: backend never rewrites the text on their account.
    compression_ratio: float | None = None
    avg_logprob: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
            "confidence": self.confidence,
            "words": self.words,
            "temperature": self.temperature,
            "no_speech_prob": self.no_speech_prob,
            "speaker_id": self.speaker_id,
            "compression_ratio": self.compression_ratio,
            "avg_logprob": self.avg_logprob,
        }


class AsrBackend:
    """Base class for transcription backends."""

    name = "base"

    def transcribe(
        self,
        samples: np.ndarray,
        sample_rate: int = ASR_SAMPLE_RATE,
        *,
        language: str | None = None,
        initial_prompt: str | None = None,
        is_final: bool = True,
        offset_ms: int = 0,
    ) -> list[AsrSegment]:  # pragma: no cover - interface
        """Transcribe ``samples``; returned times are relative to their start.

        ``offset_ms`` is the absolute session time of the first sample.  Real
        backends ignore it; the deterministic mock backend uses it to place its
        scripted utterances.
        """
        raise NotImplementedError

    def close(self) -> None:
        return None

    def anomaly_flags(self, segment: AsrSegment) -> list[str]:
        """Backend quality flags for one decoded segment.

        A backend that can recognise a pathological decode (a Whisper repetition
        loop, for example) reports it here so the pipeline can *mark* the
        segment and surface it in ``conference.status``.  Flagging never rewrites
        the text: the decoded words stay exactly as the model produced them, and
        an operator or the review layer decides what to do with them.
        """
        return []

    def info(self) -> dict[str, Any]:
        return {"backend": self.name}

    @staticmethod
    def _ensure_16k(samples: np.ndarray, sample_rate: int) -> np.ndarray:
        from .audio_util import ensure_16k  # type: ignore  # pragma: no cover

        return ensure_16k(samples, sample_rate)

    def __enter__(self) -> "AsrBackend":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def normalise_text(text: str) -> str:
    return " ".join(str(text).replace("\u3000", " ").split())
