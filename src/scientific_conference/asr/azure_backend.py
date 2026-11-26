"""Optional Azure Speech transcription adapter.

Status on this machine: **not exercised** - no Azure subscription key is
configured, so the project's verification record covers it as "installed but
unverified".  It is included because the design document names the Azure
ConversationTranscriber as the first candidate for cloud diarization.

Scope of this adapter
---------------------
It transcribes one finished audio window at a time with ``SpeechRecognizer``,
which fits the windowed pipeline used here.  It does **not** provide speaker
labels: realtime diarization in Azure is delivered by the continuous
``ConversationTranscriber`` API, which owns its own audio stream and lifecycle
and is therefore not wired into this build.  Consequently
``diarization.backend: azure`` is rejected by the configuration validator, and
cloud transcripts come back with ``speaker_unknown`` unless another diarizer is
enabled.

Privacy: enabling this backend sends microphone audio to Microsoft.  The
configuration validator refuses to load it unless ``privacy.upload_audio`` is
explicitly set to true.
"""

from __future__ import annotations

import os
import threading
from typing import Any

import numpy as np

from .base import ASR_SAMPLE_RATE, AsrBackend, AsrError, AsrSegment, normalise_text

__all__ = ["AzureSpeechBackend", "azure_available"]


def azure_available() -> bool:
    try:
        import azure.cognitiveservices.speech  # noqa: F401

        return True
    except Exception:
        return False


class AzureSpeechBackend(AsrBackend):
    name = "azure"

    def __init__(
        self,
        *,
        region: str = "",
        key: str | None = None,
        key_env: str = "AZURE_SPEECH_KEY",
        endpoint: str = "",
        language: str = "zh-CN",
        sample_rate: int = ASR_SAMPLE_RATE,
    ) -> None:
        try:
            import azure.cognitiveservices.speech as speechsdk
        except Exception as exc:  # pragma: no cover - optional dependency
            raise AsrError(
                "azure-cognitiveservices-speech is not installed; "
                "install it into the external venv or use asr.backend=faster_whisper"
            ) from exc

        self._sdk = speechsdk
        self.language = language
        self.sample_rate = int(sample_rate)
        resolved_key = key or os.environ.get(key_env, "")
        if not resolved_key and not endpoint:
            raise AsrError(f"Azure key not found; set the {key_env} environment variable")
        if endpoint:
            self.speech_config = speechsdk.SpeechConfig(subscription=resolved_key, endpoint=endpoint)
        else:
            if not region:
                raise AsrError("asr.azure.region is required when no endpoint is configured")
            self.speech_config = speechsdk.SpeechConfig(subscription=resolved_key, region=region)
        self.speech_config.speech_recognition_language = language
        self._lock = threading.Lock()

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
        sdk = self._sdk
        x = np.asarray(samples, dtype=np.float32).ravel()
        if sample_rate != self.sample_rate:
            from ..audio.resample import resample_fixed

            x = resample_fixed(x, sample_rate, self.sample_rate)
        pcm = np.clip(x * 32767.0, -32768, 32767).astype("<i2").tobytes()

        stream = sdk.audio.PushAudioInputStream(
            sdk.audio.AudioStreamFormat(samples_per_second=self.sample_rate, bits_per_sample=16, channels=1)
        )
        audio_config = sdk.audio.AudioConfig(stream=stream)
        recognizer = sdk.SpeechRecognizer(speech_config=self.speech_config, audio_config=audio_config)
        results: list[AsrSegment] = []

        def on_recognized(evt) -> None:  # pragma: no cover - SDK callback
            if evt.result.reason != sdk.ResultReason.RecognizedSpeech or not evt.result.text:
                return
            offset_ms = int(evt.result.offset / 10_000)
            duration_ms = int(evt.result.duration / 10_000)
            results.append(
                AsrSegment(
                    start_ms=offset_ms,
                    end_ms=offset_ms + duration_ms,
                    text=normalise_text(evt.result.text),
                )
            )

        recognizer.recognized.connect(on_recognized)
        with self._lock:
            recognizer.start_continuous_recognition()
            stream.write(pcm)
            stream.close()
            recognizer.stop_continuous_recognition()
        return sorted(results, key=lambda s: s.start_ms)

    def info(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "language": self.language,
            "diarization": "not provided by this adapter (see module docstring)",
            "verified": False,
        }
