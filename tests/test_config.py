"""Configuration loading, merging and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scientific_conference.config import Config, ConfigError, load_config, to_plain


def write_yaml(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_defaults_without_file(tmp_path):
    cfg = load_config(Path(__file__).resolve().parents[1] / "config" / "example.yaml", env=False)
    assert cfg.asr.backend == "faster_whisper"
    assert cfg.vad.backend == "silero"
    assert cfg.segmenter.turn_silence_ms == 4000
    assert cfg.runner.agent == "offline_extractive"


def test_file_overrides_and_nested_merge(tmp_path):
    path = write_yaml(
        tmp_path / "c.yaml",
        {
            "segmenter": {"turn_silence_ms": 1234},
            "asr": {"model": "medium"},
        },
    )
    cfg = load_config(path, env=False)
    assert cfg.segmenter.turn_silence_ms == 1234
    assert cfg.segmenter.grace_ms == 2500  # untouched sibling keeps its default
    assert cfg.asr.model == "medium"
    assert cfg.asr.beam_size == 5


def test_unknown_key_is_rejected(tmp_path):
    path = write_yaml(tmp_path / "c.yaml", {"asr": {"modell": "small"}})
    with pytest.raises(ConfigError) as exc:
        load_config(path, env=False)
    assert "modell" in str(exc.value)


def test_env_override(tmp_path, monkeypatch):
    path = write_yaml(tmp_path / "c.yaml", {"asr": {"device": "cpu"}})
    monkeypatch.setenv("SC_ASR__DEVICE", "cuda")
    monkeypatch.setenv("SC_SEGMENTER__TURN_SILENCE_MS", "4321")
    cfg = load_config(path, env=True)
    assert cfg.asr.device == "cuda"
    assert cfg.segmenter.turn_silence_ms == 4321


def test_azure_requires_upload_consent(tmp_path):
    path = write_yaml(tmp_path / "c.yaml", {"asr": {"backend": "azure"}})
    with pytest.raises(ConfigError) as exc:
        load_config(path, env=False)
    assert "upload_audio" in str(exc.value)


def test_cloud_notes_require_consent(tmp_path):
    path = write_yaml(
        tmp_path / "c.yaml",
        {"runner": {"agent": "openai_compatible", "openai": {"base_url": "http://x", "model": "m"}}},
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path, env=False)
    assert "allow_cloud_notes" in str(exc.value)


def test_cli_agent_requires_command(tmp_path):
    path = write_yaml(tmp_path / "c.yaml", {"runner": {"agent": "cli"}})
    with pytest.raises(ConfigError):
        load_config(path, env=False)


@pytest.mark.parametrize(
    "data",
    [
        {"asr": {"backend": "nope"}},
        {"vad": {"backend": "nope"}},
        {"diarization": {"backend": "azure"}},
        {"audio": {"chunk_seconds": 0}},
        {"vad": {"threshold": 1.5}},
    ],
)
def test_invalid_values(tmp_path, data):
    path = write_yaml(tmp_path / "c.yaml", data)
    with pytest.raises(ConfigError):
        load_config(path, env=False)


def test_derived_paths_and_hashes(cfg: Config, tmp_path):
    assert cfg.output_root == (tmp_path / "meetings").resolve()
    assert cfg.skill_path.name == "SKILL.md"
    assert cfg.models_dir == cfg.install_root / "models" / "scientific_conference"
    h1 = cfg.asr_config_hash()
    cfg.segmenter.turn_silence_ms += 1
    assert cfg.asr_config_hash() != h1
    assert len(h1) == 16


def test_example_config_is_loadable():
    example = Path(__file__).resolve().parents[1] / "config" / "example.yaml"
    cfg = load_config(example, env=False)
    plain = to_plain(cfg)
    assert plain["asr"]["backend"] == "faster_whisper"
    assert plain["privacy"]["upload_audio"] is False


def test_live_asr_profiles_retain_cpu_fallback():
    from scientific_conference.asr.faster_whisper_backend import FasterWhisperBackend

    cfg = load_config(Path(__file__).resolve().parents[1] / "config" / "example.yaml", env=False)
    assert FasterWhisperBackend._candidate_settings(cfg.asr.device, cfg.asr.compute_type) == [
        ("cuda", "float16"), ("cpu", "int8")
    ]
