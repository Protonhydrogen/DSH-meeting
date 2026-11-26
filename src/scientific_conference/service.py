"""Control plane: create sessions, spawn the recorder process, query state.

The MCP adapter and the CLI both call into this module, which means the MCP
layer never owns the only copy of the recording state - the session database
does.  That is what lets a CLI, an MCP client and a local runner share one
session.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config, project_root, to_plain
from .db import session_dir_name, utc_now
from .events import EventQueue
from .recorder import Recorder, RecorderConfig
from .store import (
    STATE_CLOSED,
    STATE_DRAINING,
    STATE_FAILED,
    STATE_PLANNED,
    STATE_RECORDING,
    STATE_STARTING,
    Store,
    index_sessions,
    session_paths,
)

__all__ = [
    "start_session",
    "create_meeting",
    "update_meeting",
    "spawn_service",
    "run_service",
    "stop_session",
    "session_status",
    "list_sessions",
    "archive_session",
    "mark_boundary",
    "set_speaker",
    "get_transcript",
    "export_session",
    "open_store",
]


def open_store(cfg: Config, session_id: str) -> Store:
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session_id or ""):
        raise ValueError("无效的会议编号")
    return Store.open(cfg.output_root, session_id)


def _meeting_title(title: str) -> str:
    text = (title or "").strip()
    if not text:
        raise ValueError("会议名称不能为空")
    if len(text) > 300 or any(c in text for c in ("\n", "\r", "\x00")):
        raise ValueError("会议名称最多 300 个字符且需为单行文本")
    return text


def _meeting_agenda(agenda: str) -> str:
    text = agenda or ""
    if not isinstance(text, str) or len(text) > 4000:
        raise ValueError("议程最多 4000 个字符")
    return text


def _meeting_result(store: Store) -> dict[str, Any]:
    from . import meeting_metadata

    row = store.session()
    metadata = meeting_metadata.read(store)
    moment, source = meeting_metadata.meeting_clock(row, metadata["scheduled_for"])
    return {
        "session_id": store.session_id,
        "state": row["state"],
        "title": row.get("title") or "",
        "presenter": metadata["presenter"],
        "report_title": metadata["report_title"],
        "scheduled_for": metadata["scheduled_for"],
        "location": metadata["location"],
        "meeting_time": moment,
        "meeting_time_source": source,
    }


def create_meeting(
    cfg: Config,
    *,
    title: str,
    agenda: str = "",
    presenter: str = "none",
    report_title: str = "none",
    scheduled_for: str = "",
    location: str = "",
) -> dict[str, Any]:
    """Create a meeting record before the conference; never starts audio.

    ``started_utc`` stays NULL here on purpose: a pre-created meeting is not a
    recorded meeting, and the authoritative meeting time is when a recorder
    actually begins.
    """
    from . import meeting_metadata

    name = _meeting_title(title)
    plan = _meeting_agenda(agenda)
    scheduled = meeting_metadata.validate_scheduled(scheduled_for)
    place = meeting_metadata.validate_location(location)
    sid = session_dir_name()
    store = Store.create(
        cfg.output_root,
        sid,
        cfg,
        consent=False,
        title=name,
        agenda=plan,
        state=STATE_PLANNED,
    )
    try:
        meeting_metadata.mark_planned(store)
        if presenter not in ("", None, "none") or report_title not in ("", None, "none"):
            # A pre-created meeting has its own name; reporter fields are stored
            # beside it instead of replacing ``sessions.title``.
            meeting_metadata.save(store, presenter or "none", report_title or "none", display_title=False)
        meeting_metadata.save_schedule(store, scheduled_for=scheduled, location=place)
        store.write_manifest(extra={"scheduled_for": scheduled, "location": place})
        result = _meeting_result(store)
        result["output_dir"] = str(store.paths.root)
        return result
    finally:
        store.db.close()


def update_meeting(
    cfg: Config,
    session_id: str,
    *,
    title: str | None = None,
    agenda: str | None = None,
    presenter: str | None = None,
    report_title: str | None = None,
    scheduled_for: str | None = None,
    location: str | None = None,
) -> dict[str, Any]:
    """Edit meeting identity/plan. A live recording may only change reporter fields."""
    from . import meeting_metadata

    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session_id or ""):
        raise ValueError("无效的会议编号")
    if not session_paths(cfg.output_root, session_id).db.is_file():
        raise FileNotFoundError("会议不存在")
    store = open_store(cfg, session_id)
    try:
        row = store.session()
        active = (row["state"] in (STATE_STARTING, STATE_RECORDING, STATE_DRAINING)
                  and process_alive(row.get("pid")))
        if active and any(value is not None for value in (title, agenda, scheduled_for, location)):
            raise ValueError("录音进行中只能修改报告人和报告题目")
        with store.db.tx() as conn:
            if title is not None:
                conn.execute("UPDATE sessions SET title=?,updated_utc=? WHERE session_id=?",
                             (_meeting_title(title), utc_now(), session_id))
            if agenda is not None:
                conn.execute("UPDATE sessions SET agenda=?,updated_utc=? WHERE session_id=?",
                             (_meeting_agenda(agenda), utc_now(), session_id))
        if presenter is not None or report_title is not None:
            current = meeting_metadata.read(store)
            meeting_metadata.save(
                store,
                current["presenter"] if presenter is None else presenter,
                current["report_title"] if report_title is None else report_title,
                display_title=False,
            )
        if scheduled_for is not None or location is not None:
            meeting_metadata.save_schedule(store, scheduled_for=scheduled_for, location=location)
        store.write_manifest(extra=meeting_metadata.read_schedule(store))
        return _meeting_result(store)
    finally:
        store.db.close()


def start_session(
    cfg: Config,
    *,
    session_id: str | None = None,
    title: str = "",
    agenda: str = "",
    consent: bool = False,
    source: str = "microphone",
    wav_path: str | None = None,
    realtime: bool = False,
    foreground: bool = False,
    device_name: str = "",
    dsh_auto_summary: bool = False,
) -> dict[str, Any]:
    """Create the session directory and start the recorder.

    When ``session_id`` names an existing *planned* meeting, its identity and
    creation time are retained and the recorder starts into that same record.
    With ``foreground=False`` (the default) the recorder runs in its own
    detached process, so the CLI command or MCP call returns immediately and
    the recording survives the caller exiting.
    """
    if cfg.privacy.require_consent and not consent:
        raise PermissionError(
            "recording consent not confirmed: pass consent=true after informing participants "
            "(set privacy.require_consent=false only if your policy allows it)"
        )
    if source not in {"microphone", "system", "file"}:
        raise ValueError("Invalid recording source")
    sid = session_id or session_dir_name()
    reused: dict[str, Any] | None = None
    if session_id:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session_id):
            raise ValueError("无效的会议编号")
        if not (cfg.output_root / sid / "session.sqlite3").is_file():
            raise ValueError("只有筹备中的会议可以从头开始录音（未找到该会议）")
        existing = Store.open(cfg.output_root, sid)
        try:
            row = existing.session()
            if row["state"] != STATE_PLANNED:
                raise ValueError("只有筹备中的会议可以从头开始录音")
            reused = {
                "created_utc": row.get("created_utc"),
                # The explicit meeting name survives the start of recording.
                "title": row.get("title") or title,
                "agenda": agenda or row.get("agenda") or "",
            }
        finally:
            existing.db.close()
    store = Store.create(
        cfg.output_root,
        sid,
        cfg,
        device_id=str(cfg.audio.device_id),
        device_name=device_name,
        sample_rate=cfg.audio.sample_rate,
        channels=cfg.audio.channels,
        consent=consent,
        title=reused["title"] if reused else title,
        agenda=reused["agenda"] if reused else agenda,
        state=STATE_STARTING,
        created_utc=reused["created_utc"] if reused else None,
    )
    with store.db.tx() as conn:
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('capture_source',?)", (source,))
        if dsh_auto_summary and cfg.privacy.allow_cloud_notes:
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('dsh_auto_summary','1')")
    rec_cfg = RecorderConfig(
        session_id=sid,
        output_root=cfg.output_root,
        source=source,
        wav_path=wav_path,
        realtime=realtime,
    )
    if foreground:
        recorder = Recorder(cfg, store, rec_cfg)
        code = recorder.run()
        return {"session_id": sid, "mode": "foreground", "exit_code": code, "output_dir": str(store.paths.root)}

    # The exact configuration is snapshotted into the meeting directory and the
    # detached worker is started from it, so a meeting can always be traced back
    # to (and reproduced with) the settings that produced it.
    assert store.paths is not None
    snapshot = store.paths.root / "config.snapshot.yaml"
    _write_config_snapshot(cfg, snapshot)

    pid = spawn_service(
        cfg,
        sid,
        source=source,
        wav_path=wav_path,
        realtime=realtime,
        config_path=str(snapshot),
    )
    store.set_pid(pid)
    return {
        "session_id": sid,
        "mode": "background",
        "pid": pid,
        "output_dir": str(store.paths.root),
        "state": store.session_state(),
        "config_snapshot": str(snapshot),
    }


def _write_config_snapshot(cfg: Config, path: Path) -> None:
    """Write the effective configuration next to the meeting data."""
    try:
        import yaml

        from .config import to_plain

        data = to_plain(cfg)
        data.setdefault("project", {})["output_root"] = str(cfg.output_root)
        path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    except Exception:  # pragma: no cover - snapshot is a convenience, not a gate
        pass


def spawn_service(
    cfg: Config,
    session_id: str,
    *,
    source: str = "microphone",
    wav_path: str | None = None,
    realtime: bool = False,
    config_path: str | None = None,
) -> int:
    """Start ``scientific_conference service`` as a detached process."""
    paths = session_paths(cfg.output_root, session_id)
    src_dir = Path(__file__).resolve().parents[1]
    cmd = [sys.executable, "-m", "scientific_conference"]
    if config_path:
        cmd += ["--config", str(config_path)]
    # ``--output-root`` is a global option and must precede the sub-command.
    cmd += ["--output-root", str(cfg.output_root)]
    cmd += [
        "service",
        "--session-id",
        session_id,
        "--source",
        source,
    ]
    if wav_path:
        cmd += ["--wav", str(wav_path)]
    if realtime:
        cmd.append("--realtime")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    # Keep Python bytecode out of the project directory (AGENTS.md).
    env.setdefault("PYTHONPYCACHEPREFIX", str(cfg.cache_dir / "pycache"))
    env.setdefault("HF_HOME", str(cfg.install_root / "cache" / "huggingface"))
    env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    env.setdefault("HF_HUB_OFFLINE", "1")
    if config_path:
        env["SC_CONFIG"] = str(config_path)
    else:
        env.pop("SC_CONFIG", None)

    log = paths.log.open("a", encoding="utf-8")
    try:
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            )
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            cwd=str(project_root()),
            env=env,
            close_fds=True,
            **kwargs,
        )
    finally:
        log.close()
    return int(proc.pid)


def spawn_runner(cfg: Config, session_id: str, agent: str | None = None) -> int:
    """Run notes independently, using the recorder's exact configuration."""
    if (agent or cfg.runner.agent) == "external":
        return 0  # The connected MCP client owns summarization and its cursor.
    paths = session_paths(cfg.output_root, session_id)
    cmd = [sys.executable, "-m", "scientific_conference",
           "--config", str(paths.root / "config.snapshot.yaml"),
           "--output-root", str(cfg.output_root), "runner",
           "--session-id", session_id, "--follow"]
    if agent:
        cmd += ["--agent", agent]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    with (paths.root / "runner.log").open("a", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                env=env, cwd=str(project_root()), **kwargs)
    (paths.root / "runner.pid").write_text(str(proc.pid), encoding="ascii")
    return proc.pid


