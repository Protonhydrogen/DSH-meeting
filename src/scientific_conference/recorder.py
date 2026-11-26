"""The recording worker: capture, transcribe, segment, persist, emit events.

Threading model (one process per session):

``capture``
    reads the audio source and pushes raw blocks into a bounded queue.  It never
    resamples, writes files or recognises anything.
``convert``
    pops raw blocks, resamples to 16 kHz mono, writes the source-format and
    16 kHz chunk files, and pushes the 16 kHz audio into the ASR queue.
``asr``
    runs the VAD + ASR windowing layer and persists stabilised segments.
``service loop``
    drives the time-based segmenter transitions (turn/talk closing, provisional
    notes), applies control commands and refreshes ``status.json``.

Because the three queues are independent, a slow model never blocks the
microphone: audio keeps being written to disk while a window is being
transcribed.  Queue depths, dropped blocks and audio gaps are all reported in
``conference.status`` instead of being silently discarded.
"""

from __future__ import annotations

import copy
import json
import os
import queue
import threading
import time
import traceback
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import SKILL_VERSION
from .asr import ASR_SAMPLE_RATE, RECORDING_BACKENDS, StreamingTranscriber, build_backend, select_recording_backend
from .asr.qwen import QWEN_BACKEND, QwenTranscriber
from .asr.streaming import StableSegment
from .audio import StreamingResampler, open_source, write_wav_int16
from .audio.sources import AudioSource
from .db import utc_now
from .diarization import SPEAKER_UNKNOWN, load_diarizer
from .events import (
    ASR_AUDIO_GAP,
    ASR_SWITCHED,
    AUDIO_GAP,
    SESSION_CLOSED,
    SESSION_ERROR,
    SESSION_STARTED,
    SPEAKER_UPDATED,
    TALK_CLOSED,
    TRANSCRIPT_REVISED,
    TURN_FINALIZED,
    PROVISIONAL_TICK,
    EventQueue,
)
from .segmenter import Action, Segmenter
from .store import (
    MODE_TALK,
    MODE_TURN,
    STATE_CLOSED,
    STATE_DRAINING,
    STATE_FAILED,
    STATE_RECORDING,
    Store,
)
from .vad import load_vad

__all__ = ["Recorder", "RecorderConfig"]


@dataclass
class RecorderConfig:
    session_id: str
    output_root: Path
    source: str = "microphone"
    wav_path: str | None = None
    realtime: bool = False
    tick_ms: int = 500


