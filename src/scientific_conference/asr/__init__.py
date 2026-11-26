"""Speech recognition backends and the incremental streaming wrapper."""

from __future__ import annotations

from typing import Any

from .base import ASR_SAMPLE_RATE, AsrBackend, AsrError, AsrSegment, normalise_text
from .mock_backend import MockAsrBackend
from .streaming import StableSegment, StreamingTranscriber

__all__ = [
    "ASR_SAMPLE_RATE",
    "AsrBackend",
    "AsrError",
    "AsrSegment",
    "normalise_text",
    "MockAsrBackend",
    "StableSegment",
    "StreamingTranscriber",
    "build_backend",
]


RECORDING_BACKENDS = ("faster_whisper", "qwen3_asr")


def select_recording_backend(cfg, backend: str):
    if backend not in RECORDING_BACKENDS:
        raise ValueError("Transcription model must be faster_whisper or qwen3_asr")
    cfg.asr.backend = backend


def build_backend(cfg, *, script: list[dict[str, Any]] | None = None):
    """Instantiate the configured ASR backend."""
    backend = cfg.asr.backend
    if backend == "qwen3_asr":
        from .qwen import QwenBackend

        engine = QwenBackend(cfg.asr.qwen, language_hints=cfg.asr.language_hints)
        engine.prepare()
        return engine
    if backend == "mock":
        return MockAsrBackend(script=script)
    if backend == "azure":
        from .azure_backend import AzureSpeechBackend

        return AzureSpeechBackend(
            region=cfg.asr.azure.region,
            key_env=cfg.asr.azure.key_env,
            endpoint=cfg.asr.azure.endpoint,
            language=cfg.asr.azure.language,
        )
    if backend == "faster_whisper":
        from .faster_whisper_backend import FasterWhisperBackend
        from ..glossary import prompt

        return FasterWhisperBackend(
            cfg.asr.model,
            device=cfg.asr.device,
            compute_type=cfg.asr.compute_type,
            beam_size=cfg.asr.beam_size,
            temperature=cfg.asr.temperature,
            temperature_fallback=cfg.asr.temperature_fallback,
            compression_ratio_threshold=cfg.asr.compression_ratio_threshold,
            log_prob_threshold=cfg.asr.log_prob_threshold,
            no_speech_threshold=cfg.asr.no_speech_threshold,
            word_timestamps=cfg.asr.word_timestamps,
            language_hints=list(cfg.asr.language_hints),
            initial_prompt=prompt(cfg.asr.glossary_profile) if cfg.asr.glossary_profile else cfg.asr.initial_prompt,
            models_dir=str(cfg.models_dir),
        )
    raise AsrError(f"unknown asr backend {backend!r}")
