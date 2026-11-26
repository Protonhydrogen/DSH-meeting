"""Local Qwen3-ASR streaming client and VAD-to-transcript adapter."""

from __future__ import annotations

import base64
import json
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

import numpy as np

from .base import ASR_SAMPLE_RATE, AsrError, normalise_text
from .streaming import StableSegment, _new_segment_id
from ..diarization import SPEAKER_UNKNOWN

QWEN_MODEL = "Qwen/Qwen3-ASR-0.6B"
QWEN_BACKEND = "qwen3_asr"
LANGUAGES = {"zh": "Chinese", "en": "English"}


class QwenBackend:
    name = QWEN_BACKEND

    def __init__(self, settings, *, language_hints=()):
        self.settings = settings
        self._url = f"http://127.0.0.1:{settings.port}"
        self._opener = build_opener(ProxyHandler({}))
        self._window = None
        self._pending = []
        self._sample_count = 0
        self._language = settings.language
        if self._language == "inherit":
            hints = list(language_hints)
            self._language = hints[0] if len(hints) == 1 else "auto"

    def _request(self, route, data=None, *, timeout=None):
        request = Request(self._url + route)
        if data is not None:
            request.data = json.dumps(data, ensure_ascii=False).encode("utf-8")
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(request, timeout=timeout or self.settings.timeout_seconds) as response:
                result = json.loads(response.read(65537))
                if not isinstance(result, dict):
                    raise ValueError("Unexpected local ASR response")
                return result
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read(4096)).get("error", str(exc))
            except (ValueError, OSError, AttributeError):
                detail = str(exc)
            raise AsrError(f"Qwen3-ASR: {detail}") from exc
        except (OSError, ValueError) as exc:
            raise AsrError(f"Qwen3-ASR local service on port {self.settings.port}: {exc}") from exc

    def prepare(self):
        result = self._request("/health", timeout=2)
        if result.get("model") != QWEN_MODEL or result.get("protocol") != 1:
            raise AsrError("Unexpected Qwen3-ASR service model or protocol")

    def begin(self):
        self._window = uuid4().hex
        self._request("/begin", {
            "window": self._window,
            "language": LANGUAGES.get(self._language),
            "chunk_seconds": self.settings.chunk_seconds,
        })

    def push(self, audio):
        self._pending.append(audio)
        self._sample_count += audio.size
        if self._sample_count < ASR_SAMPLE_RATE / 5:
            return None
        return self._send_pending()

    def _send_pending(self):
        samples = np.concatenate(self._pending).astype("<f4", copy=False)
        self._pending.clear()
        self._sample_count = 0
        return self._request("/push", {
            "window": self._window,
            "audio": base64.b64encode(samples.tobytes()).decode("ascii"),
        })

    def finish(self):
        if self._pending:
            self._send_pending()
        result = self._request("/finish", {"window": self._window})
        self._window = None
        return result

    def cancel(self):
        window, self._window = self._window, None
        self._pending.clear()
        self._sample_count = 0
        if window is not None:
            try:
                self._request("/cancel", {"window": window}, timeout=2)
            except AsrError:
                pass

    close = cancel

    def info(self):
        return {
            "backend": self.name, "model": QWEN_MODEL, "mode": "streaming",
            "timing": "audio_range", "chunk_seconds": self.settings.chunk_seconds,
            "language": self._language,
        }


