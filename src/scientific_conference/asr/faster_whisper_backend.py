"""Local speech recognition with faster-whisper.

This adapter is *offline by default*: the model is loaded from
a pre-downloaded Hugging Face snapshot (or the configured model directory) and no audio ever leaves the machine.

The backend is deliberately stateless: the incremental windowing, overlap
de-duplication and revision logic lives in :mod:`scientific_conference.asr.streaming`,
so a different backend can be dropped in without touching the pipeline.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np

from ..config import default_install_root

from .base import ASR_SAMPLE_RATE, AsrBackend, AsrError, AsrSegment, normalise_text

__all__ = ["FasterWhisperBackend", "resolve_local_model"]


def resolve_local_model(model: str, models_dir: str | Path | None, cache_dir: str | Path | None) -> Path | None:
    """Find an already-downloaded CTranslate2 model directory.

    Looking the snapshot up ourselves keeps the backend fully offline: nothing
    is written to the Hugging Face cache directory at run time, which matters
    both for the "no dependency inside the project" rule and for reproducibility.
    """
    direct = Path(model).expanduser()
    if direct.is_dir():
        return direct

    roots: list[Path] = []
    for candidate in (models_dir, cache_dir):
        if candidate:
            roots.append(Path(candidate))
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        roots.extend([Path(hf_home) / "hub", Path(hf_home)])
    managed_cache = default_install_root() / "cache" / "huggingface"
    roots.extend([managed_cache / "hub", managed_cache])
    roots.append(Path.home() / ".cache" / "huggingface" / "hub")

    # Turbo uses a different CT2 publisher in faster-whisper's supported model map.
    from faster_whisper.utils import _MODELS
    repository = _MODELS.get(model, f"Systran/faster-whisper-{model}")
    repo = "models--" + repository.replace("/", "--")
    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        snaps = root / repo / "snapshots"
        if not snaps.is_dir():
            continue
        dirs = sorted((d for d in snaps.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
        if dirs:
            return dirs[-1]
    return None


def _ensure_cuda_dlls() -> list[str]:
    """Make the CUDA runtime shipped as pip wheels importable.

    ``nvidia-cublas-cu12`` / ``nvidia-cudnn-cu12`` / ``nvidia-cuda-runtime-cu12``
    install their DLLs under ``site-packages/nvidia/*/bin``, which is not on the
    Windows search path.  CTranslate2 needs ``cublas64_12.dll`` and the cuDNN 9
    libraries, so those directories are registered before the model is loaded.
    """
    added: list[str] = []
    try:
        import sysconfig

        site = Path(sysconfig.get_paths()["purelib"])
    except Exception:  # pragma: no cover
        return added
    for bin_dir in sorted(site.glob("nvidia/*/bin")):
        if not bin_dir.is_dir():
            continue
        try:
            os.add_dll_directory(str(bin_dir))
        except (AttributeError, OSError):  # pragma: no cover - non-Windows
            pass
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
        added.append(str(bin_dir))
    return added