def spawn_translator(cfg: Config, session_id: str, *, target_language: str | None = None) -> int:
    """Run the realtime translation worker for one session (independent process)."""
    from .translation import write_translation_state

    paths = session_paths(cfg.output_root, session_id)
    target = (target_language or cfg.translation.target_language or "en").lower()
    write_translation_state(
        cfg, session_id, enabled=True, target_language=target, backend=cfg.translation.backend
    )
    cmd = [
        sys.executable,
        "-m",
        "scientific_conference",
        "--config",
        str(paths.root / "config.snapshot.yaml"),
        "--output-root",
        str(cfg.output_root),
        "translate",
        "--session-id",
        session_id,
        "--target-language",
        target,
        "--follow",
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("HF_HOME", str(cfg.install_root / "cache" / "huggingface"))
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    with (paths.root / "translation.log").open("a", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, cwd=str(project_root()), **kwargs
        )
    (paths.root / "translation.pid").write_text(str(proc.pid), encoding="ascii")
    return proc.pid


def stop_translator(cfg: Config, session_id: str) -> dict[str, Any]:
    """Ask a running translation worker to exit (it stops after the current segment)."""
    from .translation import read_translation_state, write_translation_state

    paths = session_paths(cfg.output_root, session_id)
    state = read_translation_state(cfg, session_id)
    write_translation_state(
        cfg,
        session_id,
        enabled=False,
        target_language=state.target_language or cfg.translation.target_language,
        backend=state.backend or cfg.translation.backend,
    )
    pid = None
    try:
        pid = int((paths.root / "translation.pid").read_text().strip())
    except (OSError, ValueError):
        pass
    return {"session_id": session_id, "enabled": False, "pid": pid, "alive": process_alive(pid)}


def translation_status(cfg: Config, session_id: str) -> dict[str, Any]:
    """What the dashboard shows about the translation worker."""
    from .translation import read_translation_state

    paths = session_paths(cfg.output_root, session_id)
    from .translation import TranslationState

    # Default to "off": a session that never started a translation worker must
    # not claim that translation is running.
    state = read_translation_state(cfg, session_id, default=TranslationState(enabled=False))
    pid = None
    try:
        pid = int((paths.root / "translation.pid").read_text().strip())
    except (OSError, ValueError):
        pass
    store = open_store(cfg, session_id)
    try:
        counts = store.translation_counts()
    finally:
        store.db.close()
    return {
        "enabled": bool(state.enabled),
        "backend": state.backend or cfg.translation.backend,
        "target_language": state.target_language or cfg.translation.target_language,
        "configured_backend": cfg.translation.backend,
        "configured_target": cfg.translation.target_language,
        "pid": pid,
        "alive": process_alive(pid),
        "counts": counts,
    }


def run_service(cfg: Config, session_id: str, *, source: str = "microphone", wav_path: str | None = None, realtime: bool = False) -> int:
    """Entry point of the detached recorder process."""
    store = open_store(cfg, session_id)
    rec_cfg = RecorderConfig(
        session_id=session_id,
        output_root=cfg.output_root,
        source=source,
        wav_path=wav_path,
        realtime=realtime,
    )
    recorder = Recorder(cfg, store, rec_cfg)
    return recorder.run()


def wait_until_ready(cfg: Config, session_id: str, timeout_s: float = 30.0) -> str:
    deadline = time.monotonic() + timeout_s
    store = open_store(cfg, session_id)
    while time.monotonic() < deadline:
        state = store.session_state()
        if state in (STATE_RECORDING, STATE_CLOSED, STATE_FAILED):
            return state
        time.sleep(0.2)
    return store.session_state()


def process_alive(pid: int | None) -> bool:
    """Whether a PID is still running (Windows; returns True when unknown)."""
    if not pid:
        return False
    if os.name != "nt":  # pragma: no cover - this project targets Windows
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:  # pragma: no cover
        return True


def worker_health(cfg: Config, session_id: str) -> dict[str, Any]:
    """Report whether the background worker is alive and what it last logged."""
    store = open_store(cfg, session_id)
    paths = session_paths(cfg.output_root, session_id)
    row = store.db.query_one("SELECT pid FROM sessions WHERE session_id=?", (session_id,))
    pid = int(row["pid"]) if row and row["pid"] else None
    tail = ""
    if paths.log.exists():
        try:
            lines = paths.log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            tail = "\n".join(lines[-6:])
        except Exception:  # pragma: no cover
            tail = ""
    return {
        "session_id": session_id,
        "state": store.session_state(),
        "pid": pid,
        "alive": process_alive(pid),
        "log_tail": tail,
        "log_path": str(paths.log),
    }


def stop_session(cfg: Config, session_id: str, *, timeout_s: float = 180.0, wait: bool = True) -> dict[str, Any]:
    """Ask the recorder to stop, then wait for it to drain."""
    store = open_store(cfg, session_id)
    state = store.session_state()
    if state in (STATE_CLOSED, STATE_FAILED):
        return {"session_id": session_id, "state": state, "already_stopped": True}
    store.push_control("stop", {})
    if not wait:
        return {"session_id": session_id, "state": store.session_state(), "requested": True}

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = store.session_state()
        if state in (STATE_CLOSED, STATE_FAILED):
            return {"session_id": session_id, "state": state, "pending": False}
        time.sleep(0.5)
    return {
        "session_id": session_id,
        "state": store.session_state(),
        "pending": True,
        "note": "still draining; audio and pending work are preserved",
    }


def session_status(cfg: Config, session_id: str) -> dict[str, Any]:
    store = open_store(cfg, session_id)
    events = EventQueue(store.db)
    out: dict[str, Any] = {
        "session_id": session_id,
        "state": store.session_state(),
        "session": {
            k: v
            for k, v in store.session().items()
            if k in ("created_utc", "started_utc", "ended_utc", "device_name", "sample_rate", "channels",
                     "asr_backend", "vad_backend", "diarization", "title", "last_error", "consent")
        },
        "audio": store.audio_stats(),
        "counts": {
            "segments": len(store.segments(include_superseded=True)),
            "turns": len(store.turns()),
            "talks": len(store.talks()),
            "events": events.max_seq(session_id),
            "notes": len(store.notes(active_only=True)),
        },
        "tasks": store.task_counts(),
    }
    from . import meeting_metadata
    schedule = meeting_metadata.read_schedule(store)
    moment, source = meeting_metadata.meeting_clock(store.session(), schedule["scheduled_for"])
    out.update(schedule)
    out["meeting_time"] = moment
    out["meeting_time_source"] = source
    try:
        out["translation"] = translation_status(cfg, session_id)
    except Exception:  # translation status must never break a status call
        out["translation"] = {"enabled": False, "backend": cfg.translation.backend}
    if store.paths is not None and store.paths.manifest.exists():
        status_file = store.paths.root / "status.json"
        if status_file.exists():
            try:
                live = json.loads(status_file.read_text(encoding="utf-8"))
                out["live"] = {
                    "updated_utc": live.get("updated_utc"),
                    "backlog": live.get("backlog"),
                    "totals": live.get("totals"),
                    "open_turn": live.get("open_turn"),
                    "open_talk": live.get("open_talk"),
                    "pid": live.get("pid"),
                    "last_error": live.get("last_error"),
                }
            except Exception:
                pass
    return out


def _meeting_moment(item: dict[str, Any]) -> float:
    """Sort key: recording start, then plan, then creation (never pre-creation first)."""
    value = item.get("started_utc") or item.get("scheduled_for") or item.get("created_utc") or ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.timestamp()
    except (TypeError, ValueError, OSError):
        return 0.0


def list_sessions(cfg: Config, *, limit: int = 50) -> list[dict[str, Any]]:
    sessions = index_sessions(cfg.output_root)
    # A meeting created days early must appear where it actually happened, not
    # where its record was created.
    sessions.sort(key=_meeting_moment, reverse=True)
    return sessions[:limit]


def archive_session(cfg: Config, session_id: str) -> dict[str, Any]:
    """Move an inactive meeting into the project-local recoverable trash."""
    if not session_id or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
                             for ch in session_id) or len(session_id) > 100:
        raise ValueError("无效的会话编号")
    paths = session_paths(cfg.output_root, session_id)
    if not paths.db.is_file():
        raise FileNotFoundError("会议不存在")

    store = Store.open(cfg.output_root, session_id)
    try:
        row = store.session()
        state = row["state"]
        if state not in (STATE_CLOSED, STATE_FAILED, STATE_PLANNED):
            # A killed/crashed recorder can leave the durable state as
            # starting/recording/draining forever. The dashboard presents
            # that condition as "interrupted", so it must also be removable.
            # Keep a short grace period to avoid racing a recorder that has
            # only just been created and has not written its PID yet.
            recorder_alive = process_alive(row.get("pid"))
            try:
                updated = datetime.fromisoformat(str(row.get("updated_utc") or "").replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                stale_seconds = (datetime.now(timezone.utc) - updated).total_seconds()
            except ValueError:
                stale_seconds = 0
            interrupted = state in ("starting", "recording", "draining") and not recorder_alive and stale_seconds >= 15
            if not interrupted:
                raise ValueError("会议仍在录音或处理剩余音频，请先停止并等待录音结束")
    finally:
        store.db.close()

    for name, label in (("runner.pid", "科研总结"), ("translation.pid", "翻译")):
        try:
            pid = int((paths.root / name).read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            continue
        if process_alive(pid):
            raise ValueError(f"{label}后台仍在运行，请等待完成后再删除")

    trash = Path(cfg.output_root) / "_trash"
    trash.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace("-", "").replace(":", "").replace(".", "")
    destination = trash / f"{session_id}--{stamp}"
    if destination.exists():
        destination = trash / f"{session_id}--{stamp}-{time.time_ns()}"
    # Windows refuses to rename a directory while another request, SQLite or
    # a background file-sync process still has a handle open. Dashboard reads are serialized with
    # deletion, but a handle that was opened just before the request can take
    # a moment to close, so retry the same atomic move briefly.
    deadline = time.monotonic() + 5
    while True:
        try:
            paths.root.rename(destination)
            break
        except PermissionError as exc:
            if time.monotonic() >= deadline:
                raise ValueError("无法移入回收站：Windows 拒绝移动会议目录。会议内容仍保留，请重试；若持续失败，需要检查文件句柄或目录权限。无需关闭当前页面。") from exc
            time.sleep(0.1)
    (destination / "deleted.json").write_text(
        json.dumps({
            "session_id": session_id,
            "deleted_utc": utc_now(),
            "original_path": str(paths.root),
            "recoverable": True,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "session_id": session_id,
        "deleted": True,
        "recoverable": True,
        "trash_path": str(destination),
    }


def mark_boundary(cfg: Config, session_id: str, kind: str = "turn", speaker_id: str | None = None) -> dict[str, Any]:
    if kind not in ("turn", "talk", "session"):
        raise ValueError("kind must be turn, talk or session")
    store = open_store(cfg, session_id)
    control_id = store.push_control("boundary", {"kind": kind, "speaker_id": speaker_id})
    return {"session_id": session_id, "control_id": control_id, "kind": kind, "state": store.session_state()}


def set_speaker(
    cfg: Config, session_id: str, speaker_id: str, display_name: str | None, source: str = "manual"
) -> dict[str, Any]:
    store = open_store(cfg, session_id)
    if source not in ("manual", "agenda", "auto"):
        raise ValueError("source must be manual, agenda or auto")
    if store.session_state() in (STATE_RECORDING,):
        control_id = store.push_control("speaker", {"speaker_id": speaker_id, "display_name": display_name, "source": source})
        return {"session_id": session_id, "control_id": control_id, "applied_live": True}
    store.set_speaker(speaker_id, display_name, source)
    return {"session_id": session_id, "applied_live": False}


def get_transcript(
    cfg: Config, session_id: str, *, turn_id: str | None = None, talk_id: str | None = None, include_superseded: bool = False
) -> dict[str, Any]:
    store = open_store(cfg, session_id)
    segments = store.segments(turn_id=turn_id, talk_id=talk_id, include_superseded=include_superseded)
    speakers = {row["speaker_id"]: row["display_name"] for row in store.speakers()}
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "talk_id": talk_id,
        "segment_count": len(segments),
        "speakers": speakers,
        "segments": [
            {
                "segment_id": s["segment_id"],
                "turn_id": s["turn_id"],
                "talk_id": s["talk_id"],
                "start_ms": s["start_ms"],
                "end_ms": s["end_ms"],
                "text": s["text"],
                "speaker_id": s["speaker_id"],
                "speaker_display": speakers.get(s["speaker_id"]),
                "is_final": bool(s["is_final"]),
                "revision": s["revision"],
                "flags": json.loads(s["flags"]) if s["flags"] else [],
                "superseded_by": s["superseded_by"],
            }
            for s in segments
        ],
    }


def export_session(cfg: Config, session_id: str, fmt: str = "md") -> dict[str, Any]:
    """Write derived artefacts (transcript/events/notes index) to the meeting dir."""
    store = open_store(cfg, session_id)
    assert store.paths is not None
    written: list[str] = []
    if fmt in ("jsonl", "all"):
        store.export_transcript_jsonl()
        store.export_events_jsonl()
        written += [str(store.paths.transcript_jsonl), str(store.paths.events_jsonl)]
    if fmt in ("md", "all"):
        index_lines = [f"# 会议纪要索引 {session_id}", ""]
        for note in store.notes(active_only=True):
            index_lines.append(f"## {note['mode']} {note['target_id']} (revision {note['source_revision']})")
            index_lines.append("")
            index_lines.append(note["content_md"])
            index_lines.append("")
        index_path = store.paths.root / "conference_summary.md"
        index_path.write_text("\n".join(index_lines), encoding="utf-8")
        written.append(str(index_path))
    manifest = store.write_manifest()
    written.append(str(store.paths.manifest))
    return {"session_id": session_id, "written": written, "counts": manifest["counts"]}
