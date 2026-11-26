"""Configuration model, loading and validation.

Precedence (highest first):

1. explicit CLI arguments
2. ``SC_*`` environment variables
3. the ``--config`` YAML file
4. defaults defined here

Nothing in this module touches the network or the audio device.  Loading a
config is a pure, side-effect free operation so that it can be used from the
CLI, the MCP adapter and the runner alike.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

try:  # PyYAML is a hard dependency, but keep the error readable.
    import yaml
except Exception:  # pragma: no cover - only triggers on a broken install
    yaml = None  # type: ignore[assignment]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def default_install_root() -> Path:
    """External per-user directory for models, caches, and integration state."""
    env = os.environ.get("SC_INSTALL_ROOT")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "DSH-meeting"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "dsh-meeting"


def project_root() -> Path:
    """Root of this source checkout."""
    return Path(__file__).resolve().parents[2]


class ConfigError(ValueError):
    """Raised when a configuration file is unusable."""


def _coerce(cls: type, data: Any, path: str = "") -> Any:
    """Build a (possibly nested) dataclass from plain dict data.

    Unknown keys are an error: silent typos in a config file are a common
    source of "the setting did not apply" confusion.
    """
    if is_dataclass(cls):
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ConfigError(f"{path or cls.__name__}: expected a mapping, got {type(data).__name__}")
        known = {f.name: f for f in fields(cls)}
        unknown = set(data) - set(known)
        if unknown:
            raise ConfigError(
                f"{path or cls.__name__}: unknown key(s) {sorted(unknown)}; "
                f"known keys are {sorted(known)}"
            )
        kwargs = {}
        for name, f in known.items():
            if name in data:
                kwargs[name] = _coerce(_type_of(f), data[name], f"{path}{name}.")
        return cls(**kwargs)  # type: ignore[misc]
    return data


def _type_of(f: Any) -> type:
    """Resolve a dataclass field type.

    ``from __future__ import annotations`` turns every annotation into a string,
    so the name is evaluated in this module's namespace (annotations are our own
    source text, never user input).
    """
    t = f.type
    if not isinstance(t, str):
        return t
    cached = _TYPE_CACHE.get(t)
    if cached is not None:
        return cached
    import sys
    import typing

    namespace: dict[str, Any] = dict(vars(sys.modules[__name__]))
    namespace.update(vars(typing))
    try:
        resolved = eval(t, namespace)  # noqa: S307 - our own annotations only
    except Exception as exc:  # pragma: no cover - defensive
        raise ConfigError(f"cannot resolve annotation {t!r}: {exc}") from exc
    _TYPE_CACHE[t] = resolved
    return resolved


_TYPE_CACHE: dict[str, Any] = {}


def to_plain(obj: Any) -> Any:
    """Recursively convert dataclasses to plain JSON-serialisable structures."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_plain(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    return obj


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _deep_get(data: dict, dotted: str) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(dotted)
        cur = cur[part]
    return cur


