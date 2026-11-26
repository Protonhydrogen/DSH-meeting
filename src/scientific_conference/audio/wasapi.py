"""Capture the Windows default playback endpoint with WASAPI loopback."""

from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import queue
import struct
import sys
import threading
import time
from uuid import UUID

import numpy as np

from .sources import AudioSource, SourceError


def pcm_block(raw: bytes | None, frames: int, channels: int, encoding: str) -> np.ndarray:
    if raw is None:
        return np.zeros((frames, channels), dtype=np.int16)
    if encoding == 'float32':
        samples = np.frombuffer(raw, dtype='<f4')
        samples = np.clip(np.nan_to_num(samples, nan=0.0) * 32768, -32768, 32767).astype(np.int16)
    elif encoding == 'pcm16':
        samples = np.frombuffer(raw, dtype='<i2')
    elif encoding == 'pcm32':
        samples = (np.frombuffer(raw, dtype='<i4') >> 16).astype(np.int16)
    else:  # Packed, little-endian 24-bit PCM: keep its most significant bytes.
        octets = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        samples = np.ascontiguousarray(octets[:, 1:]).view('<i2')
    return samples.reshape(frames, channels)


def _guid(value: str):
    return (C.c_ubyte * 16).from_buffer_copy(UUID(value).bytes_le)


def _check(hr: int, operation: str) -> None:
    if hr < 0:
        raise SourceError(f'WASAPI {operation} (0x{hr & 0xFFFFFFFF:08X})')


def _call(obj, slot, name, types=(), *args):
    # COM method indexes include the three IUnknown entries.
    table = C.cast(obj, C.POINTER(C.POINTER(C.c_void_p))).contents
    result_type = W.ULONG if name == 'Release' else C.c_long
    method = C.WINFUNCTYPE(result_type, C.c_void_p, *types)(table[slot])
    _check(method(obj, *args), name)


