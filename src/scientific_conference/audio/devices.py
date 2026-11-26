"""Audio device enumeration and selection.

``sounddevice`` (PortAudio) is imported lazily so that the rest of the toolkit
- and its test suite - keeps working on a machine without PortAudio or without
an audio device at all.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

__all__ = ["DeviceInfo", "list_devices", "resolve_device", "audio_backend_status"]


@dataclass
class DeviceInfo:
    index: int
    name: str
    hostapi: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float
    is_default_input: bool
    supported_sample_rates: list[int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sd():
    import sounddevice as sd  # imported lazily on purpose

    return sd


def audio_backend_status() -> dict[str, Any]:
    """Report whether the audio backend can be used at all."""
    try:
        sd = _sd()
    except Exception as exc:  # pragma: no cover - depends on the machine
        return {"available": False, "error": f"{type(exc).__name__}: {exc}", "devices": 0}
    try:
        devs = sd.query_devices()
        return {"available": True, "error": None, "devices": len(devs), "portaudio": sd.get_portaudio_version()[1]}
    except Exception as exc:  # pragma: no cover
        return {"available": False, "error": f"{type(exc).__name__}: {exc}", "devices": 0}


def _probe_sample_rates(index: int, channels: int, candidates: tuple[int, ...]) -> list[int]:
    sd = _sd()
    ok: list[int] = []
    for rate in candidates:
        try:
            sd.check_input_settings(device=index, channels=channels, samplerate=rate, dtype="int16")
        except Exception:
            continue
        ok.append(rate)
    return ok


def list_devices(*, probe_sample_rates: bool = True) -> list[DeviceInfo]:
    """Enumerate input-capable devices.

    Sample rates are *probed* instead of assumed: many Windows microphones
    advertise 44.1/48 kHz only, so the transcription branch resamples rather
    than asking the device for 16 kHz directly.
    """
    sd = _sd()
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    try:
        default_in = sd.default.device[0]
    except Exception:  # pragma: no cover
        default_in = None

    out: list[DeviceInfo] = []
    for idx, dev in enumerate(devices):
        if int(dev.get("max_input_channels", 0)) <= 0:
            continue
        api_name = hostapis[int(dev["hostapi"])]["name"] if dev.get("hostapi") is not None else ""
        rates: list[int] = []
        if probe_sample_rates:
            rates = _probe_sample_rates(
                idx, 1, (16000, 22050, 32000, 44100, 48000, 96000)
            )
        out.append(
            DeviceInfo(
                index=idx,
                name=str(dev.get("name", "")).strip(),
                hostapi=str(api_name),
                max_input_channels=int(dev.get("max_input_channels", 0)),
                max_output_channels=int(dev.get("max_output_channels", 0)),
                default_samplerate=float(dev.get("default_samplerate", 0.0) or 0.0),
                is_default_input=(default_in is not None and int(default_in) == idx),
                supported_sample_rates=rates,
            )
        )
    return out


def resolve_device(device_id: Any) -> tuple[int | None, str]:
    """Turn a config value into ``(device_index, human_name)``.

    ``"default"``/``None`` uses the PortAudio default input.  An integer is
    used as-is.  A string is matched case-insensitively against device names,
    preferring input-capable devices.
    """
    if device_id in (None, "", "default"):
        # Prefer the same physical microphone through WASAPI on Windows.
        # Legacy MME can advertise valid formats but fail when opening a stream.
        try:
            devices = list_devices(probe_sample_rates=False)
            default = next((d for d in devices if d.is_default_input), None)
            if default is not None:
                wasapi = next((d for d in devices if "WASAPI" in d.hostapi and d.name == default.name), None)
                if wasapi is not None:
                    return wasapi.index, wasapi.name
        except Exception:
            pass
        return None, "system default input"
    if isinstance(device_id, int) or (isinstance(device_id, str) and device_id.isdigit()):
        idx = int(device_id)
        sd = _sd()
        info = sd.query_devices(idx)
        return idx, str(info["name"]).strip()

    needle = str(device_id).lower()
    for dev in list_devices(probe_sample_rates=False):
        if needle in dev.name.lower():
            return dev.index, dev.name
    raise ValueError(f"no input device matching {device_id!r}")


def pick_sample_rate(index: int | None, channels: int, requested: int = 0) -> int:
    """Choose the capture sample rate: the requested one, else the device default."""
    sd = _sd()
    if requested:
        sd.check_input_settings(device=index, channels=channels, samplerate=requested, dtype="int16")
        return int(requested)
    info = sd.query_devices(index if index is not None else sd.default.device[0])
    rate = int(round(float(info.get("default_samplerate") or 48000)))
    if rate <= 0:
        rate = 48000
    sd.check_input_settings(device=index, channels=channels, samplerate=rate, dtype="int16")
    return rate