class QwenTranscriber:
    """One streaming decoder state per VAD utterance, with a strict size limit."""

    def __init__(self, backend, gate, *, session_offset_ms=0, max_window_ms=20000,
                 preroll_ms=500, end_hold_ms=0, diarizer=None):
        self.backend = backend
        self.gate = gate
        self.offset_ms = session_offset_ms
        self._limit = self._samples(max_window_ms)
        self._end_hold = self._samples(end_hold_ms)
        self._history = max(self._samples(preroll_ms), self._end_hold)
        self._diarizer = diarizer
        self._pre = np.empty(0, dtype=np.float32)
        self._clock = 0
        self._cursor = 0
        self._start = None
        self._speech = False
        self._failed = False
        self._closed_until = 0
        self._audio = []
        self.partial = ""
        self.segments_emitted = 0
        self.trimmed_segments = 0
        self.dropped_segments = 0
        self.last_error = None

    @staticmethod
    def _samples(ms):
        return round(ms * ASR_SAMPLE_RATE / 1000)

    def _ms(self, sample):
        return self.offset_ms + sample * 1000 / ASR_SAMPLE_RATE

    @property
    def stream_ms(self):
        return self._ms(self._clock)

    @property
    def span_open(self):
        return self._speech

    def _fail(self, exc):
        self.last_error = f"{type(exc).__name__}: {exc}"
        self._failed = True
        self.partial = ""
        self.backend.cancel()

    def _begin(self, start):
        self._start = self._cursor = start
        self._failed = False
        self._audio = []
        self.partial = ""
        try:
            self.backend.begin()
        except (AsrError, OSError) as exc:
            self._fail(exc)

    def _finish(self):
        start, self._start = self._start, None
        self.partial = ""
        self._closed_until = self._cursor
        if self._failed:
            return []
        try:
            result = self.backend.finish()
        except (AsrError, OSError) as exc:
            self._fail(exc)
            return []
        self.last_error = None
        text = normalise_text(result.get("text", ""))
        if not text or self._cursor <= start:
            return []
        start_ms, end_ms = round(self._ms(start)), round(self._ms(self._cursor))
        speaker = SPEAKER_UNKNOWN
        if self._diarizer is not None and self._audio:
            assigned, _ = self._diarizer.assign(
                np.concatenate(self._audio), ASR_SAMPLE_RATE,
                start_ms=start_ms, end_ms=end_ms, text=text,
            )
            speaker = assigned or SPEAKER_UNKNOWN
        self.segments_emitted += 1
        return [StableSegment(
            _new_segment_id(), start_ms, max(start_ms + 1, end_ms), text, speaker,
            flags=["timing:audio_range"], window_id=f"win_{uuid4().hex[:8]}",
        )]

    def _feed_until(self, end, origin, audio):
        out = []
        while self._cursor < end:
            if self._start is None:
                self._begin(self._cursor)
            limit = min(end, self._start + self._limit)
            piece = audio[self._cursor - origin:limit - origin]
            if not self._failed:
                try:
                    result = self.backend.push(piece)
                    if result is not None:
                        self.partial = normalise_text(result.get("text", ""))
                    if self._diarizer is not None:
                        self._audio.append(piece)
                except (AsrError, OSError) as exc:
                    self._fail(exc)
            self._cursor = limit
            if self._cursor == self._start + self._limit:
                out.extend(self._finish())
        return out

    def push(self, samples):
        samples = np.asarray(samples, dtype=np.float32).ravel()
        if not samples.size:
            return []
        origin = self._clock - self._pre.size
        audio = np.concatenate((self._pre, samples))
        self._clock += samples.size
        out = []
        for event in self.gate.push(samples):
            position = min(self._clock, self._samples(event.time_ms))
            if event.kind == "start":
                self._speech = True
                self._cursor = max(origin, position, self._closed_until)
            elif event.kind == "end" and self._speech:
                out.extend(self._feed_until(position, origin, audio))
                if self._start is not None:
                    out.extend(self._finish())
                self._speech = False
        if self._speech:
            # VAD reports a padded end after observing the full silence threshold.
            out.extend(self._feed_until(self._clock - self._end_hold, origin, audio))
        self._pre = audio[-self._history:].copy() if self._history else audio[:0]
        return out

    def flush(self):
        self.gate.flush()
        out = []
        if self._speech:
            out.extend(self._feed_until(self._clock, self._clock - self._pre.size, self._pre))
        self._speech = False
        if self._start is not None:
            out.extend(self._finish())
        return out