def _deep_set(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = data
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
        if not isinstance(cur, dict):
            raise ConfigError(f"cannot set {dotted}: {part} is not a mapping")
    cur[parts[-1]] = value


def _env_overrides(data: dict) -> dict:
    """Apply ``SC_<SECTION>__<KEY>=value`` environment overrides.

    Values are parsed as YAML scalars so ``SC_ASR__DEVICE=cuda`` and
    ``SC_SEGMENTER__TURN_SILENCE_MS=4000`` both work.
    """
    out = copy.deepcopy(data)
    for name, raw in os.environ.items():
        if not name.startswith("SC_") or name in {"SC_INSTALL_ROOT", "SC_CONFIG"}:
            continue
        dotted = name[3:].lower().replace("__", ".")
        if yaml is not None:
            try:
                value: Any = yaml.safe_load(raw)
            except Exception:
                value = raw
        else:  # pragma: no cover
            value = raw
        _deep_set(out, dotted, value)
    return out


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------
@dataclass
class ProjectConfig:
    output_root: str = "./meetings"
    keep_audio_days: int = 0  # 0 == never delete
    locale: str = "zh-CN"


@dataclass
class AudioConfig:
    device_id: Any = "default"  # "default" | int index | device name substring
    sample_rate: int = 0  # 0 == whatever the device offers natively
    channels: int = 1
    chunk_seconds: float = 10.0
    queue_max_chunks: int = 900
    save_source_format: bool = True
    reconnect_attempts: int = 20
    reconnect_delay_s: float = 2.0


@dataclass
class AzureAsrConfig:
    region: str = ""
    key_env: str = "AZURE_SPEECH_KEY"
    language: str = "zh-CN"
    endpoint: str = ""


@dataclass
class QwenAsrConfig:
    model: str = "Qwen/Qwen3-ASR-0.6B"
    port: int = 8787
    language: str = "inherit"  # inherit | auto | zh | en
    chunk_seconds: float = 2.0
    max_utterance_seconds: float = 20.0
    timeout_seconds: float = 15.0


@dataclass
class AsrConfig:
    backend: str = "faster_whisper"  # faster_whisper | qwen3_asr | azure | mock
    model: str = "small"
    models_dir: str = ""  # filled from the install root when empty
    device: str = "auto"  # auto | cpu | cuda
    compute_type: str = "auto"  # auto | int8 | float16 | float32
    language_hints: list[str] = field(default_factory=lambda: ["zh", "en"])
    beam_size: int = 5
    temperature: float = 0.0
    #: Whisper's repetition guard only runs when faster-whisper receives more
    #: than one temperature: a decode whose text compresses better than
    #: ``compression_ratio_threshold`` (a repetition loop) or whose average log
    #: probability is below ``log_prob_threshold`` is retried at the next
    #: temperature.  ``False`` reproduces the old single-temperature behaviour
    #: for an exact rollback; see docs/13-whisper-repetition.md.
    temperature_fallback: bool = True
    compression_ratio_threshold: float = 2.4
    log_prob_threshold: float = -1.0
    no_speech_threshold: float = 0.6
    word_timestamps: bool = True
    initial_prompt: str = ""
    glossary_profile: str = ""  # empty disables conference-specific hints
    # streaming wrapper parameters
    utterance_silence_ms: int = 700
    max_window_ms: int = 20000
    min_segment_ms: int = 300
    max_asr_lag_ms: int = 30000
    azure: AzureAsrConfig = field(default_factory=AzureAsrConfig)
    qwen: QwenAsrConfig = field(default_factory=QwenAsrConfig)


@dataclass
class VadConfig:
    backend: str = "silero"  # silero | energy | none
    model_path: str = ""  # default: <models_dir>/silero_vad.onnx
    threshold: float = 0.5
    min_speech_ms: int = 250
    min_silence_ms: int = 400
    pad_ms: int = 200
    energy_threshold: float = 0.012


@dataclass
class DiarizationConfig:
    # Optional post-meeting worker. Never imported by the recorder/ASR environment.
    post_python: str = ""
    post_model: str = ""
    #: ``none``  -> every segment is labelled ``speaker_unknown`` (honest default)
    #: Azure's ConversationTranscriber is not wired into the windowed pipeline in
    #: this build, so it is deliberately not an accepted value here.
    backend: str = "none"
    threshold: float = 0.75  # minimum cosine similarity to join an existing cluster
    max_speakers: int = 8
    stickiness: float = 0.04


@dataclass
class SegmenterConfig:
    turn_silence_ms: int = 4000
    grace_ms: int = 2500
    speaker_change_hold_ms: int = 1500
    short_backchannel_ms: int = 900
    provisional_interval_ms: int = 90000
    talk_close_silence_ms: int = 20000
    #: When false (default) a talk only ends on long silence or an explicit
    #: boundary, so that a Q&A block stays attached to the talk it belongs to.
    auto_talk_from_speaker_change: bool = False


@dataclass
class NotesConfig:
    skill_path: str = ""  # default: <project>/skills/scientific-conference/SKILL.md
    language: str = "zh"
    keep_english_terms: bool = True
    default_mode: str = "turn"


@dataclass
class CliAgentConfig:
    command: list[str] = field(default_factory=list)
    timeout_s: float = 300.0
    prompt_via: str = "stdin"  # stdin | arg


@dataclass
class OpenAiAgentConfig:
    credential_name: str = ""  # Windows DPAPI credential reference, never the secret
    base_url: str = ""
    model: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    timeout_s: float = 180.0
    temperature: float = 0.2
    max_tokens: int = 0  # 0: omit; otherwise cap generated output per request
    compact_prompt: bool = False  # preserve every evidence segment, remove JSON indentation
    json_mode: bool = True  # turn off for compatible endpoints without response_format
    thinking: str = ""  # omit by default; DeepSeek accepts enabled/disabled
    require_api_key: bool = False


@dataclass
class WhisperTranslateConfig:
    model: str = ""  # empty = reuse asr.model
    device: str = "auto"
    compute_type: str = "auto"
    beam_size: int = 1


@dataclass
class TranslationConfig:
    """Realtime subtitles in another language.

    Backends:

    ``none``              翻译关闭。
    ``whisper_translate`` 复用本机 faster-whisper（``task=translate``），**离线、零新增依赖**，
                          但只能译成英文，且对每个片段重跑一次语音识别。
    ``openai_compatible`` 文本翻译，任意 ``/chat/completions`` 端点（需要密钥与
                          ``privacy.allow_cloud_notes``）。
    ``cli``               文本翻译，调用本机 agent CLI。
    """

    enabled: bool = False
    backend: str = "whisper_translate"
    target_language: str = "en"
    source_language: str = ""  # 空 = 自动
    poll_wait_ms: int = 1000
    #: 音频块尚未落盘时最多等待多久（毫秒），超过则记为 error 而不是静默卡住。
    audio_wait_ms: int = 30000
    max_attempts: int = 3
    #: 目标语言与识别语言相同（或检测为同一语言）时是否跳过翻译。
    skip_same_language: bool = True
    whisper: WhisperTranslateConfig = field(default_factory=WhisperTranslateConfig)
    openai: OpenAiAgentConfig = field(default_factory=OpenAiAgentConfig)
    cli: CliAgentConfig = field(default_factory=CliAgentConfig)


@dataclass
class RunnerConfig:
    agent: str = "offline_extractive"  # offline_extractive | cli | openai_compatible
    poll_wait_ms: int = 5000
    max_attempts: int = 4
    backoff_base_s: float = 5.0
    lease_seconds: int = 600
    max_events_per_cycle: int = 20
    consumer_id: str = "local-runner"
    cli: CliAgentConfig = field(default_factory=CliAgentConfig)
    openai: OpenAiAgentConfig = field(default_factory=OpenAiAgentConfig)


@dataclass
class PrivacyConfig:
    upload_audio: bool = False
    allow_cloud_notes: bool = False
    bind_host: str = "127.0.0.1"
    require_consent: bool = True


@dataclass
class DashboardConfig:
    """Which pages may embed the loopback panel (CSP ``frame-ancestors``).

    The panel listens on 127.0.0.1 only and every API call is checked against
    Host, Origin and a per-instance token, so letting other loopback origins
    frame the page does not hand out data or control. This exists because the
    DSH sidebar browser carries pages in an iframe whose origin (the DSH GUI
    port) can never equal the panel port; ``'self'`` alone renders blank.
    """

    embed_origins: list[str] = field(default_factory=lambda: ["http://127.0.0.1:*", "http://localhost:*"])


@dataclass
class Config:
    project: ProjectConfig = field(default_factory=ProjectConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    diarization: DiarizationConfig = field(default_factory=DiarizationConfig)
    segmenter: SegmenterConfig = field(default_factory=SegmenterConfig)
    notes: NotesConfig = field(default_factory=NotesConfig)
    translation: TranslationConfig = field(default_factory=TranslationConfig)
    runner: RunnerConfig = field(default_factory=RunnerConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)

    # -- derived paths ---------------------------------------------------
    @property
    def install_root(self) -> Path:
        return default_install_root()

    @property
    def models_dir(self) -> Path:
        p = self.asr.models_dir
        if p:
            return Path(p)
        return self.install_root / "models" / "scientific_conference"

    @property
    def cache_dir(self) -> Path:
        return self.install_root / "caches" / "scientific_conference"

    @property
    def output_root(self) -> Path:
        p = Path(self.project.output_root)
        if not p.is_absolute():
            p = project_root() / p
        return p.resolve()

    @property
    def skill_path(self) -> Path:
        if self.notes.skill_path:
            p = Path(self.notes.skill_path)
            return p if p.is_absolute() else (project_root() / p).resolve()
        return project_root() / "skills" / "scientific-conference" / "SKILL.md"

    @property
    def vad_model_path(self) -> Path:
        if self.vad.model_path:
            p = Path(self.vad.model_path)
            return p if p.is_absolute() else (self.models_dir / p)
        return self.models_dir / "silero_vad.onnx"

    # -- derived values --------------------------------------------------
    def asr_config_hash(self) -> str:
        """Hash of everything that can change transcription output.

        Used together with ``session+turn+revision+skill_version`` for task
        de-duplication, as required by the design document.
        """
        payload = to_plain(
            {
                "asr": self.asr,
                "vad": self.vad,
                "diarization": self.diarization,
                "segmenter": self.segmenter,
            }
        )
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16]

    def notes_config_hash(self) -> str:
        payload = to_plain({"notes": self.notes, "runner": self.runner})
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16]

    def translation_identity(self) -> str:
        """Identity of the translation setup (backend + target + model).

        Stored next to every translated segment so a change of engine or target
        language is visible in the data instead of silently mixing outputs.
        """
        payload = to_plain(
            {
                "backend": self.translation.backend,
                "target": self.translation.target_language,
                "source": self.translation.source_language,
                "whisper": self.translation.whisper,
                "openai_model": self.translation.openai.model,
            }
        )
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:12]


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
DEFAULT_CONFIG_FILES = (
    "config/local.yaml",
    "config/example.yaml",
)


