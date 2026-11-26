"""Command line interface.

Every command reuses the same business layer as the MCP adapter; nothing here
holds recording state of its own.  Diagnostics go to stdout for humans and to
stderr for machine-readable errors, and ``service`` (the internal worker) sends
its log to a file so a stdio MCP transport is never polluted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import SKILL_VERSION, __version__
from .config import ConfigError, dump_config, load_config, project_root
from .db import utc_now
from .events import EventQueue
from .service import (
    create_meeting,
    export_session,
    get_transcript,
    list_sessions,
    mark_boundary,
    open_store,
    run_service,
    session_status,
    start_session,
    stop_session,
    wait_until_ready,
    worker_health,
)


def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass


def _print(data: Any, as_json: bool = True) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(data)


def _emit(args: argparse.Namespace, data: dict[str, Any], human: str | None = None) -> int:
    if getattr(args, "json", False) or human is None:
        _print(data, as_json=True)
    else:
        print(human)
        if getattr(args, "verbose", False):
            _print(data, as_json=True)
    return 0


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_live(cfg, args) -> int:
    from .dashboard import launch_live
    result = launch_live(cfg, start=args.start, consent=args.consent,
                         title=args.title, session_id=args.session_id or "")
    if args.open_browser:
        import webbrowser
        webbrowser.open(result["dashboard_url"])
    return _emit(args, result)


def cmd_devices(cfg, args) -> int:
    from .audio import audio_backend_status, list_devices

    status = audio_backend_status()
    devices: list[dict[str, Any]] = []
    if status["available"]:
        try:
            devices = [d.to_dict() for d in list_devices(probe_sample_rates=not args.fast)]
        except Exception as exc:
            status = {"available": False, "error": f"{type(exc).__name__}: {exc}", "devices": 0}
    payload = {"backend": status, "devices": devices, "configured": cfg.audio.device_id}
    lines = [f"音频后端：{'可用' if status['available'] else '不可用'} {status.get('error') or ''}"]
    for dev in devices:
        mark = "*" if dev["is_default_input"] else " "
        rates = ",".join(str(r) for r in dev["supported_sample_rates"]) or "n/a"
        lines.append(
            f"{mark} [{dev['index']:>2}] {dev['name']} | {dev['hostapi']} | in={dev['max_input_channels']}"
            f" | default={dev['default_samplerate']:.0f}Hz | 16k可用={('16000' in rates)} | rates={rates}"
        )
    return _emit(args, payload, "\n".join(lines))


def cmd_new(cfg, args) -> int:
    """Create a meeting record before the conference; never opens the microphone."""
    if args.output_dir:
        cfg.project.output_root = args.output_dir
    info = create_meeting(
        cfg,
        title=args.title,
        agenda=args.agenda or "",
        presenter=args.presenter or "none",
        report_title=args.report_title or "none",
        scheduled_for=args.scheduled_for or "",
        location=args.location or "",
    )
    human = (
        f"已创建会议 {info['session_id']}（筹备中，未录音）\n"
        f"计划时间：{info.get('scheduled_for') or '未填写'}\n"
        f"输出目录：{info.get('output_dir')}\n"
        f"提示：填写报告信息与发言人后，用 conference start --session-id {info['session_id']} 开始录音。"
    )
    return _emit(args, info, human)


def cmd_start(cfg, args) -> int:
    if args.output_dir:
        cfg.project.output_root = args.output_dir
    if args.asr:
        cfg.asr.backend = args.asr
    if args.device is not None:
        cfg.audio.device_id = args.device
    if args.model:
        cfg.asr.model = args.model
    if args.language:
        cfg.asr.language_hints = [args.language]
    if args.upload_audio:
        cfg.privacy.upload_audio = True

    info = start_session(
        cfg,
        session_id=args.session_id,
        title=args.title or "",
        agenda=args.agenda or "",
        consent=args.consent,
        source=args.source,
        wav_path=args.wav,
        realtime=args.realtime,
        foreground=args.foreground,
    )
    if not args.foreground:
        state = wait_until_ready(cfg, info["session_id"], timeout_s=args.ready_timeout)
        info["state"] = state
        info["output_dir"] = str(cfg.output_root / info["session_id"])
        if state not in ("recording", "closed", "failed"):
            # The worker did not reach the recording state: say so explicitly
            # (plus the tail of its log) instead of leaving a silent "starting".
            health = worker_health(cfg, info["session_id"])
            info["error"] = (
                "录音工作进程未在超时内进入 recording 状态"
                + ("，且进程已退出" if not health["alive"] else "（进程仍在运行，可能在加载模型）")
            )
            info["worker"] = health
            print(json.dumps(info, ensure_ascii=False, indent=2), file=sys.stderr)
            if not health["alive"]:
                return 1
    if not args.no_runner:
        _maybe_start_runner(cfg, info["session_id"], args)
    human = (
        f"会话 {info['session_id']} 已启动（{info['mode']}），状态={info.get('state')}\n"
        f"输出目录：{info.get('output_dir')}\n"
        f"提示：录音期间可用 conference status / stop，后台 runner 会持续生成纪要。"
    )
    return _emit(args, info, human)


def _maybe_start_runner(cfg, session_id: str, args) -> None:
    """Start a detached runner unless the caller asked for the foreground one."""
    if args.runner == "none":
        return
    from .service import spawn_runner

    spawn_runner(cfg, session_id, args.agent)


def cmd_status(cfg, args) -> int:
    info = session_status(cfg, args.session_id)
    info["worker"] = worker_health(cfg, args.session_id)
    live = info.get("live") or {}
    human = (
        f"会话 {info['session_id']}：{info['state']}\n"
        f"设备：{info['session'].get('device_name')} @ {info['session'].get('sample_rate')}Hz\n"
        f"音频：{info['audio'].get('duration_ms', 0) / 1000:.1f}s / {info['audio'].get('chunks', 0)} 块"
        f" / 缺口 {info['audio'].get('gap_ms', 0)}ms\n"
        f"转写：{info['counts']['segments']} 片段 / {info['counts']['turns']} 轮次 / {info['counts']['talks']} 报告\n"
        f"事件：{info['counts']['events']}，纪要：{info['counts']['notes']}，任务：{info['tasks']}\n"
        f"积压：{live.get('backlog')}，当前轮次：{live.get('open_turn')}\n"
        f"工作进程：pid={info['worker']['pid']} alive={info['worker']['alive']}"
    )
    return _emit(args, info, human)


def cmd_stop(cfg, args) -> int:
    info = stop_session(cfg, args.session_id, timeout_s=args.timeout, wait=not args.no_wait)
    human = f"会话 {info['session_id']} 状态：{info['state']}"
    if info.get("pending"):
        human += "\n仍有待处理音频，服务会继续排空；可稍后再查 status。"
    return _emit(args, info, human)


def cmd_boundary(cfg, args) -> int:
    return _emit(args, mark_boundary(cfg, args.session_id, args.kind))




def cmd_transcript(cfg, args) -> int:
    info = get_transcript(
        cfg, args.session_id, turn_id=args.turn, talk_id=args.talk, include_superseded=args.all_revisions
    )
    if args.json:
        return _emit(args, info)
    lines = []
    for seg in info["segments"]:
        ms = seg["start_ms"]
        stamp = f"{ms // 60000:02d}:{(ms % 60000) / 1000:05.2f}"
        speaker = seg.get("speaker_display") or seg.get("speaker_id") or "unknown"
        lines.append(f"[{stamp}] {speaker}: {seg['text']}")
    print("\n".join(lines) if lines else "（无转写内容）")
    return 0


def cmd_export(cfg, args) -> int:
    info = export_session(cfg, args.session_id, args.format)
    human = "已写出：\n" + "\n".join(info["written"])
    return _emit(args, info, human)


def cmd_poll(cfg, args) -> int:
    store = open_store(cfg, args.session_id)
    queue = EventQueue(store.db)
    result = queue.poll(
        args.session_id, args.consumer, after_seq=args.after, limit=args.limit, wait_ms=args.wait_ms
    )
    return _emit(args, result)


def cmd_ack(cfg, args) -> int:
    store = open_store(cfg, args.session_id)
    queue = EventQueue(store.db)
    cursor = queue.ack(args.session_id, args.consumer, args.through)
    return _emit(args, {"session_id": args.session_id, "consumer": args.consumer, "acked_seq": cursor})


def cmd_save_notes(cfg, args) -> int:
    """Persist a note produced elsewhere (idempotent, revision-guarded)."""
    from .notes import normalise_note, render_markdown, validate_note

    store = open_store(cfg, args.session_id)
    payload = json.loads(Path(args.file).read_text(encoding="utf-8")) if args.file else json.loads(sys.stdin.read())
    errors = validate_note(payload)
    if errors:
        print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    note = normalise_note(payload, defaults={"mode": args.mode, "target_id": args.target})
    markdown = render_markdown(note, evidence={})
    key = args.idempotency_key or f"manual:{args.session_id}:{args.mode}:{args.target}:{utc_now()}"
    note_id, created, rejected = store.save_note(
        mode=args.mode,
        target_id=args.target,
        source_revision=args.revision,
        content_json=note,
        content_md=markdown,
        agent="external",
        config_hash="manual",
        idempotency_key=key,
        path=None,
        skill_version=args.skill_version or SKILL_VERSION,
    )
    if rejected:
        print(json.dumps({"ok": False, "rejected": rejected}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 3
    return _emit(args, {"ok": True, "note_id": note_id, "created": created})


def cmd_sessions(cfg, args) -> int:
    info = {"output_root": str(cfg.output_root), "sessions": list_sessions(cfg, limit=args.limit)}
    lines = [f"输出根目录：{cfg.output_root}"]
    for s in info["sessions"]:
        lines.append(
            f"- {s['session_id']} | {s.get('state')} | {s.get('created_utc')} | "
            f"时长 {s.get('counts', {}).get('duration_ms', 0) / 1000:.0f}s | 纪要 {s.get('counts', {}).get('notes', 0)}"
        )
    return _emit(args, info, "\n".join(lines))


def _fmt_clock(ms: int | None) -> str:
    ms = int(ms or 0)
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f"{h:d}:{m:02d}:{s:02d}.{milli // 100}" if h else f"{m:02d}:{s:02d}.{milli // 100}"


def cmd_follow(cfg, args) -> int:
    """Live view of the transcript and of the notes as they are produced.

    Reads the same session database the recorder writes to (SQLite WAL allows a
    concurrent reader), so it never disturbs the recording and needs no extra
    service.  Nothing is acknowledged: this is a read-only observer.
    """
    from .events import EventQueue

    store = open_store(cfg, args.session_id)
    queue = EventQueue(store.db)
    session_id = args.session_id

    last_segment_rowid = int(args.from_rowid or 0)
    last_seq = int(args.after_seq or 0)
    cursor = store.db.query_one(
        "SELECT COALESCE(MAX(rowid), 0) AS m FROM segments WHERE session_id=?", (session_id,)
    )
    if args.tail_only and not args.from_rowid and cursor:
        last_segment_rowid = int(cursor["m"])
    if args.tail_only and not args.after_seq:
        last_seq = queue.max_seq(session_id)

    started = time.monotonic()
    idle = 0
    exit_code = 0
    target_lang = (args.target_language or cfg.translation.target_language or "en").lower()
    translation_map: dict[str, dict] = {}
    translation_started = False
    if args.translate and not args.json:
        print(f"# 实时翻译：开启（目标语言 {target_lang}）", flush=True)
    if not args.json:
        print(f"# 跟踪会话 {session_id}（Ctrl+C 退出）", flush=True)
    try:
        while True:
            printed = 0

            if args.translate:
                if not translation_started and args.start_translation:
                    try:
                        from .service import spawn_translator, translation_status

                        status = translation_status(cfg, session_id)
                        if not status.get("alive"):
                            spawn_translator(cfg, session_id, target_language=target_lang)
                            if not args.json:
                                print(f"# 已启动翻译进程（{cfg.translation.backend} → {target_lang}）", flush=True)
                        translation_started = True
                    except Exception as exc:
                        if not args.json:
                            print(f"# 翻译进程启动失败：{exc}", flush=True)
                        translation_started = True
                translation_map = store.translation_map(target_lang)

            segments = store.db.query(
                "SELECT rowid, * FROM segments WHERE session_id=? AND rowid>? ORDER BY rowid LIMIT 200",
                (session_id, last_segment_rowid),
            )
            speakers = {row["speaker_id"]: row["display_name"] for row in store.speakers()}
            for row in segments:
                last_segment_rowid = int(row["rowid"])
                speaker = row["speaker_id"] or "unknown"
                display = speakers.get(speaker)
                label = f"{speaker}({display})" if display else speaker
                text = str(row["text"] or "")
                provisional = "" if row["is_final"] else "（临时）"
                if row["superseded_by"]:
                    continue  # replaced by a later revision
                translation = translation_map.get(str(row["segment_id"]))
                if args.json:
                    print(
                        json.dumps(
                            {
                                "kind": "segment",
                                "segment_id": row["segment_id"],
                                "start_ms": row["start_ms"],
                                "end_ms": row["end_ms"],
                                "speaker_id": speaker,
                                "speaker_display": display,
                                "turn_id": row["turn_id"],
                                "talk_id": row["talk_id"],
                                "revision": row["revision"],
                                "text": text,
                                "translation": (translation or {}).get("text"),
                                "translation_lang": (translation or {}).get("target_lang"),
                                "translation_state": (translation or {}).get("state"),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                else:
                    print(f"[{_fmt_clock(row['start_ms'])}] {label}: {text}{provisional}", flush=True)
                    if args.translate:
                        if translation and translation.get("state") == "error":
                            print(f"    ↳ [{target_lang}] ⚠ {translation.get('error')}", flush=True)
                        elif translation and str(translation.get("text") or "").strip():
                            print(f"    ↳ [{target_lang}] {translation['text']}", flush=True)
                        elif translation is None:
                            print(f"    ↳ [{target_lang}] （翻译中…）", flush=True)
                printed += 1

            batch = queue.poll(session_id, "follow-observer", after_seq=last_seq, limit=200, wait_ms=0)
            for event in batch["events"]:
                last_seq = int(event["seq"])
                etype = event.get("type")
                # The raw turn payload repeats every segment; keep the stream
                # readable and emit just the summary in JSON mode.
                slim = {k: v for k, v in event.items() if k != "segments"}
                if etype == "turn.finalized":
                    if not args.json:
                        print(
                            f"── 轮次 {event.get('turn_id')} 结束"
                            f"（{event.get('boundary_reason')}，{_fmt_clock(event.get('start_ms'))}–"
                            f"{_fmt_clock(event.get('end_ms'))}，{event.get('segment_count', 0)} 段）",
                            flush=True,
                        )
                    elif args.json:
                        print(json.dumps({"kind": "event", **slim}, ensure_ascii=False), flush=True)
                elif etype == "segment.translated":
                    if not args.json:
                        if event.get("state") == "error":
                            print(
                                f"⚠ 翻译失败 {event.get('segment_id')}：{event.get('error')}",
                                flush=True,
                            )
                        else:
                            print(
                                f"↳ [{event.get('target_lang')}] {event.get('text')}",
                                flush=True,
                            )
                    else:
                        print(json.dumps({"kind": "event", **slim}, ensure_ascii=False), flush=True)
                elif etype == "talk.closed":
                    if not args.json:
                        print(f"══ 报告 {event.get('talk_id')} 结束（{event.get('boundary_reason')}）", flush=True)
                    else:
                        print(json.dumps({"kind": "event", **slim}, ensure_ascii=False), flush=True)
                elif etype == "session.closed":
                    if not args.json:
                        print(f"■ 会话结束（{event.get('reason')}，时长 {_fmt_clock(event.get('duration_ms'))}）", flush=True)
                    else:
                        print(json.dumps({"kind": "event", **slim}, ensure_ascii=False), flush=True)
                elif etype in ("note.saved",):
                    path = event.get("path")
                    if not args.json:
                        print(
                            f"📝 纪要已保存 [{event.get('mode')} {event.get('target_id')}]"
                            f" r{event.get('source_revision')} → {path}",
                            flush=True,
                        )
                        if args.notes and path:
                            try:
                                print(Path(path).read_text(encoding="utf-8"), flush=True)
                            except Exception as exc:
                                print(f"   （读取失败：{exc}）", flush=True)
                    else:
                        print(json.dumps({"kind": "event", **slim}, ensure_ascii=False), flush=True)
                elif etype == "transcript.revised":
                    if not args.json:
                        print(
                            f"↻ 转写修订 {event.get('turn_id')} → revision {event.get('revision')}"
                            f"（{event.get('revision_reason') or event.get('boundary_reason')}）",
                            flush=True,
                        )
                    else:
                        print(json.dumps({"kind": "event", **slim}, ensure_ascii=False), flush=True)
                printed += 1

            state = store.session_state()
            idle = 0 if printed else idle + 1
            if args.max_cycles and idle >= args.max_cycles:
                break
            if state in ("closed", "failed") and printed == 0:
                idle = max(idle, 2)
                if args.max_cycles and idle >= args.max_cycles:
                    break
                if not args.follow_closed:
                    if not args.json:
                        print(f"# 会话已结束，跟踪退出（状态 {state}）", flush=True)
                    break
            if args.duration and time.monotonic() - started >= args.duration:
                break
            time.sleep(max(0.2, args.interval))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        exit_code = 0
        if not args.json:
            print("# 已退出跟踪（会话与录音不受影响）", flush=True)
    except BrokenPipeError:  # pragma: no cover - e.g. ``conference follow | head``
        try:
            sys.stdout.close()
        except Exception:
            pass
        exit_code = 0
    return exit_code


def cmd_translate(cfg, args) -> int:
    """Run the realtime translation worker for a session (independent process)."""
    from .translation import TranslationWorker, build_translator, translation_state_path

    if args.backend:
        cfg.translation.backend = args.backend
    if args.target_language:
        cfg.translation.target_language = args.target_language
    store = open_store(cfg, args.session_id)
    try:
        translator = build_translator(cfg)
    except Exception as exc:
        print(
            json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    worker = TranslationWorker(cfg, store, translator, target_language=cfg.translation.target_language)
    if args.once:
        summary = worker.run_cycle(limit=args.limit)
        return _emit(args, {"session_id": args.session_id, **worker.info(), **summary})
    return worker.run_forever(
        follow=args.follow,
        max_idle_cycles=args.max_idle,
        state_file=translation_state_path(cfg, args.session_id),
    )


def cmd_runner(cfg, args) -> int:
    from .agents import build_agent
    from .runner import Runner

    if args.agent:
        cfg.runner.agent = args.agent
    if args.once:
        cfg.runner.max_events_per_cycle = args.limit
    store = open_store(cfg, args.session_id)
    runner = Runner(cfg, store, consumer_id=args.consumer, agent=build_agent(cfg))
    if args.once:
        summary = runner.run_cycle()
        return _emit(args, {"session_id": args.session_id, "agent": runner.agent.name, **summary})
    code = runner.run_forever(follow=args.follow, max_idle_cycles=args.max_idle)
    return code


def cmd_service(cfg, args) -> int:
    return run_service(cfg, args.session_id, source=args.source, wav_path=args.wav, realtime=args.realtime)


def cmd_config(cfg, args) -> int:
    data = json.loads(dump_config(cfg))
    data["_derived"] = {
        "output_root": str(cfg.output_root),
        "skill_path": str(cfg.skill_path),
        "models_dir": str(cfg.models_dir),
        "vad_model_path": str(cfg.vad_model_path),
        "asr_config_hash": cfg.asr_config_hash(),
        "notes_config_hash": cfg.notes_config_hash(),
    }
    return _emit(args, data)


def cmd_skill(cfg, args) -> int:
    from .skill import load_skill

    doc = load_skill(cfg.skill_path)
    if args.print_body:
        print(doc.body)
        return 0
    return _emit(args, doc.info(), f"技能文件：{doc.path}\n版本：{doc.version}\n字符数：{len(doc.body)}")


def cmd_doctor(cfg, args) -> int:
    from .audio import audio_backend_status
    from .skill import load_skill
    from .vad import load_vad

    checks: dict[str, Any] = {}
    backend = audio_backend_status()
    checks["audio_backend"] = backend
    checks["output_root"] = {"path": str(cfg.output_root), "exists": cfg.output_root.exists(), "writable": os.access(cfg.output_root.parent, os.W_OK)}
    checks["skill"] = load_skill(cfg.skill_path).info()
    gate = None
    try:
        gate = load_vad(cfg)
        checks["vad"] = gate.info()
        checks["vad"]["fallbacks"] = getattr(gate, "fallback_notes", [])
    except Exception as exc:
        checks["vad"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        from .asr.faster_whisper_backend import resolve_local_model

        path = resolve_local_model(cfg.asr.model, cfg.models_dir, cfg.cache_dir)
        checks["asr_model_local"] = str(path) if path else None
    except Exception as exc:
        checks["asr_model_local"] = {"error": str(exc)}
    checks["install_root"] = str(cfg.install_root)
    checks["python"] = sys.version
    ok = backend.get("available") and "error" not in checks["vad"]
    return _emit(args, {"ok": bool(ok), "checks": checks})


def cmd_selftest(cfg, args) -> int:
    from .selftest import run_selftest

    result = run_selftest(cfg, workdir=args.workdir, keep=args.keep, asr=args.asr, wav=args.wav, diarization="none")
    code = 0 if result.get("ok") else 1
    if args.json:
        _print(result, as_json=True)
    return code


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conference",
        description="学术会议实时录音与科研纪要（本地优先）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  conference doctor\n"
            "  conference devices\n"
            "  conference start --title \"组会\" --consent\n"
            "  conference status --session-id sess_...\n"
            "  conference stop --session-id sess_...\n"
            "  conference selftest\n"
        ),
    )
    parser.add_argument("--config", help="YAML 配置文件路径（默认 config/local.yaml 或 config/example.yaml）")
    parser.add_argument(
        "--output-root",
        "--output-dir",
        dest="output_root",
        help="会议产物根目录（覆盖配置；等同于设置 project.output_root）",
    )
    parser.add_argument("--json", action="store_true", help="始终输出 JSON")
    parser.add_argument("--verbose", action="store_true", help="人类可读输出后附加 JSON")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("live", help="一步启动实时转写/纪要面板，可同时开始录音")
    p.add_argument("--start", action="store_true", help="同时启动录音与纪要后台")
    p.add_argument("--consent", action="store_true", help="本次录音已获许可")
    p.add_argument("--title", default="")
    p.add_argument("--session-id", help="打开已有会议，不开始新录音")
    p.add_argument("--open-browser", action="store_true", help="用默认浏览器打开面板")
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("devices", help="枚举录音设备")
    p.add_argument("--fast", action="store_true", help="跳过采样率探测")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("new", help="提前创建会议（只建记录，不打开麦克风）")
    p.add_argument("--title", required=True, help="会议名称")
    p.add_argument("--presenter", default="", help="报告人")
    p.add_argument("--report-title", default="", help="报告题目")
    p.add_argument("--scheduled-for", default="", help="计划时间（ISO 8601，例如 2026-11-02T14:00）")
    p.add_argument("--location", default="", help="地点")
    p.add_argument("--agenda", default="", help="议程")
    p.add_argument("--output-dir", help="会议输出根目录（默认 ./meetings）")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("start", help="开始录音会话（--session-id 可复用提前创建的会议）")
    p.add_argument("--session-id")
    p.add_argument("--title", default="")
    p.add_argument("--agenda", default="")
    p.add_argument("--consent", action="store_true", help="确认已获得录音许可")
    p.add_argument("--source", choices=["microphone", "system", "file"], default="microphone")
    p.add_argument("--wav", help="--source file 时的输入音频")
    p.add_argument("--realtime", action="store_true", help="按真实时间回放文件")
    p.add_argument("--foreground", action="store_true", help="在当前进程内录音（默认后台进程）")
    p.add_argument("--device", help="设备索引/名称/默认")
    p.add_argument("--asr", choices=["faster_whisper", "qwen3_asr", "mock", "azure"])
    p.add_argument("--model", help="faster-whisper 模型名，例如 small/medium")
    p.add_argument("--language", help="主语言提示，例如 zh 或 en")
    p.add_argument("--output-dir", help="会议输出根目录（默认 ./meetings）")
    p.add_argument("--upload-audio", action="store_true", help="确认允许云端 ASR 上传音频")
    p.add_argument("--ready-timeout", type=float, default=60.0)
    p.add_argument("--runner", choices=["auto", "none"], default="auto", help="是否同时启动后台 runner")
    p.add_argument("--agent", choices=["offline_extractive", "cli", "openai_compatible"], help="runner 使用的 agent")
    p.add_argument("--no-runner", action="store_true", help="不启动 runner")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("status", help="查询会话状态")
    p.add_argument("--session-id", required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("stop", help="停止录音并排空")
    p.add_argument("--session-id", required=True)
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument("--no-wait", action="store_true")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("boundary", help="人工标记轮次/报告边界")
    p.add_argument("--session-id", required=True)
    p.add_argument("--kind", choices=["turn", "talk", "session"], default="turn")
    p.set_defaults(func=cmd_boundary)

    p = sub.add_parser("transcript", help="读取稳定转写")
    p.add_argument("--session-id", required=True)
    p.add_argument("--turn")
    p.add_argument("--talk")
    p.add_argument("--all-revisions", action="store_true")
    p.set_defaults(func=cmd_transcript)

    p = sub.add_parser("export", help="导出转写/事件/纪要索引")
    p.add_argument("--session-id", required=True)
    p.add_argument("--format", choices=["md", "jsonl", "all"], default="all")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("poll", help="拉取事件（至少一次投递）")
    p.add_argument("--session-id", required=True)
    p.add_argument("--consumer", default="local-runner")
    p.add_argument("--after", type=int)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--wait-ms", type=int, default=0)
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("ack", help="确认事件已处理")
    p.add_argument("--session-id", required=True)
    p.add_argument("--consumer", default="local-runner")
    p.add_argument("--through", type=int, required=True)
    p.set_defaults(func=cmd_ack)

    p = sub.add_parser("save-notes", help="保存外部生成的结构化纪要")
    p.add_argument("--session-id", required=True)
    p.add_argument("--mode", choices=["turn", "talk", "session"], required=True)
    p.add_argument("--target")
    p.add_argument("--revision", type=int, default=1)
    p.add_argument("--file", help="JSON 文件；缺省从 stdin 读取")
    p.add_argument("--idempotency-key")
    p.add_argument("--skill-version")
    p.set_defaults(func=cmd_save_notes)

    p = sub.add_parser("sessions", help="列出历史会话")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("follow", help="实时跟踪转写与纪要（只读，不影响录音）")
    p.add_argument("--session-id", required=True)
    p.add_argument("--interval", type=float, default=1.0, help="轮询间隔秒（默认 1.0）")
    p.add_argument("--tail-only", action="store_true", help="只显示启动之后的内容（默认从会话开头补显示）")
    p.add_argument("--from-rowid", type=int, help="从指定片段序号之后开始")
    p.add_argument("--after-seq", type=int, help="从指定事件 seq 之后开始")
    p.add_argument("--notes", action="store_true", help="纪要生成时直接打印正文")
    p.add_argument("--translate", action="store_true", help="同时显示实时翻译")
    p.add_argument("--target-language", help="翻译目标语言（默认取配置）")
    p.add_argument(
        "--start-translation",
        action="store_true",
        help="若翻译进程未运行则自动启动（配合 --translate）",
    )
    p.add_argument("--follow-closed", action="store_true", help="会话结束后继续等待（默认结束后退出）")
    p.add_argument("--max-cycles", type=int, default=0, help="连续 N 个周期没有新内容就退出（0=不因空闲退出）")
    p.add_argument("--duration", type=float, default=0.0, help="最多跟踪多少秒后退出（0=不限制）")
    p.set_defaults(func=cmd_follow)

    p = sub.add_parser("translate", help="实时翻译工作进程（独立于录音与纪要）")
    p.add_argument("--session-id", required=True)
    p.add_argument("--target-language", help="目标语言，例如 en")
    p.add_argument("--backend", choices=["none", "whisper_translate", "openai_compatible", "cli"])
    p.add_argument("--once", action="store_true", help="只跑一个周期")
    p.add_argument("--follow", action="store_true", help="持续运行到会话结束")
    p.add_argument("--max-idle", type=int, default=3)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_translate)

    p = sub.add_parser("runner", help="运行纪要调度器")
    p.add_argument("--session-id", required=True)
    p.add_argument("--consumer", default="local-runner")
    p.add_argument("--agent", choices=["offline_extractive", "cli", "openai_compatible"])
    p.add_argument("--once", action="store_true", help="只跑一个周期")
    p.add_argument("--follow", action="store_true", help="持续运行直到会话关闭且任务完成")
    p.add_argument("--max-idle", type=int, default=2, help="非 follow 模式下允许的空转周期")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_runner)

    p = sub.add_parser("service", help="内部：前台运行录音服务")
    p.add_argument("--session-id", required=True)
    p.add_argument("--source", choices=["microphone", "system", "file"], default="microphone")
    p.add_argument("--wav")
    p.add_argument("--realtime", action="store_true")
    p.set_defaults(func=cmd_service)

    p = sub.add_parser("config", help="打印生效配置")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("skill", help="查看 scientific-conference 技能")
    p.add_argument("--print-body", action="store_true")
    p.set_defaults(func=cmd_skill)

    p = sub.add_parser("doctor", help="自检环境、模型与设备")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("selftest", help="合成音频端到端自测")
    p.add_argument("--workdir", help="自测输出目录（默认 <output_root>/_selftest）")
    p.add_argument("--keep", action="store_true", help="保留自测产物")
    p.add_argument("--asr", choices=["faster_whisper", "mock"], default="faster_whisper")
    p.add_argument("--wav", help="使用已有音频替代内置合成音频")
    p.set_defaults(func=cmd_selftest)

    return parser


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(json.dumps({"ok": False, "config_error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if getattr(args, "output_root", None):
        cfg.project.output_root = args.output_root
    try:
        return int(args.func(cfg, args) or 0)
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