class Recorder:
    def __init__(self, cfg, store: Store, rec_cfg: RecorderConfig, *, backend_factory=None):
        self.cfg = cfg
        self.store = store
        self.rec_cfg = rec_cfg
        self.events = EventQueue(store.db)
        self.session_id = rec_cfg.session_id
        self._backend_factory = backend_factory

        self.raw_q: queue.Queue[Any] = queue.Queue(maxsize=max(32, cfg.audio.queue_max_chunks // 4))
        self.asr_q: queue.Queue[Any] = queue.Queue(maxsize=cfg.audio.queue_max_chunks)

        self._stop = threading.Event()
        self._capture_done = threading.Event()
        self._convert_done = threading.Event()
        self._stop_reason = "manual"
        self._state_lock = threading.RLock()
        self._threads: list[threading.Thread] = []
        self._source: AudioSource | None = None
        self._resampler: StreamingResampler | None = None
        self._segmenter = Segmenter(cfg.segmenter)
        self._backend = None
        self._glossary_profile = cfg.asr.glossary_profile
        self._pending_profile = None
        self._pending_asr = None
        self._asr_switch = {}
        if self._glossary_profile and self._glossary_profile != "general":
            from .glossary import profile
            selected = profile(self._glossary_profile)
            self.store.set_speaker('glossary_' + selected['id'], selected['speaker'], 'manual')
        self._diarizer = None
        self._transcriber: StreamingTranscriber | QwenTranscriber | None = None
        self._frames_read = 0
        self._frames_converted = 0
        self._chunk_index = 0
        self._chunk_buf_src: list[np.ndarray] = []
        self._chunk_buf_16k: list[np.ndarray] = []
        self._chunk_buf_frames = 0
        self._chunk_start_ms = 0
        self._last_chunk_end_ms = 0
        self._totals = {"segments": 0, "turns": 0, "talks": 0, "events": 0, "gaps": 0, "dropped": 0}
        self._last_gap_ms = 0
        self._asr_clock_ms = 0
        self._asr_error: str | None = None
        self._status_error: str | None = None
        self.backlog: dict[str, int] = {"raw": 0, "asr": 0}
        self.last_error: str | None = None
        self.startup_phase = "opening_system_audio" if rec_cfg.source == "system" else "opening_microphone"

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def run(self) -> int:
        """Run until the source ends or a stop command arrives."""
        assert self.store.paths is not None
        try:
            self._setup()
            self.startup_phase = "starting_capture"
            self._source.start()
        except Exception as exc:
            self._fatal(exc)
            return 1

        self.startup_phase = "ready"
        self.store.set_state(STATE_RECORDING)
        self.store.set_pid(_current_pid())
        self.events.append(
            self.session_id,
            SESSION_STARTED,
            {
                "device": self._source.name if self._source else None,
                "sample_rate": self._source.sample_rate if self._source else None,
                "channels": self._source.channels if self._source else None,
                "asr": self._backend.info() if self._backend else None,
                "diarization": self._diarizer.info() if self._diarizer else None,
                "skill_version": SKILL_VERSION,
                "audio_dir": str(self.store.paths.audio),
            },
            dedupe_key="session.started",
        )
        self.store.log("info", "recording started")

        self._start_threads()
        last_status = 0.0
        try:
            while not self._stop.is_set():
                self._apply_control()
                with self._state_lock:
                    # The boundary clock follows the audio the ASR has actually
                    # consumed, not the capture position: otherwise a lagging
                    # transcriber would look like silence.
                    capture_ms = self._session_ms()
                    now_ms = min(capture_ms, max(self._asr_clock_ms, 0))
                    speech_active = bool(self._transcriber and self._transcriber.span_open)
                    actions = self._segmenter.tick(now_ms, speech_active=speech_active)
                    if actions:
                        # Kept for tuning: the boundary rules are explicitly
                        # "to be calibrated on real recordings".
                        self.store.log(
                            "debug",
                            "boundary decision",
                            {
                                "now_ms": now_ms,
                                "capture_ms": capture_ms,
                                "asr_ms": self._asr_clock_ms,
                                "speech_active": speech_active,
                                "turn_end_ms": (
                                    self._segmenter.current_turn.end_ms if self._segmenter.current_turn else None
                                ),
                                "actions": [a.to_dict() for a in actions],
                            },
                        )
                    for action in actions:
                        self._apply_action(action)
                status_interval = 0.5 if self._backend.name == QWEN_BACKEND or self._pending_asr else 2.0
                if time.monotonic() - last_status >= status_interval:
                    self._write_status()
                    last_status = time.monotonic()
                time.sleep(self.rec_cfg.tick_ms / 1000.0)
        except KeyboardInterrupt:  # pragma: no cover
            self._stop_reason = "interrupt"
        finally:
            self._shutdown()

        self.store.write_manifest()
        return 0 if self.last_error is None else 1

    def _session_ms(self) -> int:
        sr = self._source.sample_rate if self._source else 16000
        return int(self._frames_converted / sr * 1000)

    def request_stop(self, reason: str = "manual") -> None:
        self._stop_reason = reason
        self._stop.set()

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------
    def _setup(self) -> None:
        cfg = self.cfg
        self._write_status()
        source = open_source(cfg, source=self.rec_cfg.source, wav_path=self.rec_cfg.wav_path, realtime=self.rec_cfg.realtime)
        self._source = source
        # Fail before loading ASR, and never advertise recording until start succeeds.
        source.prepare()
        self.startup_phase = "loading_models"
        self._write_status()
        self._resampler = StreamingResampler(source.sample_rate, 16000)
        self._backend = self._backend_factory() if self._backend_factory else build_backend(cfg)
        self._diarizer = load_diarizer(cfg)
        self._transcriber = self._new_transcriber(self._backend, 0)
        self.store.set_control(
            sample_rate=source.sample_rate,
            channels=source.channels,
            device_name=source.name,
        )

    def _new_transcriber(self, backend, offset_ms):
        cfg = self.cfg
        gate = load_vad(cfg)
        for note in getattr(gate, "fallback_notes", []):
            self.store.log("warning", note)
        if backend.name == QWEN_BACKEND:
            return QwenTranscriber(
                backend, gate, session_offset_ms=offset_ms,
                max_window_ms=round(cfg.asr.qwen.max_utterance_seconds * 1000),
                preroll_ms=max(500, cfg.vad.min_speech_ms + cfg.vad.pad_ms + 100),
                end_hold_ms=max(0, gate.min_silence_ms - gate.pad_ms) + gate.frame_samples * 1000 / ASR_SAMPLE_RATE,
                diarizer=self._diarizer,
            )
        return StreamingTranscriber(
            backend, gate, session_offset_ms=offset_ms,
            max_window_ms=cfg.asr.max_window_ms,
            overlap_ms=max(1000, cfg.vad.pad_ms * 4),
            min_segment_ms=cfg.asr.min_segment_ms,
            preroll_ms=max(300, cfg.vad.pad_ms + 200),
            diarizer=self._diarizer,
        )

    def _start_threads(self) -> None:
        self._threads = [
            threading.Thread(target=self._capture_loop, name="capture", daemon=True),
            threading.Thread(target=self._convert_loop, name="convert", daemon=True),
            threading.Thread(target=self._asr_loop, name="asr", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    # ------------------------------------------------------------------
    # threads
    # ------------------------------------------------------------------
    def _capture_loop(self) -> None:
        assert self._source is not None
        try:
            while not self._stop.is_set():
                try:
                    block = self._source.read(timeout=0.5)
                except TimeoutError:
                    continue
                if block is None:
                    self.request_stop("source_eof")
                    break
                self._frames_read += block.shape[0]
                try:
                    self.raw_q.put(block, timeout=1.0)
                except queue.Full:
                    self._totals["dropped"] += 1
                    self.store.log("error", "raw audio queue full; block dropped")
        except Exception as exc:  # pragma: no cover - device dependent
            self.last_error = f"capture failed: {type(exc).__name__}: {exc}"
            self.store.log("error", self.last_error, {"trace": traceback.format_exc()[-2000:]})
            self.request_stop("capture_error")
        finally:
            try:
                self._source.stop()
            except Exception as exc:
                self.last_error = self.last_error or f"audio cleanup failed: {exc}"
                self.request_stop("capture_error")
            finally:
                self._capture_done.set()

    def _convert_loop(self) -> None:
        assert self._source is not None and self._resampler is not None
        sr = self._source.sample_rate
        try:
            while True:
                try:
                    block = self.raw_q.get(timeout=0.5)
                except queue.Empty:
                    if self._capture_done.is_set() and self.raw_q.empty():
                        break
                    continue
                start_ms = int(self._frames_converted / sr * 1000)
                mono = block if block.ndim == 1 else block.mean(axis=1)
                mono_f = mono.astype(np.float32) / 32768.0
                resampled = self._resampler.process(mono_f)
                self._frames_converted += block.shape[0]
                end_ms = int(self._frames_converted / sr * 1000)

                self._chunk_buf_src.append(np.asarray(block, dtype=np.int16))
                if resampled.size:
                    self._chunk_buf_16k.append((np.clip(resampled, -1, 1) * 32767).astype(np.int16))
                self._chunk_buf_frames += block.shape[0]
                if self._chunk_buf_frames >= sr * self.cfg.audio.chunk_seconds:
                    self._flush_chunk(end_ms)

                if resampled.size:
                    self._queue_asr_audio(start_ms, end_ms, resampled)
        except Exception as exc:  # pragma: no cover
            self.last_error = f"convert failed: {type(exc).__name__}: {exc}"
            self.store.log("error", self.last_error, {"trace": traceback.format_exc()[-2000:]})
            self.request_stop("convert_error")
        finally:
            self._convert_done.set()

    def _flush_chunk(self, end_ms: int) -> None:
        assert self.store.paths is not None
        if not self._chunk_buf_src:
            return
        self._chunk_index += 1
        idx = self._chunk_index
        src_path = self.store.paths.audio / f"chunk_{idx:05d}.wav"
        conv_path = self.store.paths.converted / f"chunk_{idx:05d}.wav"
        sr = self._source.sample_rate if self._source else 16000
        ch = self._source.channels if self._source else 1
        src_data = np.concatenate(self._chunk_buf_src, axis=0)
        write_wav_int16(src_path, src_data, sr)
        conv = np.concatenate(self._chunk_buf_16k) if self._chunk_buf_16k else np.zeros(0, dtype=np.int16)
        if conv.size:
            write_wav_int16(conv_path, conv, 16000)
        gap_ms = max(0, self._chunk_start_ms - self._last_chunk_end_ms)
        self.store.add_chunk(
            idx,
            path=self._relative(src_path),
            converted_path=self._relative(conv_path) if conv.size else None,
            start_ms=self._chunk_start_ms,
            end_ms=end_ms,
            frames=int(src_data.shape[0]),
            sample_rate=sr,
            channels=ch,
            nbytes=int(src_path.stat().st_size),
            gap_before_ms=gap_ms,
        )
        self._last_chunk_end_ms = end_ms
        self._chunk_start_ms = end_ms
        self._chunk_buf_src = []
        self._chunk_buf_16k = []
        self._chunk_buf_frames = 0

    def _relative(self, path: Path) -> str:
        assert self.store.paths is not None
        try:
            return str(path.relative_to(self.store.paths.root))
        except ValueError:
            return str(path)

    def _queue_asr_audio(self, start_ms, end_ms, audio):
        item = (start_ms, end_ms, audio)
        live_qwen = self._backend.name == QWEN_BACKEND and (
            self.rec_cfg.source != "file" or self.rec_cfg.realtime
        )
        try:
            if live_qwen:
                # A slow inference service must never hold up live WAV persistence.
                self.asr_q.put_nowait(item)
            else:
                self.asr_q.put(item, timeout=5.0)
        except queue.Full:
            self._totals["dropped"] += 1
            self.store.log("error", "ASR queue full; audio not transcribed",
                           {"start_ms": start_ms, "end_ms": end_ms})

    def _restore_asr_clock(self, start_ms):
        if self._backend.name != QWEN_BACKEND:
            return
        previous = self._transcriber.stream_ms
        # The resampler holds a sub-millisecond filter tail in normal operation.
        if start_ms - previous <= 2:
            return
        for segment in self._transcriber.flush():
            with self._state_lock:
                self._place_segment(segment)
        with self._state_lock:
            self._transcriber = self._new_transcriber(self._backend, start_ms)
            self.events.append(self.session_id, ASR_AUDIO_GAP, {
                "start_ms": round(previous), "end_ms": start_ms, "backend": self._backend.name,
            })

    def _asr_loop(self) -> None:
        assert self._transcriber is not None
        try:
            while True:
                try:
                    item = self.asr_q.get(timeout=0.5)
                except queue.Empty:
                    if self._convert_done.is_set() and self.asr_q.empty():
                        break
                    if not self._stop.is_set():
                        self._apply_pending_asr()
                    continue
                start_ms, _, audio = item
                self._restore_asr_clock(start_ms)
                if not self._stop.is_set():
                    self._apply_pending_asr()
                self._apply_pending_profile()
                segments = self._transcriber.push(audio)
                with self._state_lock:
                    for seg in segments:
                        self._place_segment(seg)
                    # The boundary clock may only move past audio whose
                    # segments are already stored.  Publishing it earlier would
                    # make the ticker treat freshly transcribed speech as
                    # silence and cut the turn in half.
                    self._asr_clock_ms = int(self._transcriber.stream_ms)
                    error = self._transcriber.last_error
                    if error and error != self._asr_error:
                        self._asr_error = error
                        self.store.log("warning", f"transcription error: {error}")
                    elif not error:
                        self._asr_error = None
        except Exception as exc:  # pragma: no cover
            self.last_error = f"asr failed: {type(exc).__name__}: {exc}"
            self.store.log("error", self.last_error, {"trace": traceback.format_exc()[-2000:]})
            self.request_stop("asr_error")

    # ------------------------------------------------------------------
    # segment persistence
    # ------------------------------------------------------------------
    def _place_segment(self, seg: StableSegment) -> None:
        if self._glossary_profile and self._glossary_profile != 'general':
            seg.speaker_id = 'glossary_' + self._glossary_profile
        placement = self._segmenter.add_segment(
            start_ms=seg.start_ms,
            end_ms=seg.end_ms,
            speaker_id=seg.speaker_id,
            text=seg.text,
            segment_id=seg.segment_id,
        )
        for action in placement.actions:
            self._apply_action(action)
        flags = list(seg.flags)
        if placement.speaker_id != seg.speaker_id and placement.speaker_id not in (None, SPEAKER_UNKNOWN):
            flags.append("backchannel")
        self.store.add_segment(
            seg.segment_id,
            start_ms=seg.start_ms,
            end_ms=seg.end_ms,
            text=seg.text,
            turn_id=placement.turn_id,
            talk_id=placement.talk_id,
            is_final=seg.is_final,
            revision=self._current_turn_revision(),
            speaker_id=seg.speaker_id,
            asr_confidence=seg.confidence,
            flags=flags,
            backend=self._backend.name if self._backend else None,
            window_id=seg.window_id,
        )
        self._totals["segments"] += 1

    def _current_turn_revision(self) -> int:
        turn = self._segmenter.current_turn
        return turn.revision if turn else 1

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def _apply_action(self, action: Action) -> None:
        if action.kind == "open_talk":
            self.store.open_talk(action.talk_id or "", action.start_ms or 0, action.speaker_id)
            self._totals["talks"] += 1
        elif action.kind == "open_turn":
            self.store.open_turn(
                action.turn_id or "", action.start_ms or 0,
                talk_id=action.talk_id, speaker_id=action.speaker_id,
            )
        elif action.kind == "finalize_turn":
            self._finalize_turn(action)
        elif action.kind == "close_talk":
            self._close_talk(action)
        elif action.kind == "provisional":
            self._emit_provisional(action)

    def _finalize_turn(self, action: Action) -> None:
        turn_id = action.turn_id or ""
        end_ms = int(action.end_ms or 0)
        revision = self.store.turn(turn_id)
        rev = int(revision["revision"]) if revision else 1
        self.store.update_turn(
            turn_id,
            end_ms=end_ms,
            state="finalized",
            boundary_reason=action.reason,
            speaker_id=action.speaker_id,
        )
        payload = self.turn_payload(turn_id, revision=rev, boundary_reason=action.reason)
        self.events.append(
            self.session_id,
            TURN_FINALIZED,
            payload,
            dedupe_key=f"turn.finalized:{turn_id}:r{rev}",
        )
        self._totals["turns"] += 1
        self.store.log("info", f"turn finalized {turn_id}", {"reason": action.reason, "end_ms": end_ms})

    def _close_talk(self, action: Action) -> None:
        talk_id = action.talk_id or ""
        end_ms = int(action.end_ms or 0)
        talk = self.store.talk(talk_id)
        rev = int(talk["revision"]) if talk else 1
        self.store.close_talk(talk_id, end_ms, speaker_id=action.speaker_id)
        segments = self.store.segments(talk_id=talk_id)
        payload = {
            "schema_version": "1.0",
            "talk_id": talk_id,
            "speaker_id": action.speaker_id,
            "revision": rev,
            "start_ms": int(talk["start_ms"]) if talk else None,
            "end_ms": end_ms,
            "boundary_reason": action.reason,
            "turn_ids": sorted({s["turn_id"] for s in segments if s["turn_id"]}),
            "segment_count": len(segments),
        }
        self.events.append(self.session_id, TALK_CLOSED, payload, dedupe_key=f"talk.closed:{talk_id}:r{rev}")
        self.store.log("info", f"talk closed {talk_id}", {"reason": action.reason})

    def _emit_provisional(self, action: Action) -> None:
        payload = self.turn_payload(action.turn_id or "", revision=action.revision, boundary_reason="provisional")
        payload["provisional"] = True
        payload["end_ms"] = int(action.end_ms or max((s['end_ms'] for s in payload['segments']), default=0))
        self.events.append(
            self.session_id,
            PROVISIONAL_TICK,
            payload,
            dedupe_key=f"turn.provisional:{action.turn_id}:{int(action.end_ms or 0)}",
        )

    # ------------------------------------------------------------------
    def turn_payload(self, turn_id: str, *, revision: int, boundary_reason: str) -> dict[str, Any]:
        turn = self.store.turn(turn_id) or {}
        segments = self.store.segments(turn_id=turn_id)
        return {
            "schema_version": "1.0",
            "turn_id": turn_id,
            "talk_id": turn.get("talk_id"),
            "speaker_id": turn.get("speaker_id") or SPEAKER_UNKNOWN,
            "speaker_display": self.store.speaker_name(turn.get("speaker_id")),
            "revision": revision,
            "start_ms": int(turn.get("start_ms") or 0),
            "end_ms": int(turn.get("end_ms") or 0),
            "boundary_reason": boundary_reason,
            "segment_count": len(segments),
            "segments": [
                {
                    "segment_id": s["segment_id"],
                    "start_ms": s["start_ms"],
                    "end_ms": s["end_ms"],
                    "text": s["text"],
                    "is_final": bool(s["is_final"]),
                    "speaker_id": s["speaker_id"],
                    "asr_confidence": s["asr_confidence"],
                    "flags": json.loads(s["flags"]) if s["flags"] else [],
                }
                for s in segments
            ],
        }

    # ------------------------------------------------------------------
    # control channel
    # ------------------------------------------------------------------
    def _apply_control(self) -> None:
        for row in self.store.pop_control():
            cmd = row["command"]
            args = json.loads(row["args"] or "{}")
            try:
                if cmd == "stop":
                    self.store.resolve_control(row["id"], "applied", {"state": "stopping"})
                    self.request_stop("manual")
                    return
                if cmd == "boundary":
                    kind = args.get("kind", "turn")
                    with self._state_lock:
                        boundary_ms = min(self._session_ms(), max(self._asr_clock_ms, 0))
                        actions = self._segmenter.manual_boundary(kind, boundary_ms, args.get("speaker_id"))
                        for action in actions:
                            self._apply_action(action)
                    self.store.resolve_control(row["id"], "applied", {"kind": kind})
                elif cmd == 'summary_interval':
                    seconds=args.get('seconds')
                    if type(seconds) is not int or not 10<=seconds<=300:
                        raise ValueError('Summary interval must be 10–300 seconds')
                    with self._state_lock:
                        self.cfg.segmenter.provisional_interval_ms=seconds*1000
                        self._segmenter.cfg.provisional_interval_ms=seconds*1000
                        with self.store.db.tx() as conn:
                            conn.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('summary_interval_ms',?)",(str(seconds*1000),))
                    self.store.resolve_control(row['id'],'applied',{'seconds':seconds})
                elif cmd == "asr_switch":
                    self._queue_asr_switch(row["id"], args.get("backend"))
                elif cmd == "profile":
                    from .glossary import profile
                    selected = profile(args.get('profile_id', 'general'))
                    with self._state_lock:
                        if self._pending_profile is not None:
                            raise ValueError('A speaker switch is already pending')
                        self._pending_profile = (row['id'], selected)
                elif cmd == "speaker":
                    self.set_speaker(args.get("speaker_id", ""), args.get("display_name"), args.get("source", "manual"))
                    self.store.resolve_control(row["id"], "applied", {})
                else:
                    self.store.resolve_control(row["id"], "rejected", {"error": f"unknown command {cmd}"})
            except Exception as exc:
                self.store.log("error", f"control {cmd} failed: {exc}")
                self.store.resolve_control(row["id"], "rejected", {"error": str(exc)})

    def _queue_asr_switch(self, control_id, backend):
        with self._state_lock:
            if backend not in RECORDING_BACKENDS or self._backend.name not in RECORDING_BACKENDS:
                raise ValueError("Unsupported live transcription model")
            if self._pending_asr:
                if self._pending_asr[0] != control_id:
                    raise ValueError("A transcription model switch is already pending")
                return
            if backend == self._backend.name:
                self.store.resolve_control(control_id, "applied", {"backend": backend})
                return
            cfg = copy.deepcopy(self.cfg)
            select_recording_backend(cfg, backend)
            future = Future()
            self._pending_asr = (control_id, backend, future)
            self._asr_switch = {"state": "preparing", "backend": backend}

        def prepare():
            try:
                future.set_result(build_backend(cfg))
            except Exception as exc:
                future.set_exception(exc)

        threading.Thread(target=prepare, name="asr-prepare", daemon=True).start()

    def _apply_pending_asr(self):
        # The ASR consumer owns the handoff. Capture and audio saving continue.
        with self._state_lock:
            if not self._pending_asr:
                return
            control_id, backend_name, future = self._pending_asr
            if not future.done():
                return
            backend = None
            try:
                backend = future.result()
                old_stream = self._transcriber
                if old_stream.span_open or not old_stream.gate.quiet_boundary:
                    self._asr_switch = {"state": "waiting_boundary", "backend": backend_name}
                    return
                # A quiet gate can still have an incomplete frame. Transfer those
                # exact samples so a new utterance can start inside that frame.
                tail = old_stream.gate.unclassified_audio
                position = old_stream.stream_ms - tail.size * 1000 / ASR_SAMPLE_RATE
                new_stream = self._new_transcriber(backend, position)
                seeded_segments = new_stream.push(tail) if tail.size else []
                if backend_name == "faster_whisper" and self._glossary_profile:
                    from .glossary import prompt
                    backend.initial_prompt = prompt(self._glossary_profile)
                details = {
                    "previous_backend": self._backend.name, **backend.info(),
                    "effective_ms": round(position),
                }
                self.events.append(self.session_id, ASR_SWITCHED, details,
                                   dedupe_key=f"asr.switched:{control_id}")
                self.store.resolve_control(control_id, "applied", details)
            except Exception as exc:
                if backend is not None:
                    backend.close()
                self._asr_switch = {"state": "rejected", "backend": backend_name, "error": str(exc)}
                self.store.resolve_control(control_id, "rejected", {"error": str(exc)})
                self.store.log("warning", f"transcription model switch failed: {exc}")
            else:
                old_backend = self._backend
                self._backend, self._transcriber = backend, new_stream
                self._asr_switch = {"state": "applied", **details}
                old_backend.close()
                for segment in seeded_segments:
                    self._place_segment(segment)
            self._pending_asr = None

    def _apply_pending_profile(self):
        # Called only by the ASR worker, so old buffered audio keeps its old prompt.
        with self._state_lock:
            pending = self._pending_profile
            self._pending_profile = None
        if pending is None:
            return
        control_id, selected = pending
        for seg in self._transcriber.flush():
            with self._state_lock:
                self._place_segment(seg)
        from .glossary import prompt
        with self._state_lock:
            now = int(self._transcriber.stream_ms)
            for action in self._segmenter.manual_boundary('talk', now):
                self._apply_action(action)
            self._glossary_profile = selected['id']
            if hasattr(self._backend, "initial_prompt"):
                self._backend.initial_prompt = prompt(selected['id'])
            if selected['id'] != 'general':
                self.store.set_speaker('glossary_' + selected['id'], selected['speaker'], 'manual')
            self.store.resolve_control(control_id, 'applied', {'profile_id':selected['id'], 'effective_ms':now})

    def set_speaker(self, speaker_id: str, display_name: str | None, source: str = "manual") -> list[str]:
        """Update a speaker mapping and mark affected turns for re-note."""
        self.store.set_speaker(speaker_id, display_name, source)
        affected: list[str] = []
        with self._state_lock:
            for turn in self.store.turns():
                if turn["speaker_id"] == speaker_id:
                    rev = self.store.bump_turn_revision(turn["turn_id"])
                    if turn["turn_id"] == (self._segmenter.current_turn.turn_id if self._segmenter.current_turn else None):
                        assert self._segmenter.current_turn is not None
                        self._segmenter.current_turn.revision = rev
                    payload = self.turn_payload(turn["turn_id"], revision=rev, boundary_reason="speaker_mapping")
                    payload["revision_reason"] = "speaker_mapping"
                    self.events.append(
                        self.session_id,
                        TRANSCRIPT_REVISED,
                        payload,
                        dedupe_key=f"transcript.revised:{turn['turn_id']}:r{rev}",
                    )
                    affected.append(turn["turn_id"])
        self.events.append(
            self.session_id,
            SPEAKER_UPDATED,
            {"speaker_id": speaker_id, "display_name": display_name, "source": source, "turns": affected},
            dedupe_key=f"speaker.updated:{speaker_id}:{utc_now()}",
        )
        return affected

    # ------------------------------------------------------------------
    # shutdown
    # ------------------------------------------------------------------
    def _shutdown(self) -> None:
        self._stop.set()
        self.store.set_state(STATE_DRAINING)
        self.store.log("info", f"draining ({self._stop_reason})")
        deadline = time.monotonic() + 180.0
        for thread in self._threads:
            remaining = max(0.5, deadline - time.monotonic())
            thread.join(timeout=remaining)
        alive = [t.name for t in self._threads if t.is_alive()]
        if alive:
            self.last_error = self.last_error or f"threads still running at stop: {alive}"
            self.store.log("error", self.last_error)

        try:
            self._flush_chunk(self._session_ms())
            if self._transcriber is not None:
                with self._state_lock:
                    for seg in self._transcriber.flush():
                        self._place_segment(seg)
                    for action in self._segmenter.force_close(0):
                        self._apply_action(action)
        except Exception as exc:  # pragma: no cover
            self.last_error = f"drain failed: {type(exc).__name__}: {exc}"
            self.store.log("error", self.last_error, {"trace": traceback.format_exc()[-2000:]})

        if self._pending_asr:
            control_id, backend_name, future = self._pending_asr
            self._pending_asr = None
            self._asr_switch = {"state": "rejected", "backend": backend_name, "error": "Recording stopped"}
            self.store.resolve_control(control_id, "rejected", {"error": "Recording stopped"})

            def close_unused(prepared):
                if not prepared.exception():
                    prepared.result().close()

            future.add_done_callback(close_unused)
        if self._backend:
            self._backend.close()

        gaps = self._source.status.get("gap_ms", 0) if self._source else 0
        if gaps:
            self.events.append(self.session_id, AUDIO_GAP, {"gap_ms": gaps}, dedupe_key=f"audio.gap:{gaps}")

        stats = self.audio_stats()
        self.events.append(
            self.session_id,
            SESSION_CLOSED,
            {
                "reason": self._stop_reason,
                "duration_ms": stats["duration_ms"],
                "counts": self._totals,
                "backlog": dict(self.backlog),
                "errors": self.last_error,
            },
            dedupe_key="session.closed",
        )
        if self.last_error:
            self.events.append(
                self.session_id, SESSION_ERROR, {"error": self.last_error}, dedupe_key="session.error"
            )
        self.store.export_transcript_jsonl()
        self.store.export_events_jsonl()
        self.store.set_state(STATE_FAILED if self.last_error else STATE_CLOSED, error=self.last_error)
        self.store.set_pid(None)
        self._write_status()
        self.store.log("info", "session closed", stats)

    def _fatal(self, exc: Exception) -> None:
        self.last_error = f"{type(exc).__name__}: {exc}"
        if self._source is not None:
            try:
                self._source.stop()
            except Exception:
                pass
        try:
            self.store.log("error", self.last_error, {"trace": traceback.format_exc()[-4000:]})
            self.events.append(self.session_id, SESSION_ERROR, {"error": self.last_error}, dedupe_key="session.error")
            self.store.set_state(STATE_FAILED, error=self.last_error)
            self.store.set_pid(None)
            self._write_status()
            self.store.write_manifest()
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def audio_stats(self) -> dict[str, Any]:
        stats = self.store.audio_stats()
        if self._source is not None:
            stats["source_blocks"] = self._source.status.get("blocks", 0)
            stats["source_overflows"] = self._source.status.get("overflows", 0)
            stats["source_gap_ms"] = self._source.status.get("gap_ms", 0)
        stats["dropped_blocks"] = self._totals["dropped"]
        return stats

    def write_status(self) -> None:
        self._write_status()

    def _write_status(self) -> None:
        assert self.store.paths is not None
        self.backlog = {"raw": self.raw_q.qsize(), "asr": self.asr_q.qsize()}
        status = {
            "startup_phase": self.startup_phase,
            "schema_version": "1.0",
            "session_id": self.session_id,
            "state": self.store.session_state(),
            "updated_utc": utc_now(),
            "pid": _current_pid(),
            "stop_reason": self._stop_reason if self._stop.is_set() else None,
            "audio": self.audio_stats(),
            "backlog": dict(self.backlog),
            "clocks": {"capture_ms": self._session_ms(), "asr_ms": self._asr_clock_ms},
            "totals": dict(self._totals),
            "glossary_profile": self._glossary_profile,
            "transcriber": {
                "segments_emitted": self._transcriber.segments_emitted if self._transcriber else 0,
                "overlap_trimmed": self._transcriber.trimmed_segments if self._transcriber else 0,
                "overlap_dropped": self._transcriber.dropped_segments if self._transcriber else 0,
                "last_error": self._transcriber.last_error if self._transcriber else None,
            },
            "open_turn": (
                self._segmenter.current_turn.turn_id if self._segmenter.current_turn else None
            ),
            "open_talk": (self._segmenter.current_talk.talk_id if self._segmenter.current_talk else None),
            "diarization": self._diarizer.info() if self._diarizer else None,
            "asr": self._backend.info() if self._backend else None,
            "asr_partial": getattr(self._transcriber, "partial", ""),
            "asr_switch": self._asr_switch,
            "last_error": self.last_error,
            "tasks": self.store.task_counts(),
            "queues": {"raw_max": self.raw_q.maxsize, "asr_max": self.asr_q.maxsize},
        }
        try:
            path = self.store.paths.root / "status.json"
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            self._status_error = None
        except Exception as exc:  # pragma: no cover - disk/permission problem
            message = f"{type(exc).__name__}: {exc}"
            if message != self._status_error:
                self._status_error = message
                self.store.log("warning", f"could not write status.json: {message}")


def _current_pid() -> int:
    import os

    return os.getpid()
