"""Incremental transcription layer: VAD spans -> stable, de-duplicated segments.

A background ASR model such as faster-whisper is *not* a streaming engine: it
transcribes a finished window of audio.  This module turns that into a usable
near-realtime stream:

1. audio is fed to the VAD gate, which reports speech spans;
2. an open window accumulates audio (with a pre-roll so the first syllable is
   not clipped);
3. the window is committed when the VAD reports the end of the span (after
   ``utterance_silence_ms`` of silence), or when it grows past ``max_window_ms``;
4. long monologues are split at the quietest 200 ms of the trailing search
   window, and ``overlap_ms`` of audio is carried into the next window so a
   word cut in half is still recognised;
5. segments coming out of the overlapped region are trimmed or dropped using
   word timestamps, so text is never duplicated.

Every committed segment carries an absolute session time range, a revision and
a stability flag.  The time model is absolute: the transcriber is told the
session time of the first sample it receives and tracks the end of the audio
stored so far, so out-of-order or repeated appends cannot happen.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from .base import ASR_SAMPLE_RATE, AsrBackend, AsrSegment, normalise_text
from ..diarization import SPEAKER_UNKNOWN, Diarizer

__all__ = ["StableSegment", "StreamingTranscriber"]


@dataclass
class StableSegment:
    segment_id: str
    start_ms: int
    end_ms: int
    text: str
    speaker_id: str | None
    is_final: bool = True
    revision: int = 1
    confidence: float | None = None
    flags: list[str] = field(default_factory=list)
    words: list[dict[str, Any]] = field(default_factory=list)
    window_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
            "speaker_id": self.speaker_id,
            "is_final": self.is_final,
            "revision": self.revision,
            "confidence": self.confidence,
            "flags": list(self.flags),
            "window_id": self.window_id,
        }


def _new_segment_id() -> str:
    return f"seg_{uuid.uuid4().hex[:10]}"


class StreamingTranscriber:
    """Consumes 16 kHz mono audio and emits :class:`StableSegment` objects."""

    def __init__(
        self,
        backend: AsrBackend,
        gate: Any,
        *,
        session_offset_ms: float = 0,
        max_window_ms: int = 20000,
        overlap_ms: int = 1500,
        split_search_ms: int = 4000,
        min_segment_ms: int = 300,
        preroll_ms: int = 500,
        diarizer: Diarizer | None = None,
        on_segment: Callable[[StableSegment], None] | None = None,
        language: str | None = None,
    ) -> None:
        self.backend = backend
        self.gate = gate
        self.offset_ms = float(session_offset_ms)
        self.max_window_ms = int(max_window_ms)
        self.overlap_ms = int(overlap_ms)
        self.split_search_ms = int(split_search_ms)
        self.min_segment_ms = int(min_segment_ms)
        self.preroll_ms = int(preroll_ms)
        self.diarizer = diarizer
        self.on_segment = on_segment
        self.language = language

        self._stream_ms = float(self.offset_ms)  # end of all audio pushed so far
        self._pre = np.zeros(0, dtype=np.float32)
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_start_ms = float(self.offset_ms)
        self._win_end_ms = float(self.offset_ms)
        self._span_open = False
        self._committed_until_ms = -1.0
        self._recent_texts: list[str] = []
        self._window_id = ""
        self.segments_emitted = 0
        self.trimmed_segments = 0
        self.dropped_segments = 0
        self.last_error: str | None = None

    # ------------------------------------------------------------------
    @property
    def buffered_ms(self) -> float:
        return self._buf.size / ASR_SAMPLE_RATE * 1000.0

    @property
    def window_end_ms(self) -> float:
        return self._win_end_ms

    @property
    def span_open(self) -> bool:
        """True while speech is in progress *or* being decoded.

        The recorder's boundary clock uses this to distinguish "the room is
        silent" from "the model has not caught up yet": while a window is open
        or being transcribed, no turn may be closed as silence.
        """
        return self._span_open

    @property
    def stream_ms(self) -> float:
        """Session time of the end of all audio pushed into the transcriber."""
        return self._stream_ms

    def push(self, samples: np.ndarray) -> list[StableSegment]:
        x = np.asarray(samples, dtype=np.float32).ravel()
        if x.size == 0:
            return []
        call_start_ms = self._stream_ms
        call_end_ms = call_start_ms + x.size / ASR_SAMPLE_RATE * 1000.0

        events = self.gate.push(x)
        out: list[StableSegment] = []
        for ev in events:
            ev_abs = float(ev.time_ms) + self.offset_ms
            if ev.kind == "start":
                self._open_window(call_start_ms)
            elif ev.kind == "end":
                self._append_up_to(min(ev_abs, call_end_ms), x, call_start_ms)
                out.extend(self._commit_window(ev_abs))

        self._append_up_to(call_end_ms, x, call_start_ms)
        if self._span_open:
            out.extend(self._maybe_split())
        self._stream_ms = call_end_ms
        self._update_preroll(x)

        for seg in out:
            self.segments_emitted += 1
            if self.on_segment is not None:
                self.on_segment(seg)
        return out

    def flush(self) -> list[StableSegment]:
        """Close the stream: commit whatever is still open."""
        out: list[StableSegment] = []
        for ev in self.gate.flush():
            if ev.kind == "end":
                out.extend(self._commit_window(float(ev.time_ms) + self.offset_ms))
        if self._buf.size:
            self._span_open = False
            out.extend(self._trim_committed(self._transcribe(self._buf, self._buf_start_ms)))
            self._buf = np.zeros(0, dtype=np.float32)
        for seg in out:
            self.segments_emitted += 1
            if self.on_segment is not None:
                self.on_segment(seg)
        return out

    # ------------------------------------------------------------------
    def _open_window(self, call_start_ms: float) -> None:
        # Only a short pre-roll is needed here: the VAD start time already
        # includes its own padding.  The longer ``overlap_ms`` is used when a
        # long monologue is split, not when a new utterance begins.
        keep = int(self.preroll_ms / 1000.0 * ASR_SAMPLE_RATE)
        pre = self._pre[-keep:] if (keep and self._pre.size) else np.zeros(0, dtype=np.float32)
        self._buf = pre.copy()
        self._win_end_ms = call_start_ms
        self._buf_start_ms = call_start_ms - pre.size / ASR_SAMPLE_RATE * 1000.0
        self._span_open = True
        self._window_id = f"win_{uuid.uuid4().hex[:8]}"

    def _append_up_to(self, limit_ms: float, x: np.ndarray, call_start_ms: float) -> None:
        if not self._span_open or limit_ms <= self._win_end_ms:
            return
        start_off = int(max(0.0, (self._win_end_ms - call_start_ms)) / 1000.0 * ASR_SAMPLE_RATE)
        end_off = int(min(float(x.size), (limit_ms - call_start_ms) / 1000.0 * ASR_SAMPLE_RATE))
        if end_off <= start_off:
            return
        piece = x[start_off:end_off]
        self._buf = np.concatenate([self._buf, piece]) if self._buf.size else piece.copy()
        self._win_end_ms = call_start_ms + end_off / ASR_SAMPLE_RATE * 1000.0

    def _update_preroll(self, x: np.ndarray) -> None:
        keep = int(max(self.preroll_ms, self.overlap_ms) / 1000.0 * ASR_SAMPLE_RATE)
        if keep <= 0:
            return
        if x.size >= keep:
            self._pre = x[-keep:].copy()
        else:
            self._pre = np.concatenate([self._pre, x])[-keep:]

    def _maybe_split(self) -> list[StableSegment]:
        if self.buffered_ms < self.max_window_ms:
            return []
        split_ms = self._find_split_point()
        if split_ms is None:
            return []
        split_pos = int((split_ms - self._buf_start_ms) / 1000.0 * ASR_SAMPLE_RATE)
        split_pos = max(1, min(split_pos, self._buf.size - 1))
        audio = self._buf[:split_pos]
        committed_end = self._buf_start_ms + audio.size / ASR_SAMPLE_RATE * 1000.0
        keep_from = max(0, split_pos - int(self.overlap_ms / 1000.0 * ASR_SAMPLE_RATE))

        segments = self._transcribe(audio, self._buf_start_ms)
        for seg in segments:
            seg.flags.append("window_split")
        # Trim against the *previous* boundary first: the segments just produced
        # define the new boundary, so comparing them with it would discard them.
        out = self._trim_committed(segments)
        self._committed_until_ms = committed_end
        self._buf = self._buf[keep_from:].copy()
        self._buf_start_ms += keep_from / ASR_SAMPLE_RATE * 1000.0
        self._window_id = f"win_{uuid.uuid4().hex[:8]}"
        return out

    def _find_split_point(self) -> float | None:
        """Pick the quietest 200 ms inside the trailing search window."""
        search_ms = min(self.split_search_ms, self.buffered_ms - self.max_window_ms * 0.5)
        if search_ms <= 0:
            return None
        win = int(0.2 * ASR_SAMPLE_RATE)
        latest = self._buf.size - win
        earliest = max(0, latest - int(search_ms / 1000.0 * ASR_SAMPLE_RATE))
        if latest - earliest < win:
            return None
        best_pos, best_energy = latest, float("inf")
        step = max(1, int(0.05 * ASR_SAMPLE_RATE))
        for pos in range(earliest, latest, step):
            energy = float(np.mean(np.square(self._buf[pos : pos + win])))
            if energy < best_energy:
                best_energy, best_pos = energy, pos
        return self._buf_start_ms + best_pos / ASR_SAMPLE_RATE * 1000.0

    def _commit_window(self, end_ms: float) -> list[StableSegment]:
        if not self._span_open:
            return []
        if self._buf.size == 0:
            self._span_open = False
            return []
        audio = self._buf
        start = self._buf_start_ms
        self._buf = np.zeros(0, dtype=np.float32)
        # ``_span_open`` deliberately stays true while the window is being
        # transcribed: the boundary clock (driven by another thread) must not
        # mistake audio that is still being decoded for silence.
        segments = self._transcribe(audio, start)
        for seg in segments:
            if seg.end_ms - seg.start_ms < self.min_segment_ms:
                seg.flags.append("short")
        out = self._trim_committed(segments)
        # Everything up to the end of this window is now committed, so the next
        # window can drop whatever overlaps it.
        self._committed_until_ms = start + audio.size / ASR_SAMPLE_RATE * 1000.0
        self._span_open = False
        return out

    def _trim_committed(self, segments: list[StableSegment]) -> list[StableSegment]:
        """Drop/tighten the part of the overlapped region already committed."""
        if self._committed_until_ms < 0:
            self._remember([s.text for s in segments if s.text])
            return segments
        out: list[StableSegment] = []
        for seg in segments:
            if seg.end_ms <= self._committed_until_ms:
                self.dropped_segments += 1  # entirely inside the previous window
                continue
            if seg.start_ms < self._committed_until_ms:
                trimmed_words = [w for w in seg.words if w.get("end_ms", 0) > self._committed_until_ms]
                if seg.words:
                    if not trimmed_words:
                        self.dropped_segments += 1
                        continue
                    seg.text = normalise_text("".join(str(w.get("word", "")) for w in trimmed_words))
                    seg.words = trimmed_words
                    seg.start_ms = int(min(w["start_ms"] for w in trimmed_words))
                    seg.flags.append("overlap_trimmed")
                    self.trimmed_segments += 1
                else:
                    # No word timings available.  If the segment lies mostly in
                    # the already committed region - or repeats text the
                    # previous window already produced - it is a duplicate, so
                    # drop it; otherwise keep the text and only tighten the
                    # start time.  Every drop is counted in ``dropped_segments``
                    # and surfaced in ``conference.status``.
                    midpoint = (seg.start_ms + seg.end_ms) / 2.0
                    if midpoint <= self._committed_until_ms:
                        self.dropped_segments += 1
                        continue
                    if self._is_repeat(seg.text):
                        self.dropped_segments += 1
                        continue
                    seg.start_ms = int(self._committed_until_ms)
                    seg.flags.append("overlap_clamped")
                    self.trimmed_segments += 1
            out.append(seg)
        self._remember([s.text for s in out if s.text])
        return out

    def _is_repeat(self, text: str) -> bool:
        """True when ``text`` was already emitted by a recent window.

        Only consulted for backends that return no word timings *and* for a
        segment that overlaps the already committed region, where an identical
        string is almost certainly the same words decoded twice.  Short texts
        must match exactly; longer ones may also be contained in a previous
        segment (the tail of a sentence cut in half by the window boundary).
        """
        needle = normalise_text(text)
        if not needle:
            return True
        for previous in self._recent_texts:
            if needle == previous:
                return True
            if len(needle) >= 6 and needle in previous:
                return True
        return False

    def _remember(self, texts: list[str]) -> None:
        for text in texts:
            normalized = normalise_text(text)
            if normalized:
                self._recent_texts.append(normalized)
        if len(self._recent_texts) > 8:
            self._recent_texts = self._recent_texts[-8:]

    def _transcribe(self, audio: np.ndarray, start_ms: float) -> list[StableSegment]:
        if audio.size < int(0.15 * ASR_SAMPLE_RATE):
            return []
        try:
            raw = self.backend.transcribe(
                audio, ASR_SAMPLE_RATE, language=self.language, offset_ms=int(round(start_ms))
            )
        except Exception as exc:  # keep the audio pipeline alive
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        out: list[StableSegment] = []
        for seg in raw:
            abs_start = int(round(start_ms + seg.start_ms))
            abs_end = int(round(start_ms + seg.end_ms))
            speaker, conf = self._speaker_for(audio, seg, start_ms, abs_start, abs_end)
            # Word times must be rebased to the same absolute session timeline
            # as the segment, otherwise the overlap trimmer would compare them
            # against the wrong clock and drop real content.
            words = [
                {
                    "start_ms": int(round(start_ms + float(w.get("start_ms", 0)))),
                    "end_ms": int(round(start_ms + float(w.get("end_ms", 0)))),
                    "word": str(w.get("word", "")),
                    "prob": w.get("prob"),
                }
                for w in seg.words
            ]
            stable = StableSegment(
                segment_id=_new_segment_id(),
                start_ms=abs_start,
                end_ms=abs_end,
                text=seg.text,
                speaker_id=speaker,
                is_final=True,
                revision=1,
                confidence=seg.confidence,
                flags=[],
                words=words,
                window_id=self._window_id,
            )
            # Backend-recognised decode problems (a repetition loop that even the
            # temperature fallback could not fix) are *marked*, never rewritten:
            # the decode stays auditable and the reviewer can skip it.
            anomaly_flags = getattr(self.backend, "anomaly_flags", None)
            if callable(anomaly_flags):
                stable.flags.extend(anomaly_flags(seg))
            if conf is not None:
                stable.flags.append(f"speaker_sim={conf}")
            out.append(stable)
        return out

    def _speaker_for(
        self, audio: np.ndarray, seg: AsrSegment, base_ms: float, abs_start: int, abs_end: int
    ) -> tuple[str | None, float | None]:
        if seg.speaker_id:
            return seg.speaker_id, None
        if self.diarizer is None:
            return SPEAKER_UNKNOWN, None
        rel_start = max(0, int((abs_start - base_ms) / 1000.0 * ASR_SAMPLE_RATE))
        rel_end = min(audio.size, int((abs_end - base_ms) / 1000.0 * ASR_SAMPLE_RATE))
        clip = audio[rel_start:rel_end]
        if clip.size == 0:
            clip = audio
        speaker, sim = self.diarizer.assign(
            clip, ASR_SAMPLE_RATE, start_ms=abs_start, end_ms=abs_end, text=seg.text
        )
        return (speaker or SPEAKER_UNKNOWN), sim
