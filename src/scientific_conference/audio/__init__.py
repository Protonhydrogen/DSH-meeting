"""Audio acquisition layer."""

from .devices import DeviceInfo, audio_backend_status, list_devices, pick_sample_rate, resolve_device
from .resample import StreamingResampler, float_to_pcm16, pcm16_to_float, resample_fixed
from .sources import AudioSource, MicrophoneSource, SourceError, WavFileSource, open_source
from .wavio import read_wav, wav_info, write_wav_int16

__all__ = [
    "DeviceInfo",
    "audio_backend_status",
    "list_devices",
    "pick_sample_rate",
    "resolve_device",
    "StreamingResampler",
    "float_to_pcm16",
    "pcm16_to_float",
    "resample_fixed",
    "AudioSource",
    "MicrophoneSource",
    "SourceError",
    "WavFileSource",
    "open_source",
    "read_wav",
    "wav_info",
    "write_wav_int16",
]
