"""Audio sources: microphone capture and file replay.

Both sources expose the same tiny interface so the pipeline can be exercised
without a microphone (offline replay of a WAV file, which is what the test
suite and the acceptance run use).

Contract
--------
``start()``          begin producing
``read(timeout)``    return the next int16 block shaped ``(frames, channels)``,
                     or ``None`` when the stream is finished
``stop()``           release the device / file handle
``status``           counters: blocks, overflows, disconnect events, gap ms
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from .wavio import read_wav

__all__ = ["AudioSource", "MicrophoneSource", "WavFileSource", "open_source"]


class SourceError(RuntimeError):
    pass


class AudioSource:
    """Base class - not usable directly."""

    sample_rate: int = 0
    channels: int = 1
    kind: str = "base"

    def __init__(self) -> None:
        self.status: dict[str, Any] = {
            "blocks": 0,
            "frames": 0,
            "overflows": 0,
            "disconnects": 0,
            "gap_ms": 0,
            "error": None,
        }

    def prepare(self) -> None:
        """Validate/open the input without beginning capture."""

    def start(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def read(self, timeout: float | None = None) -> np.ndarray | None:  # pragma: no cover
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover
        raise NotImplementedError

    @property
    def name(self) -> str:  # pragma: no cover
        return self.kind


class MicrophoneSource(AudioSource):
    """WASAPI/PortAudio microphone capture via ``sounddevice``.

    The PortAudio callback only copies the block into a bounded queue; it never
    performs I/O, resampling or recognition, exactly as the design requires.
    """

    kind = "microphone"

    def __init__(
        self,
        *,
        device: int | None = None,
        device_name: str = "system default input",
        sample_rate: int = 48000,
        channels: int = 1,
        blocksize: int = 0,
        queue_max_blocks: int = 900,
    ) -> None:
        super().__init__()
        self.device = device
        self._name = device_name
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        # 30 ms blocks keep the end-to-end latency low without waking the CPU
        # too often; PortAudio may ignore the hint and use its own size.
        self.blocksize = int(blocksize) if blocksize else max(1, int(self.sample_rate * 0.03))
        self._q: queue.Queue[Any] = queue.Queue(maxsize=queue_max_blocks)
        self._stream = None
        self._stopped = threading.Event()
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._name

    def _callback(self, indata, frames, time_info, status) -> None:  # pragma: no cover - device thread
        if status:
            text = str(status)
            if "overflow" in text:
                self.status["overflows"] += 1
            self.status["error"] = text
        block = np.array(indata, dtype=np.int16, copy=True)
        try:
            self._q.put_nowait(block)
        except queue.Full:
            # Dropping the oldest block keeps the recorder alive under load;
            # the drop is counted and surfaced as a gap instead of being silent.
            try:
                dropped = self._q.get_nowait()
                self.status["overflows"] += 1
                self.status["gap_ms"] += int(len(dropped) / self.sample_rate * 1000)
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(block)
            except queue.Full:
                pass

    def prepare(self) -> None:
        import sounddevice as sd
        if self._stream is not None:
            return
        self._stream = sd.InputStream(
            device=self.device,
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            blocksize=self.blocksize,
            callback=self._callback,
        )

    def start(self) -> None:
        self.prepare()
        self._stream.start()

    def read(self, timeout: float | None = None) -> np.ndarray | None:
        try:
            block = self._q.get(timeout=timeout)
        except queue.Empty:
            if self._stopped.is_set():
                return None
            raise TimeoutError("no audio block within timeout")
        self.status["blocks"] += 1
        self.status["frames"] += int(block.shape[0])
        return block

    def stop(self) -> None:
        self._stopped.set()
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.stop()
                finally:
                    self._stream.close()
                    self._stream = None

    def pending_blocks(self) -> int:
        return self._q.qsize()


class WavFileSource(AudioSource):
    """Replay a WAV file as if it were a live microphone.

    Used by ``conference selftest``, by the pytest suite, and for offline
    re-processing of a recording.
    """

    kind = "file"

    def __init__(
        self,
        path: str | Path,
        *,
        block_ms: float = 30.0,
        realtime: bool = False,
        speed: float = 1.0,
        repeat: int = 1,
    ) -> None:
        super().__init__()
        self.path = Path(path)
        if not self.path.exists():
            raise SourceError(f"audio file not found: {self.path}")
        data, rate = read_wav(self.path)
        if data.ndim == 1:
            data = data[:, None]
        self._data = data
        self.sample_rate = int(rate)
        self.channels = int(data.shape[1])
        self._block = max(1, int(self.sample_rate * block_ms / 1000.0))
        self.realtime = bool(realtime)
        self.speed = float(speed)
        self.repeat = max(1, int(repeat))
        self._pos = 0
        self._round = 0
        self._next_due = 0.0

    @property
    def name(self) -> str:
        return f"file:{self.path.name}"

    @property
    def total_frames(self) -> int:
        return int(self._data.shape[0]) * self.repeat

    def start(self) -> None:
        self._pos = 0
        self._round = 0
        self._next_due = time.monotonic()

    def read(self, timeout: float | None = None) -> np.ndarray | None:
        if self._pos >= self._data.shape[0]:
            self._round += 1
            if self._round >= self.repeat:
                return None
            self._pos = 0
        end = min(self._pos + self._block, self._data.shape[0])
        chunk = self._data[self._pos : end]
        self._pos = end
        pcm = np.clip(chunk * 32767.0, -32768, 32767).astype(np.int16)
        self.status["blocks"] += 1
        self.status["frames"] += int(pcm.shape[0])
        if self.realtime:
            self._next_due += pcm.shape[0] / self.sample_rate / self.speed
            delay = self._next_due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        return pcm

    def stop(self) -> None:
        return None


def open_source(cfg, *, source: str = "microphone", wav_path: str | None = None, realtime: bool = False) -> AudioSource:
    """Factory used by the service and the CLI."""
    if source == "file":
        if not wav_path:
            raise SourceError("--wav is required when --source file")
        return WavFileSource(wav_path, realtime=realtime)
    if source == "system":
        from .wasapi import SystemAudioSource

        return SystemAudioSource()
    if source != "microphone":
        raise SourceError(f"unknown source {source!r}")

    from .devices import pick_sample_rate, resolve_device

    device_index, device_name = resolve_device(cfg.audio.device_id)
    rate = pick_sample_rate(device_index, cfg.audio.channels, cfg.audio.sample_rate)
    return MicrophoneSource(
        device=device_index,
        device_name=device_name,
        sample_rate=rate,
        channels=cfg.audio.channels,
        queue_max_blocks=cfg.audio.queue_max_chunks,
    )
