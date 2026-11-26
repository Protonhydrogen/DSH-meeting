"""Realtime translation of committed transcript segments.

Design
------
Translation is a **separate worker process**, exactly like the notes runner:

* it reads the session database (SQLite WAL allows a concurrent reader);
* it never touches the recorder, so a slow engine cannot delay the microphone;
* results go into ``segment_translations`` and are announced with a
  ``segment.translated`` event, so the CLI, the live dashboard and any MCP
  client can show them.

Backends (``translation.backend``)
----------------------------------
``whisper_translate``
    复用本机 faster-whisper 的 ``task="translate"``。**离线、零新增依赖**（模型已经
    为 ASR 下载好），代价是每个片段重跑一次语音识别，且 **只能译成英文**。
``openai_compatible``
    文本翻译，任意 ``/chat/completions`` 端点；需要密钥并显式打开
    ``privacy.allow_cloud_notes``（会把转写文本发到远端）。
``cli``
    文本翻译，调用本机 agent CLI。
``none``
    关闭。

``whisper_translate`` 需要音频，因此它从 ``audio/asr16k/`` 的 16 kHz 分块里按时间
切片；刚提交的片段若其音频块尚未落盘，会等待下一轮（不会静默跳过）。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from .audio import read_wav
from .db import utc_now
from .events import SEGMENT_TRANSLATED, EventQueue
from .store import STATE_CLOSED, STATE_FAILED, Store

__all__ = [
    "TranslationResult",
    "Translator",
    "WhisperTranslateTranslator",
    "OpenAiTranslateTranslator",
    "CliTranslateTranslator",
    "MockTranslator",
    "build_translator",
    "TranslationWorker",
    "TranslationState",
    "read_translation_state",
    "write_translation_state",
]

#: How much audio padding to give the recogniser on each side of a segment.
AUDIO_PAD_MS = 150


@dataclass
class TranslationResult:
    text: str
    state: str = "ok"  # ok | skipped | error
    error: str | None = None
    source_lang: str | None = None
    confidence: float | None = None
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "state": self.state,
            "error": self.error,
            "source_lang": self.source_lang,
            "confidence": self.confidence,
            "model": self.model,
        }


# --------------------------------------------------------------------------
# language helpers
# --------------------------------------------------------------------------
def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff")
    letters = sum(1 for ch in text if ch.isalpha() or "\u4e00" <= ch <= "\u9fff")
    return cjk / max(1, letters)


def language_bucket(text: str) -> str:
    """Coarse language guess used only to skip pointless work."""
    return "zh" if _cjk_ratio(text) >= 0.2 else "latin"


def _same_language(text: str, target: str) -> bool:
    target = (target or "").lower()
    if target.startswith("zh") or target in ("chinese", "cmn"):
        return language_bucket(text) == "zh"
    if target.startswith("en") or target in ("english", "eng"):
        return language_bucket(text) == "latin"
    return False


_LANGUAGE_ALIASES = {
    "eng": "en",
    "english": "en",
    "cmn": "zh",
    "chi": "zh",
    "zho": "zh",
    "chinese": "zh",
    "jpn": "ja",
    "japanese": "ja",
}


def language_code_matches(code: str | None, target: str | None) -> bool:
    """Compare two language codes loosely (``eng`` == ``en`` == ``english``)."""
    if not code or not target:
        return False
    left = _LANGUAGE_ALIASES.get(code.lower(), code.lower())[:2]
    right = _LANGUAGE_ALIASES.get(target.lower(), target.lower())[:2]
    return left == right


# --------------------------------------------------------------------------
# translators
# --------------------------------------------------------------------------
class Translator:
    name = "none"
    #: Whether this backend needs the audio of the segment (speech translation).
    needs_audio = False

    def translate(self, segment: dict[str, Any], audio: np.ndarray | None = None) -> TranslationResult:
        raise NotImplementedError

    def info(self) -> dict[str, Any]:
        return {"backend": self.name}

    def close(self) -> None:
        return None


class NoneTranslator(Translator):
    name = "none"

    def translate(self, segment: dict[str, Any], audio: np.ndarray | None = None) -> TranslationResult:
        return TranslationResult(text="", state="skipped", error="translation is disabled")


class MockTranslator(Translator):
    """Deterministic backend for tests: prefixes the original text."""

    name = "mock"

    def __init__(self, prefix: str = "[en] "):
        self.prefix = prefix
        self.calls: list[dict[str, Any]] = []

    def translate(self, segment: dict[str, Any], audio: np.ndarray | None = None) -> TranslationResult:
        self.calls.append({"segment_id": segment.get("segment_id"), "audio_samples": 0 if audio is None else int(audio.size)})
        return TranslationResult(text=f"{self.prefix}{segment.get('text', '')}", model="mock")


class WhisperTranslateTranslator(Translator):
    """Speech translation with the already-installed faster-whisper model."""

    name = "whisper_translate"
    needs_audio = True

    def __init__(self, cfg):
        from .asr.faster_whisper_backend import (
            FasterWhisperBackend,
            _ensure_cuda_dlls,
            resolve_local_model,
        )

        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # pragma: no cover - depends on install
            raise RuntimeError(
                "faster-whisper is not installed; whisper_translate 不可用，请改用 openai_compatible/cli"
            ) from exc

        _ensure_cuda_dlls()
        settings = cfg.translation.whisper
        model_name = settings.model or cfg.asr.model
        self.model_name = model_name
        self.beam_size = int(settings.beam_size or 1)
        self.source_language = cfg.translation.source_language or None
        path = resolve_local_model(model_name, cfg.models_dir, cfg.cache_dir)
        problems: list[str] = []
        for device, compute_type in FasterWhisperBackend._candidate_settings(settings.device, settings.compute_type):
            try:
                kwargs: dict[str, Any] = {"device": device, "compute_type": compute_type}
                if path is not None:
                    kwargs["local_files_only"] = True
                self.model = WhisperModel(str(path or model_name), **kwargs)
                # Warm up: loading does not touch cuBLAS/cuDNN, inference does.
                list(self.model.transcribe(np.zeros(8000, dtype=np.float32), task="translate")[0])
                self.device, self.compute_type = device, compute_type
                break
            except Exception as exc:  # pragma: no cover - hardware dependent
                problems.append(f"{device}/{compute_type}: {type(exc).__name__}: {exc}")
        else:  # pragma: no cover
            raise RuntimeError("whisper_translate 无法加载模型: " + "; ".join(problems))
        self.load_notes = problems
        # Whisper translates each window in isolation, which is why short
        # segments lose their context.  Feeding the previous translation of the
        # same turn back as ``initial_prompt`` measurably stabilises terminology
        # ("C 反应蛋白" stays "C-reactive protein" instead of drifting).
        self._context = ""
        self._last_turn: str | None = None

    def translate(self, segment: dict[str, Any], audio: np.ndarray | None = None) -> TranslationResult:
        if audio is None or audio.size == 0:
            return TranslationResult(text="", state="error", error="没有可用音频")
        turn_id = str(segment.get("turn_id") or "")
        if turn_id != self._last_turn:
            self._context = ""
            self._last_turn = turn_id
        segments, info = self.model.transcribe(
            np.asarray(audio, dtype=np.float32).ravel(),
            task="translate",
            language=self.source_language,
            beam_size=self.beam_size,
            vad_filter=False,
            condition_on_previous_text=False,
            initial_prompt=self._context or None,
        )
        text = " ".join(str(getattr(seg, "text", "")).strip() for seg in segments).strip()
        detected = getattr(info, "language", None)
        probs = [float(getattr(seg, "avg_logprob", -9.0)) for seg in segments] or [-9.0]
        if text:
            self._context = (self._context + " " + text).strip()[-220:]
        return TranslationResult(
            text=text,
            state="ok" if text else "error",
            error=None if text else "识别结果为空",
            source_lang=detected,
            confidence=round(max(-1.0, min(0.0, sum(probs) / len(probs)) + 1.0), 4),
            model=self.model_name,
        )

    def info(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "model": self.model_name,
            "device": self.device,
            "compute_type": self.compute_type,
            "target_language": "en",
            "network": False,
            "notes": ["Whisper 的 translate 任务只输出英文", *self.load_notes],
        }


class OpenAiTranslateTranslator(Translator):
    """Text translation through any OpenAI-compatible chat endpoint."""

    name = "openai_compatible"

    def __init__(self, cfg):
        import urllib.request  # noqa: F401  (kept for symmetry with agents.py)

        self.base_url = cfg.translation.openai.base_url.rstrip("/")
        self.model = cfg.translation.openai.model
        self.api_key_env = cfg.translation.openai.api_key_env
        self.timeout_s = float(cfg.translation.openai.timeout_s)
        self.target_language = cfg.translation.target_language
        self.source_language = cfg.translation.source_language
        self.temperature = 0.0

    def _prompt(self, text: str) -> str:
        source = self.source_language or "自动识别"
        return (
            f"把下面的会议转写片段翻译成{self.target_language}（源语言：{source}）。"
            "只输出译文本身，不要解释、不要加引号、保留数字与专有名词。\n\n"
            f"{text}"
        )

    def translate(self, segment: dict[str, Any], audio: np.ndarray | None = None) -> TranslationResult:
        import urllib.error
        import urllib.request

        key = os.environ.get(self.api_key_env, "")
        body = json.dumps(
            {
                "model": self.model,
                "temperature": self.temperature,
                "messages": [{"role": "user", "content": self._prompt(str(segment.get("text", "")))}],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # pragma: no cover - network dependent
            detail = exc.read().decode("utf-8", "replace")[:300]
            return TranslationResult(text="", state="error", error=f"HTTP {exc.code}: {detail}")
        except Exception as exc:  # pragma: no cover
            return TranslationResult(text="", state="error", error=f"{type(exc).__name__}: {exc}")
        text = str(payload["choices"][0]["message"]["content"]).strip()
        return TranslationResult(text=text, state="ok", model=self.model)

    def info(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "base_url": self.base_url,
            "model": self.model,
            "target_language": self.target_language,
            "api_key_present": bool(os.environ.get(self.api_key_env)),
            "network": True,
        }


class CliTranslateTranslator(Translator):
    """Text translation through a locally installed agent CLI."""

    name = "cli"

    def __init__(self, cfg):
        import shutil

        self.command = [str(part) for part in cfg.translation.cli.command]
        if not self.command:
            raise RuntimeError("translation.cli.command is empty")
        self.timeout_s = float(cfg.translation.cli.timeout_s)
        self.prompt_via = cfg.translation.cli.prompt_via
        self.target_language = cfg.translation.target_language
        self._shutil = shutil

    def translate(self, segment: dict[str, Any], audio: np.ndarray | None = None) -> TranslationResult:
        import subprocess

        prompt = (
            f"翻译成{self.target_language}，只输出译文：\n{segment.get('text', '')}"
        )
        cmd = list(self.command)
        stdin_data = None
        if self.prompt_via == "stdin":
            stdin_data = prompt
        else:
            cmd.append(prompt)
        resolved = self._shutil.which(cmd[0]) or cmd[0]
        try:
            proc = subprocess.run(
                [resolved, *cmd[1:]],
                input=stdin_data,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
            )
        except Exception as exc:
            return TranslationResult(text="", state="error", error=f"{type(exc).__name__}: {exc}")
        if proc.returncode != 0:
            return TranslationResult(text="", state="error", error=f"CLI 退出码 {proc.returncode}")
        return TranslationResult(text=(proc.stdout or "").strip(), state="ok")

    def info(self) -> dict[str, Any]:
        return {"backend": self.name, "command": self.command, "target_language": self.target_language}


def build_translator(cfg, *, backend: str | None = None) -> Translator:
    name = (backend or cfg.translation.backend or "none").lower()
    if name in ("none", ""):
        return NoneTranslator()
    if name == "mock":
        return MockTranslator()
    if name == "whisper_translate":
        return WhisperTranslateTranslator(cfg)
    if name == "openai_compatible":
        return OpenAiTranslateTranslator(cfg)
    if name == "cli":
        return CliTranslateTranslator(cfg)
    raise ValueError(f"unknown translation backend {name!r}")


# --------------------------------------------------------------------------
# audio lookup
# --------------------------------------------------------------------------
class TranscriptAudioReader:
    """Slices the recorded 16 kHz chunks into segment-sized audio."""

    def __init__(self, store: Store, *, pad_ms: int = AUDIO_PAD_MS):
        self.store = store
        self.pad_ms = int(pad_ms)
        self._cache: dict[str, tuple[np.ndarray, int]] = {}

    def _load(self, path: Path) -> tuple[np.ndarray, int] | None:
        key = str(path)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if not path.exists():
            return None
        try:
            data, rate = read_wav(path)
        except Exception:
            return None
        mono = data if data.ndim == 1 else data.mean(axis=1)
        if len(self._cache) >= 3:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = (mono, rate)
        return self._cache[key]

    def audio_for(self, segment: dict[str, Any]) -> tuple[np.ndarray | None, str]:
        """Return ``(audio, reason)``; ``reason`` is ``ok`` or explains the miss."""
        assert self.store.paths is not None
        start_ms = max(0, int(segment["start_ms"]) - self.pad_ms)
        end_ms = int(segment["end_ms"]) + self.pad_ms
        pieces: list[np.ndarray] = []
        rate = 16000
        for chunk in self.store.chunks():
            if not chunk.get("converted_path"):
                continue
            if chunk["end_ms"] <= start_ms or chunk["start_ms"] >= end_ms:
                continue
            loaded = self._load(self.store.paths.root / chunk["converted_path"])
            if loaded is None:
                return None, "chunk_missing"
            mono, rate = loaded
            span_start = int((max(start_ms, chunk["start_ms"]) - chunk["start_ms"]) / 1000.0 * rate)
            span_end = int((min(end_ms, chunk["end_ms"]) - chunk["start_ms"]) / 1000.0 * rate)
            span_start = max(0, min(span_start, mono.size))
            span_end = max(span_start, min(span_end, mono.size))
            if span_end > span_start:
                pieces.append(mono[span_start:span_end])
        if not pieces:
            return None, "chunk_pending"
        audio = np.concatenate(pieces) if len(pieces) > 1 else pieces[0]
        if audio.size < int(0.2 * rate):
            return None, "too_short"
        return audio.astype(np.float32), "ok"


# --------------------------------------------------------------------------
# worker
# --------------------------------------------------------------------------
@dataclass
class TranslationState:
    """Runtime on/off switch stored next to the meeting."""

    enabled: bool = True
    target_language: str = ""
    backend: str = ""
    updated_utc: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "target_language": self.target_language,
            "backend": self.backend,
            "updated_utc": self.updated_utc or utc_now(),
        }


def translation_state_path(cfg, session_id: str) -> Path:
    return Path(cfg.output_root) / session_id / "translation.state.json"


def write_translation_state(cfg, session_id: str, *, enabled: bool, target_language: str = "", backend: str = "") -> Path:
    path = translation_state_path(cfg, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = TranslationState(
        enabled=enabled,
        target_language=target_language,
        backend=backend,
        updated_utc=utc_now(),
    ).to_dict()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_translation_state(cfg, session_id: str, default: TranslationState | None = None) -> TranslationState:
    fallback = default if default is not None else TranslationState()
    path = translation_state_path(cfg, session_id)
    if not path.exists():
        return fallback
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback
    return TranslationState(
        enabled=bool(data.get("enabled", True)),
        target_language=str(data.get("target_language") or ""),
        backend=str(data.get("backend") or ""),
        updated_utc=str(data.get("updated_utc") or ""),
    )


class TranslationWorker:
    """Continuously translates newly committed segments."""

    def __init__(
        self,
        cfg,
        store: Store,
        translator: Translator | None = None,
        *,
        target_language: str | None = None,
        on_translation: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.events = EventQueue(store.db)
        self.translator = translator if translator is not None else build_translator(cfg)
        self.target_language = (target_language or cfg.translation.target_language or "en").lower()
        self.on_translation = on_translation
        self.reader = TranscriptAudioReader(store)
        self.translated = 0
        self.skipped = 0
        self.errors = 0

    # -- one pass --------------------------------------------------------
    def run_cycle(self, *, limit: int = 20) -> dict[str, Any]:
        journal = self.cfg.translation
        pending = self.store.untranslated_segments(
            self.target_language, limit=limit, max_attempts=journal.max_attempts
        )
        session_closed = self.store.session_state() in (STATE_CLOSED, STATE_FAILED)
        processed = 0
        waiting_for_audio = 0

        for segment in pending:
            audio = None
            if self.translator.needs_audio:
                segment = self._refresh(segment)
                audio, reason = self.reader.audio_for(segment)
                if audio is None:
                    if reason in ("chunk_pending", "chunk_missing") and not session_closed:
                        # The chunk covering this segment has not been written
                        # yet; wait instead of recording a failure.
                        waiting_for_audio += 1
                        break
                    self._record(segment, TranslationResult("", state="error", error=f"音频不可用：{reason}"))
                    continue

            if journal.skip_same_language and not self.translator.needs_audio and _same_language(
                str(segment.get("text", "")), self.target_language
            ):
                self._record(
                    segment,
                    TranslationResult(str(segment.get("text", "")), state="skipped", error="已经是目标语言"),
                )
                continue

            try:
                result = self.translator.translate(segment, audio=audio)
            except Exception as exc:
                result = TranslationResult("", state="error", error=f"{type(exc).__name__}: {exc}")
            # Speech translation always returns the target language, so the
            # "same language" test must look at the *detected source*, not at
            # the output - otherwise every successful translation of an English
            # target would be discarded as an unnecessary one.
            if (
                self.translator.needs_audio
                and result.state == "ok"
                and journal.skip_same_language
                and language_code_matches(result.source_lang, self.target_language)
            ):
                result = TranslationResult(
                    str(segment.get("text", "")),
                    state="skipped",
                    error=f"源语言已是 {result.source_lang}",
                    source_lang=result.source_lang,
                    model=result.model,
                )
            self._record(segment, result)
            processed += 1

        return {
            "processed": processed,
            "waiting_for_audio": waiting_for_audio,
            "pending": len(pending),
            "counts": self.store.translation_counts(self.target_language),
            "translated": self.translated,
            "skipped": self.skipped,
            "errors": self.errors,
        }

    def _refresh(self, segment: dict[str, Any]) -> dict[str, Any]:
        current = next((s for s in self.store.segments() if s["segment_id"]==segment["segment_id"]),None)
        return current or segment

    def _record(self, segment: dict[str, Any], result: TranslationResult) -> None:
        segment_id = str(segment["segment_id"])
        previous = self.store.translation(segment_id, self.target_language) or {}
        attempts = int(previous.get("attempts", 0)) + (1 if result.state == "error" else 0)
        self.store.save_translation(
            segment_id,
            target_lang=self.target_language,
            text=result.text,
            backend=self.translator.name,
            model=result.model,
            source_lang=result.source_lang,
            confidence=result.confidence,
            segment_revision=int(segment.get("revision", 1)),
            source_review_revision=segment.get("review_revision",{}).get("transcript",0),
            state=result.state,
            error=result.error,
            attempts=attempts,
        )
        if result.state == "ok":
            self.translated += 1
        elif result.state == "skipped":
            self.skipped += 1
        else:
            self.errors += 1
        event, created = self.events.append(
            self.store.session_id,
            SEGMENT_TRANSLATED,
            {
                "segment_id": segment_id,
                "turn_id": segment.get("turn_id"),
                "talk_id": segment.get("talk_id"),
                "target_lang": self.target_language,
                "text": result.text,
                "state": result.state,
                "error": result.error,
                "backend": self.translator.name,
                "model": result.model,
                "source_lang": result.source_lang,
                "start_ms": segment.get("start_ms"),
                "end_ms": segment.get("end_ms"),
            },
            dedupe_key=f"segment.translated:{segment_id}:{self.target_language}:r{segment.get('revision', 1)}:a{attempts}",
        )
        if self.on_translation is not None and created:
            self.on_translation(event)

    # -- loop ------------------------------------------------------------
    def run_forever(self, *, follow: bool = True, max_idle_cycles: int = 3, state_file: Path | None = None) -> int:
        idle = 0
        while True:
            if state_file is not None and state_file.exists():
                state = read_translation_state(self.cfg, self.store.session_id)
                if not state.enabled:
                    self.store.log("info", "translation worker stopped by request")
                    return 0
            summary = self.run_cycle()
            if summary["processed"] or summary["waiting_for_audio"]:
                idle = 0
            else:
                idle += 1
            closed = self.store.session_state() in (STATE_CLOSED, STATE_FAILED)
            if closed and idle >= max_idle_cycles:
                return 0
            if not follow and idle >= max_idle_cycles:
                return 0
            time.sleep(max(0.2, self.cfg.translation.poll_wait_ms / 1000.0))

    def info(self) -> dict[str, Any]:
        return {
            "worker": "translation",
            "target_language": self.target_language,
            **self.translator.info(),
        }
