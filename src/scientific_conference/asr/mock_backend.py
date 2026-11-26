"""Deterministic ASR backend used by the test suite and by ``--asr mock``.

It never touches a model file, so the whole pipeline (VAD -> windows ->
segments -> turns -> events -> notes) can be exercised reproducibly on any
machine.  Two modes are available:

``script``
    a list of ``{"start_ms", "end_ms", "text", "speaker_id"}`` entries; any
    window overlapping an entry returns it (times rebased to the window);
``echo``
    synthesises a short description of the audio window (duration and level),
    which is enough to check timing and de-duplication.
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

from .base import ASR_SAMPLE_RATE, AsrBackend, AsrSegment

__all__ = ["MockAsrBackend"]


class MockAsrBackend(AsrBackend):
    name = "mock"

    def __init__(self, script: Sequence[dict[str, Any]] | None = None, mode: str = "auto"):
        self.script = [dict(item) for item in (script or [])]
        self.script.sort(key=lambda item: int(item.get("start_ms", 0)))
        self.mode = mode if mode != "auto" else ("script" if self.script else "echo")
        self.calls: list[dict[str, Any]] = []

    def transcribe(
        self,
        samples: np.ndarray,
        sample_rate: int = ASR_SAMPLE_RATE,
        *,
        language: str | None = None,
        initial_prompt: str | None = None,
        is_final: bool = True,
        offset_ms: int = 0,
    ) -> list[AsrSegment]:
        x = np.asarray(samples, dtype=np.float32).ravel()
        duration_ms = int(round(x.size / sample_rate * 1000))
        self.calls.append({"offset_ms": int(offset_ms), "duration_ms": duration_ms, "is_final": is_final})

        if self.mode == "script":
            out: list[AsrSegment] = []
            window_start = int(offset_ms)
            window_end = window_start + duration_ms
            for item in self.script:
                s = int(item.get("start_ms", 0))
                e = int(item.get("end_ms", s))
                if e <= window_start or s >= window_end:
                    continue
                text = str(item.get("text", "")).strip()
                if not text:
                    continue
                out.append(
                    AsrSegment(
                        start_ms=max(0, s - window_start),
                        end_ms=min(duration_ms, e - window_start),
                        text=text,
                        confidence=item.get("confidence"),
                        speaker_id=item.get("speaker_id"),
                    )
                )
            return out

        if duration_ms < 150:
            return []
        level = float(np.sqrt(np.mean(np.square(x)))) if x.size else 0.0
        return [
            AsrSegment(
                start_ms=0,
                end_ms=duration_ms,
                text=f"[mock {duration_ms}ms level={level:.3f}]",
                confidence=None,
            )
        ]
