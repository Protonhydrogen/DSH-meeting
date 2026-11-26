"""CLI behaviour: argument handling, JSON output and the safety gates."""

from __future__ import annotations

import json

import pytest

from scientific_conference import cli
from scientific_conference.config import ConfigError


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0


def test_requires_a_subcommand(capsys):
    with pytest.raises(SystemExit):
        cli.main([])


def test_config_outputs_json(capsys, tmp_path):
    config = tmp_path / "c.yaml"
    config.write_text("project:\n  output_root: ./out\n", encoding="utf-8")
    code, out, _ = run_cli(capsys, "--config", str(config), "--json", "config")
    assert code == 0
    data = json.loads(out)
    assert data["asr"]["backend"] == "faster_whisper"
    assert data["_derived"]["skill_path"].endswith("SKILL.md")


def test_bad_config_reports_json_error(capsys, tmp_path):
    config = tmp_path / "c.yaml"
    config.write_text("asr:\n  backend: nope\n", encoding="utf-8")
    code, out, err = run_cli(capsys, "--config", str(config), "config")
    assert code == 2
    assert "config_error" in err


def test_missing_config_file(capsys):
    code, out, err = run_cli(capsys, "--config", "does-not-exist.yaml", "config")
    assert code == 2
    assert "not found" in err


def test_skill_command(capsys):
    code, out, _ = run_cli(capsys, "skill")
    assert code == 0
    assert "SKILL.md" in out
    assert "1.0.0" in out


def test_devices_command_runs(capsys):
    code, out, _ = run_cli(capsys, "--json", "devices", "--fast")
    assert code == 0
    data = json.loads(out)
    assert "backend" in data and "devices" in data


def test_doctor_reports_checks(capsys):
    code, out, _ = run_cli(capsys, "--json", "doctor")
    assert code == 0
    data = json.loads(out)
    assert "checks" in data
    assert data["checks"]["skill"]["name"] == "scientific-conference"
    assert "vad" in data["checks"]


def test_start_requires_consent_by_default(capsys, tmp_path):
    config = tmp_path / "c.yaml"
    config.write_text(f"project:\n  output_root: '{tmp_path / 'meetings'}'\nprivacy:\n  require_consent: true\n", encoding="utf-8")
    code, out, err = run_cli(capsys, "--config", str(config), "start", "--source", "file", "--wav", "missing.wav")
    assert code == 1
    assert "consent" in err.lower()


def test_sessions_lists_an_empty_root(capsys, tmp_path):
    config = tmp_path / "c.yaml"
    config.write_text(f"project:\n  output_root: '{tmp_path / 'meetings'}'\n", encoding="utf-8")
    code, out, _ = run_cli(capsys, "--config", str(config), "--json", "sessions")
    assert code == 0
    assert json.loads(out)["sessions"] == []


def test_stop_on_unknown_session_fails_cleanly(capsys, tmp_path):
    config = tmp_path / "c.yaml"
    config.write_text(f"project:\n  output_root: '{tmp_path / 'meetings'}'\n", encoding="utf-8")
    code, out, err = run_cli(capsys, "--config", str(config), "status", "--session-id", "nope")
    assert code == 1
    assert "not found" in err or "StoreError" in err


def test_process_alive_detects_lifecycle():
    import subprocess
    import sys

    from scientific_conference.service import process_alive

    assert process_alive(None) is False
    assert process_alive(999_999_999) is False
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    try:
        assert process_alive(proc.pid) is True
    finally:
        proc.terminate()
        proc.wait(timeout=20)
    assert process_alive(proc.pid) is False


def test_worker_health_reports_state_and_log(cfg, tmp_path):
    from scientific_conference.service import worker_health
    from scientific_conference.store import Store

    session_id = "sess_health"
    store = Store.create(cfg.output_root, session_id, cfg, device_id="file", consent=True)
    store.paths.log.write_text("booting\nhello from the worker\n", encoding="utf-8")
    health = worker_health(cfg, session_id)
    assert health["session_id"] == session_id
    assert health["state"] == "starting"
    assert health["alive"] is False  # no worker was started
    assert "hello from the worker" in health["log_tail"]
    assert health["log_path"].endswith("service.log")
