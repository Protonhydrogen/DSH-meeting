"""Compatibility interface for unassigned transcript segments."""

from __future__ import annotations

from typing import Any

import numpy as np


SPEAKER_UNKNOWN = "speaker_unknown"


class Diarizer:
    name = "none"

    def assign(
        self,
        samples: np.ndarray,
        sample_rate: int,
        *,
        start_ms: int = 0,
        end_ms: int = 0,
        text: str = "",
    ) -> tuple[str | None, float | None]:
        return None, None

    def info(self) -> dict[str, Any]:
        return {"backend": self.name}

    def reset(self) -> None:
        return None


class NullDiarizer(Diarizer):
    name = "none"


def load_diarizer(cfg) -> Diarizer:
    return NullDiarizer()