class SystemAudioSource(AudioSource):
    """The worker owns its COM interfaces and hands PCM blocks to the recorder."""

    kind = 'system'

    def __init__(self):
        super().__init__()
        self._queue = queue.Queue(maxsize=100)
        self._ready = threading.Event()
        self._begin = threading.Event()
        self._started = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._error = None

    @property
    def name(self) -> str:
        return 'Windows default playback (WASAPI loopback)'

    def prepare(self) -> None:
        if sys.platform != 'win32':
            raise SourceError('System audio capture requires Windows')
        if self._thread is None:
            self._thread = threading.Thread(target=self._capture, name='wasapi-loopback', daemon=True)
            self._thread.start()
        self._await(self._ready, 'opening the default playback device')

    def _await(self, event, phase):
        if not event.wait(5):
            raise SourceError(f'WASAPI timed out while {phase}')
        if self._error:
            raise self._error

    def start(self) -> None:
        self.prepare()
        self._begin.set()
        self._await(self._started, 'starting capture')

    def read(self, timeout=None) -> np.ndarray | None:
        if self._error:
            raise self._error
        try:
            block = self._queue.get(timeout=timeout)
        except queue.Empty:
            if self._error:
                raise self._error
            if self._stop.is_set():
                return None
            raise TimeoutError('no system audio block within timeout') from None
        self.status['blocks'] += 1
        self.status['frames'] += len(block)
        return block

    def stop(self) -> None:
        self._stop.set()
        self._begin.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _enqueue(self, block):
        try:
            self._queue.put_nowait(block)
        except queue.Full:
            self.status['overflows'] += 1
            self.status['gap_ms'] += round(len(block) * 1000 / self.sample_rate)

    def _capture(self):
        try:
            self._run_wasapi()
        except Exception as error:
            self._error = SourceError(str(error))
            self.status['error'] = str(error)
            if self._started.is_set():
                self.status['disconnects'] += 1
        finally:
            self._ready.set()
            self._started.set()

    def _run_wasapi(self):
        ptr, uint = C.c_void_p, C.c_uint32
        pptr = C.POINTER(ptr)
        ole = C.WinDLL('ole32')
        ole.CoCreateInstance.argtypes = (ptr, ptr, W.DWORD, ptr, pptr)
        ole.CoTaskMemFree.argtypes = (ptr,)
        ole.CoInitializeEx.argtypes = (ptr, W.DWORD)
        _check(ole.CoInitializeEx(None, 0), 'CoInitializeEx')
        enumerator, device, client, capture, fmt = (ptr() for _ in range(5))
        started = False
        try:
            _check(ole.CoCreateInstance(_guid('bcde0395-e52f-467c-8e3d-c4579291692e'), None, 1,
                  _guid('a95664d2-9614-4f35-a746-de8db63617e6'), C.byref(enumerator)), 'create device enumerator')
            _call(enumerator, 4, 'get default playback endpoint', (C.c_int, C.c_int, pptr), 0, 1, C.byref(device))
            _call(device, 3, 'activate audio client', (ptr, W.DWORD, ptr, pptr),
                  _guid('1cb9ad4c-dbfa-4c32-b178-c2f568a703b2'), 1, None, C.byref(client))
            _call(client, 8, 'get playback format', (pptr,), C.byref(fmt))
            tag, self.channels, self.sample_rate, _, align, bits, extra = struct.unpack('<HHIIHHH', C.string_at(fmt, 18))
            if tag == 0xFFFE and extra >= 22:
                tag = struct.unpack('<I', C.string_at(fmt.value + 24, 4))[0]
            encoding = {(3, 32): 'float32', (1, 16): 'pcm16', (1, 24): 'pcm24', (1, 32): 'pcm32'}.get((tag, bits))
            if encoding is None or not self.channels or not self.sample_rate or align != self.channels * (bits // 8):
                raise SourceError(f'Unsupported WASAPI playback format ({tag}, {bits} bit)')
            _call(client, 3, 'initialize shared loopback', (C.c_int, W.DWORD, C.c_int64, C.c_int64, ptr, ptr),
                  0, 0x00020000, 2_000_000, 0, fmt, None)
            _call(client, 14, 'get capture interface', (ptr, pptr),
                  _guid('c8adbd64-e71e-48a0-a4de-185c395cd317'), C.byref(capture))
            self._ready.set()
            self._begin.wait()
            if self._stop.is_set():
                return
            _call(client, 10, 'start loopback')
            started = True
            self._started.set()
            silence_until = time.monotonic()
            silence_frames = max(1, round(self.sample_rate * 0.03))
            packet, frames, flags, data = uint(), uint(), W.DWORD(), ptr()
            while not self._stop.is_set():
                _call(capture, 5, 'get packet size', (C.POINTER(uint),), C.byref(packet))
                if packet.value:
                    _call(capture, 3, 'read packet', (pptr, C.POINTER(uint), C.POINTER(W.DWORD), ptr, ptr),
                          C.byref(data), C.byref(frames), C.byref(flags), None, None)
                    try:
                        raw = None if flags.value & 2 else C.string_at(data, frames.value * align)
                    finally:
                        _call(capture, 4, 'release packet', (uint,), frames.value)
                    if flags.value & 1:
                        self.status['overflows'] += 1
                    self._enqueue(pcm_block(raw, frames.value, self.channels, encoding))
                    silence_until = time.monotonic()
                else:
                    # An idle endpoint can deliver no packets at all. Keep silence in
                    # the time line so the speech detector can close an open span.
                    if time.monotonic() - silence_until > 0.10:
                        self._enqueue(pcm_block(None, silence_frames, self.channels, encoding))
                        silence_until += silence_frames / self.sample_rate
                    self._stop.wait(0.01)
        finally:
            try:
                if started:
                    _call(client, 11, 'stop loopback')
            finally:
                for interface in (capture, client, device, enumerator):
                    if interface:
                        _call(interface, 2, 'Release')
                if fmt:
                    ole.CoTaskMemFree(fmt)
                ole.CoUninitialize()