class FasterWhisperBackend(AsrBackend):
    name = "faster_whisper"

    #: Whisper's documented fallback ladder.  faster-whisper only retries at a
    #: higher temperature when it is given a *list*; passing a single float
    #: disables the retry, which is what let repetition loops through.
    FALLBACK_TEMPERATURES: tuple[float, ...] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)

    def __init__(
        self,
        model: str = "small",
        *,
        device: str = "auto",
        compute_type: str = "auto",
        beam_size: int = 5,
        temperature: float = 0.0,
        temperature_fallback: bool = True,
        compression_ratio_threshold: float = 2.4,
        log_prob_threshold: float = -1.0,
        no_speech_threshold: float = 0.6,
        word_timestamps: bool = True,
        language_hints: list[str] | None = None,
        initial_prompt: str = "",
        models_dir: str | Path | None = None,
        cache_dir: str | Path | None = None,
    ) -> None:
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # pragma: no cover - depends on install
            raise AsrError(
                "faster-whisper is not installed in this environment; "
                "run scripts/fetch_models.py --whisper MODEL or set asr.backend=mock"
            ) from exc

        self.cuda_dll_dirs = _ensure_cuda_dlls()

        self.model_name = model
        self.beam_size = int(beam_size)
        self.temperature = float(temperature)
        self.temperature_fallback = bool(temperature_fallback)
        self.compression_ratio_threshold = (
            None if compression_ratio_threshold is None else float(compression_ratio_threshold)
        )
        self.log_prob_threshold = None if log_prob_threshold is None else float(log_prob_threshold)
        self.no_speech_threshold = (
            None if no_speech_threshold is None else float(no_speech_threshold)
        )
        self.temperatures = self._temperature_ladder(
            self.temperature, self.temperature_fallback
        )
        self.word_timestamps = bool(word_timestamps)
        self.language_hints = list(language_hints or [])
        self.initial_prompt = initial_prompt or ""
        #: Counters surfaced through :meth:`info`, i.e. in ``conference.status``.
        self.windows_decoded = 0
        self.windows_with_fallback = 0
        self.repetition_segments = 0
        self.model_path = resolve_local_model(model, models_dir, cache_dir)
        self.device, self.compute_type, self._model = self._load(
            WhisperModel, self.model_path or model, device, compute_type, models_dir
        )

    # ------------------------------------------------------------------
    @classmethod
    def _temperature_ladder(cls, temperature: float, fallback: bool) -> list[float]:
        """The temperature list handed to faster-whisper.

        The retry on a failed decode only exists when the list has more than one
        entry, so ``temperature_fallback`` is what makes
        ``compression_ratio_threshold`` / ``log_prob_threshold`` effective.
        Higher ladder entries than the configured start are kept, lower ones are
        dropped: ``asr.temperature`` stays the *first* attempt.
        """
        start = float(temperature)
        if not fallback:
            return [start]
        ladder = [start]
        ladder.extend(t for t in cls.FALLBACK_TEMPERATURES if t > start)
        return ladder

    @staticmethod
    def _candidate_settings(device: str, compute_type: str) -> list[tuple[str, str]]:
        if device == "cpu":
            return [("cpu", "int8" if compute_type == "auto" else compute_type)]
        if device == "cuda":
            return [("cuda", "float16" if compute_type == "auto" else compute_type)]
        auto = [("cuda", "float16" if compute_type == "auto" else compute_type)]
        auto.append(("cpu", "int8" if compute_type == "auto" else compute_type))
        return auto

    def _load(self, cls, model, device, compute_type, models_dir):
        problems: list[str] = []
        for dev, ctype in self._candidate_settings(device, compute_type):
            try:
                kwargs: dict[str, Any] = {"device": dev, "compute_type": ctype}
                if models_dir:
                    kwargs["download_root"] = str(models_dir)
                if isinstance(model, Path):
                    kwargs["local_files_only"] = True
                m = cls(str(model), **kwargs)
                # Loading the model does not touch cuBLAS/cuDNN; a real
                # inference does.  Warm up here so that a missing CUDA runtime
                # falls back to CPU instead of failing mid-recording.
                list(m.transcribe(np.zeros(8000, dtype=np.float32), language="en", beam_size=1)[0])
                self.load_notes = problems + [f"using device={dev} compute_type={ctype}"]
                return dev, ctype, m
            except Exception as exc:  # pragma: no cover - hardware dependent
                problems.append(f"{dev}/{ctype}: {type(exc).__name__}: {exc}")
        raise AsrError("could not load faster-whisper model: " + "; ".join(problems))

    # ------------------------------------------------------------------
    def language_for(self, language: str | None) -> str | None:
        """Decide the language hint for one window.

        A single non-``auto`` hint forces that language.  Several hints mean a
        genuinely mixed meeting (the default is ``[zh, en]``), so the model is
        left to detect the language of *each* window - forcing Chinese would
        mangle the English questions in a Q&A block.
        """
        if language and language != "auto":
            return language
        hints = [h for h in self.language_hints if h and h != "auto"]
        if len(hints) == 1:
            return hints[0]
        return None

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
        del offset_ms  # times are returned relative to the supplied buffer
        x = np.asarray(samples, dtype=np.float32).ravel()
        if sample_rate != ASR_SAMPLE_RATE:
            from ..audio.resample import resample_fixed

            x = resample_fixed(x, sample_rate, ASR_SAMPLE_RATE)
        if x.size == 0:
            return []

        segments, info = self._model.transcribe(
            x,
            language=self.language_for(language),
            task="transcribe",
            beam_size=self.beam_size,
            temperature=self.temperatures,
            compression_ratio_threshold=self.compression_ratio_threshold,
            log_prob_threshold=self.log_prob_threshold,
            no_speech_threshold=self.no_speech_threshold,
            word_timestamps=self.word_timestamps,
            initial_prompt=initial_prompt or self.initial_prompt or None,
            vad_filter=False,  # this project runs its own VAD
            condition_on_previous_text=False,
        )
        out: list[AsrSegment] = []
        for seg in segments:
            text = normalise_text(getattr(seg, "text", ""))
            if not text:
                continue
            words = []
            for w in getattr(seg, "words", None) or []:
                words.append(
                    {
                        "start_ms": int(round(float(w.start) * 1000)),
                        "end_ms": int(round(float(w.end) * 1000)),
                        "word": str(w.word),
                        "prob": float(getattr(w, "probability", 0.0) or 0.0),
                    }
                )
            out.append(
                AsrSegment(
                    start_ms=int(round(float(seg.start) * 1000)),
                    end_ms=int(round(float(seg.end) * 1000)),
                    text=text,
                    confidence=_avg_word_prob(words),
                    words=words,
                    temperature=_float_or_none(getattr(seg, "temperature", None)),
                    no_speech_prob=_float_or_none(getattr(seg, "no_speech_prob", None)),
                    compression_ratio=_float_or_none(getattr(seg, "compression_ratio", None)),
                    avg_logprob=_float_or_none(getattr(seg, "avg_logprob", None)),
                )
            )
        self.last_info = {
            "language": getattr(info, "language", None),
            "language_probability": float(getattr(info, "language_probability", 0.0) or 0.0),
            "duration_s": float(getattr(info, "duration", 0.0) or 0.0),
        }
        self.windows_decoded += 1
        if any(s.temperature for s in out):
            self.windows_with_fallback += 1
        self.repetition_segments += sum(
            1
            for s in out
            if s.compression_ratio is not None
            and self.compression_ratio_threshold is not None
            and s.compression_ratio > self.compression_ratio_threshold
        )
        return out

    def anomaly_flags(self, segment: AsrSegment) -> list[str]:
        """Flag a decode that still looks like a repetition loop.

        ``compression_ratio`` is faster-whisper's own "too repetitive" measure
        (bytes divided by their zlib-compressed size).  The decoder already
        retries at a higher temperature when it exceeds the threshold, so a
        segment that arrives here above it has survived every retry: the text is
        then marked, reported in ``conference.status`` and left untouched.

        Routine retries are *not* flagged per segment - they are counted in
        :meth:`info` instead, so a segment's flags stay a list of anomalies
        rather than a log of decoder temperatures.
        """
        flags: list[str] = []
        ratio = segment.compression_ratio
        if (
            ratio is not None
            and self.compression_ratio_threshold is not None
            and ratio > self.compression_ratio_threshold
        ):
            flags.append("repetition_suspected")
            flags.append(f"compression_ratio={ratio:.2f}")
            if segment.temperature is not None:
                flags.append(f"temperature={segment.temperature:.1f}")
        return flags

    def info(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "model": self.model_name,
            "device": self.device,
            "compute_type": self.compute_type,
            "beam_size": self.beam_size,
            "temperatures": list(self.temperatures),
            "compression_ratio_threshold": self.compression_ratio_threshold,
            "log_prob_threshold": self.log_prob_threshold,
            "no_speech_threshold": self.no_speech_threshold,
            "condition_on_previous_text": False,
            "language_hints": self.language_hints,
            "windows_decoded": self.windows_decoded,
            "windows_with_fallback": self.windows_with_fallback,
            "repetition_segments": self.repetition_segments,
            "notes": getattr(self, "load_notes", []),
        }


def _float_or_none(value: Any) -> float | None:
    """``None`` stays ``None``; a missing attribute must not become ``0.0``.

    A fabricated ``0.0`` compression ratio would look like a perfectly clean
    decode, so absent decoder signals are reported as absent.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def _avg_word_prob(words: list[dict[str, Any]]) -> float | None:
    """Mean word probability, or ``None`` when the backend gave no words.

    The project never fabricates a confidence percentage: when the backend does
    not expose one, ``null`` is stored.
    """
    probs = [w["prob"] for w in words if w.get("prob")]
    if not probs:
        return None
    return round(sum(probs) / len(probs), 4)