def load_config(path: str | os.PathLike[str] | None = None, *, env: bool = True) -> Config:
    """Load, merge and validate the configuration."""
    raw: dict = {}
    chosen: Path | None = None
    if path:
        chosen = Path(path)
        if not chosen.exists():
            raise ConfigError(f"config file not found: {chosen}")
    elif os.environ.get("SC_CONFIG"):
        chosen = Path(os.environ["SC_CONFIG"])
        if not chosen.exists():
            raise ConfigError(f"SC_CONFIG not found: {chosen}")
    else:
        for candidate in DEFAULT_CONFIG_FILES:
            p = project_root() / candidate
            if p.exists():
                chosen = p
                break

    if chosen is not None:
        if yaml is None:  # pragma: no cover
            raise ConfigError("PyYAML is required to read a config file")
        try:
            raw = yaml.safe_load(chosen.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # pragma: no cover
            raise ConfigError(f"failed to parse {chosen}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{chosen}: top level must be a mapping")

    if env:
        raw = _env_overrides(raw)

    cfg = _coerce(Config, raw)
    _validate(cfg)
    return cfg


_VALID = {
    "asr.backend": {"faster_whisper", "qwen3_asr", "azure", "mock"},
    "asr.qwen.language": {"inherit", "auto", "zh", "en"},
    "asr.device": {"auto", "cpu", "cuda"},
    "asr.compute_type": {"auto", "int8", "int8_float16", "float16", "float32"},
    "vad.backend": {"silero", "energy", "none"},
    "diarization.backend": {"none"},
    "runner.agent": {"offline_extractive", "cli", "openai_compatible", "external"},
    "translation.backend": {"none", "whisper_translate", "openai_compatible", "cli"},
}


def _validate(cfg: Config) -> None:
    plain = to_plain(cfg)
    for dotted, allowed in _VALID.items():
        value = _deep_get(plain, dotted)
        if value not in allowed:
            raise ConfigError(f"{dotted}={value!r} is not one of {sorted(allowed)}")

    qwen = cfg.asr.qwen
    if qwen.model != "Qwen/Qwen3-ASR-0.6B":
        raise ConfigError("asr.qwen.model must be Qwen/Qwen3-ASR-0.6B")
    if type(qwen.port) is not int or not 1024 <= qwen.port <= 65535:
        raise ConfigError("asr.qwen.port must be an integer between 1024 and 65535")
    for name, value, lower, upper in (
        ("chunk_seconds", qwen.chunk_seconds, 0.5, 4),
        ("max_utterance_seconds", qwen.max_utterance_seconds, 4, 60),
        ("timeout_seconds", qwen.timeout_seconds, 1, 60),
    ):
        if type(value) not in (int, float) or not lower <= value <= upper:
            raise ConfigError(f"asr.qwen.{name} must be between {lower} and {upper}")


    if cfg.asr.backend == "azure" and not cfg.privacy.upload_audio:
        raise ConfigError(
            "asr.backend=azure sends microphone audio to the cloud; set privacy.upload_audio=true to confirm"
        )
    if cfg.runner.agent == "openai_compatible" and not cfg.privacy.allow_cloud_notes:
        raise ConfigError(
            "runner.agent=openai_compatible sends transcripts to a remote model; "
            "set privacy.allow_cloud_notes=true to confirm"
        )
    if cfg.runner.agent == "cli" and not cfg.runner.cli.command:
        raise ConfigError("runner.agent=cli requires runner.cli.command")

    if cfg.translation.backend == "whisper_translate":
        if cfg.translation.target_language.lower() not in ("en", "eng", "english"):
            raise ConfigError(
                "translation.backend=whisper_translate 只能译成英文（Whisper 的 translate 任务只输出英文）；"
                "请设置 translation.target_language=en，或改用 openai_compatible / cli 后端"
            )
    elif cfg.translation.backend == "openai_compatible":
        if not cfg.translation.openai.base_url or not cfg.translation.openai.model:
            raise ConfigError("translation.backend=openai_compatible requires translation.openai.base_url and .model")
        if not cfg.privacy.allow_cloud_notes:
            raise ConfigError(
                "translation.backend=openai_compatible sends transcript text to a remote model; "
                "set privacy.allow_cloud_notes=true to confirm"
            )
    elif cfg.translation.backend == "cli" and not cfg.translation.cli.command:
        raise ConfigError("translation.backend=cli requires translation.cli.command")
    if not cfg.translation.target_language:
        raise ConfigError("translation.target_language must not be empty")

    for name, value in (
        ("audio.chunk_seconds", cfg.audio.chunk_seconds),
        ("vad.threshold", cfg.vad.threshold),
    ):
        if not isinstance(value, (int, float)):
            raise ConfigError(f"{name} must be a number")
    if cfg.audio.chunk_seconds <= 0:
        raise ConfigError("audio.chunk_seconds must be > 0")
    if not 0.0 < cfg.vad.threshold < 1.0:
        raise ConfigError("vad.threshold must be between 0 and 1")
    if cfg.segmenter.turn_silence_ms <= 0:
        raise ConfigError("segmenter.turn_silence_ms must be > 0")

    if not isinstance(cfg.asr.temperature_fallback, bool):
        raise ConfigError("asr.temperature_fallback must be true or false")
    if not 0.0 <= float(cfg.asr.temperature) <= 1.0:
        raise ConfigError("asr.temperature must be between 0 and 1")
    for name, value in (
        ("asr.compression_ratio_threshold", cfg.asr.compression_ratio_threshold),
        ("asr.log_prob_threshold", cfg.asr.log_prob_threshold),
        ("asr.no_speech_threshold", cfg.asr.no_speech_threshold),
    ):
        if value is not None and not isinstance(value, (int, float)):
            raise ConfigError(f"{name} must be a number or null")
    if cfg.asr.compression_ratio_threshold is not None and cfg.asr.compression_ratio_threshold <= 0:
        raise ConfigError("asr.compression_ratio_threshold must be > 0")


def dump_config(cfg: Config) -> str:
    return json.dumps(to_plain(cfg), indent=2, ensure_ascii=False)
