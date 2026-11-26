"""Shared pytest fixtures.

The suite is deliberately offline and hardware-free: it uses the ``energy`` VAD,
the ``mock`` ASR backend and synthesised audio, so it runs on any machine and
never touches the microphone, the network or the model cache.
"""

from __future__ import annotations

import copy
import os
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def pytest_configure(config) -> None:
    """Never let a stray environment variable change what the tests configure."""
    for name in list(os.environ):
        if name.startswith("SC_") and name != "SC_INSTALL_ROOT":
            os.environ.pop(name, None)


#: Root for per-test working directories.
#:
#: ``%TEMP%`` is not always writable for this project (the workspace sandbox
#: only guarantees the project tree), and pytest's own ``tmp_path_factory``
#: insists on deleting its base directory between sessions, which some Windows
#: file sandboxes reject.  The fixture below therefore replaces ``tmp_path``
#: with a plain, unique directory under the project and leaves cleanup to the
#: next run.
TMP_ROOT = Path(tempfile.gettempdir()) / "dsh-meeting-tests"


@pytest.fixture()
def tmp_path(request) -> Path:
    import re
    import uuid

    # ``PYTEST_TMP_ROOT`` lets a sandboxed run point the suite at a directory it
    # can actually create in.  The default root is created by earlier runs and
    # may then be owned by a sandbox account, which denies new children.
    root = Path(os.environ.get("PYTEST_TMP_ROOT") or TMP_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)[:60] or "test"
    path = root / f"{safe}_{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path
from scientific_conference.config import Config, load_config  # noqa: E402
from scientific_conference.audio import write_wav_int16  # noqa: E402


@pytest.fixture()
def cfg(tmp_path: Path) -> Config:
    """A configuration that writes everything below ``tmp_path``."""
    config = load_config(PROJECT_ROOT / "config" / "example.yaml", env=False)
    config.project.output_root = str(tmp_path / "meetings")
    config.asr.backend = "mock"
    config.vad.backend = "energy"
    config.vad.energy_threshold = 0.02
    config.vad.min_speech_ms = 200
    config.vad.min_silence_ms = 300
    config.diarization.backend = "none"
    config.segmenter.turn_silence_ms = 2000
    config.segmenter.grace_ms = 500
    config.segmenter.talk_close_silence_ms = 6000
    config.privacy.require_consent = False
    config.runner.agent = "offline_extractive"
    return config


@pytest.fixture()
def cfg_factory(cfg: Config):
    def make(**overrides):
        clone = copy.deepcopy(cfg)
        for dotted, value in overrides.items():
            section, _, key = dotted.partition("__")
            if key:
                setattr(getattr(clone, section), key, value)
            else:
                setattr(clone, section, value)
        return clone

    return make


def burst_audio(
    segments: list[tuple[int, int, float]],
    total_ms: int,
    sample_rate: int = 16000,
    *,
    seed: int = 0,
) -> np.ndarray:
    """Build mono audio with loud noise bursts at the given ``(start, end, amp)``."""
    rng = np.random.default_rng(seed)
    out = np.zeros(int(total_ms / 1000.0 * sample_rate), dtype=np.float32)
    for start_ms, end_ms, amp in segments:
        a = int(start_ms / 1000.0 * sample_rate)
        b = int(end_ms / 1000.0 * sample_rate)
        n = max(0, b - a)
        if n:
            # Amplitude-modulated noise reads as "speech" to the energy gate and
            # avoids depending on a downloaded VAD model.
            t = np.arange(n) / sample_rate
            wave = rng.standard_normal(n) * (0.4 + 0.6 * np.abs(np.sin(2 * np.pi * 4 * t)))
            out[a:b] = (wave * amp).astype(np.float32)
    return out


@pytest.fixture()
def meeting_wav(tmp_path: Path):
    """A two-speaker, three-turn synthetic meeting (16 kHz mono WAV)."""

    def make(name: str = "meeting.wav", *, sample_rate: int = 16000, plan=None):
        plan = plan or [
            # (start_ms, end_ms, amplitude)
            (500, 2500, 0.35),
            (2900, 5200, 0.35),
            (10000, 12500, 0.30),
            (12900, 15000, 0.30),
            (20000, 22000, 0.35),
        ]
        audio = burst_audio(plan, total_ms=24000, sample_rate=sample_rate)
        path = tmp_path / name
        pcm = np.clip(audio, -1, 1) * 32767
        write_wav_int16(path, pcm.astype(np.int16), sample_rate)
        return path, plan

    return make
