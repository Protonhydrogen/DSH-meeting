"""Local live dashboard. Standard library only; no CDN or frontend dependencies."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request, build_opener, ProxyHandler

from .asr import RECORDING_BACKENDS, select_recording_backend
from .asr.base import AsrError
from .asr.qwen import QWEN_BACKEND, QwenBackend
from .config import load_config, project_root, to_plain
from .service import (start_session, create_meeting, update_meeting, spawn_runner,
                      spawn_translator, stop_translator,
                      translation_status, process_alive, open_store,
                      mark_boundary, stop_session, archive_session, _write_config_snapshot)
from .store import STATE_PLANNED


def restricted_windows_process():
    """Detect restricted tokens without elevating or changing permissions."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.IsTokenRestricted.argtypes = [wintypes.HANDLE]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise OSError("无法检查录音服务的 Windows 运行权限")
    try:
        return bool(advapi.IsTokenRestricted(token))
    finally:
        kernel.CloseHandle(token)


def _meeting_sort_key(item):
    """Recording start first, then plan, then creation (pre-creation is not a meeting time)."""
    from datetime import datetime

    value = item.get("started_utc") or item.get("scheduled_for") or item.get("created_utc") or ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.timestamp()
    except (TypeError, ValueError, OSError):
        return 0.0


def checked_store(cfg, sid):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", sid or ""):
        raise ValueError("无效的会话编号")
    if not (cfg.output_root / sid / "session.sqlite3").is_file():
        raise FileNotFoundError("会议不存在")
    return open_store(cfg, sid)


def runner_alive(store):
    """Return whether this session's saved runner still looks reusable.

    Closed-session runners exit after draining their queue.  Their PID file is
    intentionally retained for diagnostics, but Windows can later reuse that
    PID for an unrelated process.  Treat an old PID file on a closed meeting as
    stale even when the numeric PID happens to be alive.
    """
    try:
        pid_path = store.paths.root / "runner.pid"
        pid = int(pid_path.read_text().strip())
        alive = process_alive(pid)
        if alive and store.session_state() in ("closed", "failed"):
            alive = time.time() - pid_path.stat().st_mtime <= 15 * 60
        return alive
    except (OSError, ValueError):
        return False


def snapshot(cfg, sid):
    store = checked_store(cfg, sid)
    try:
        row = store.session()
        from .meeting_metadata import read as report_metadata, meeting_clock
        metadata=report_metadata(store)
        moment, moment_source = meeting_clock(row, metadata["scheduled_for"])
        live = {}
        try:
            live = json.loads((store.paths.root / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        notes = store.notes()
        from .review_layers import epoch
        current_review_epoch=epoch(store)
        runner_is_alive = runner_alive(store)
        config = json.loads(row["config_json"])
        translation = translation_status(cfg, sid)
        target_lang = (translation.get("target_language") or cfg.translation.target_language or "en").lower()
        # Only the fields the page renders: never ship the whole table.
        translations = {
            segment_id: {
                "text": item.get("text") or "",
                "target_lang": item.get("target_lang"),
                "state": item.get("state"),
                "error": item.get("error"),
                "backend": item.get("backend"),
                "updated_utc": item.get("updated_utc"),
                "human_confirmed":item.get("human_confirmed",False),
                "stale_source":item.get("stale_source",False),
                "review_revision":item.get("review_revision",0),
            }
            for segment_id, item in store.translation_map(target_lang).items()
        }
        from .reading import reading_blocks
        segments = store.segments()
        note_versions = {}
        for item in store.db.query("SELECT mode,IFNULL(target_id,'') AS target,COUNT(*) AS n FROM notes GROUP BY mode,IFNULL(target_id,'')"):
            note_versions[(item['mode'], item['target'])] = item['n']
        working = {r['type']: r['n'] for r in store.db.query("SELECT type,COUNT(*) AS n FROM tasks WHERE state='leased' GROUP BY type")}
        return {"ok": True, "display_schema_version": 1, "session_id": sid, "title": row.get("title"), **metadata,
                "created_utc": row.get("created_utc"), "started_utc": row.get("started_utc"),
                "meeting_time": moment, "meeting_time_source": moment_source,
                "scheduled_for": metadata["scheduled_for"], "location": metadata["location"],
                "working_tasks": working, "review_epoch":current_review_epoch,
                "reading_blocks": reading_blocks(segments, closed=row['state'] in ('closed','failed')),
                "state": row["state"], "error": row.get("last_error"),
                "language": config.get("notes", {}).get("language", cfg.notes.language),
                "asr_backend": (live.get("asr") or {}).get("backend", config.get("asr", {}).get("backend")),
                "capture_source": (store.db.query_one("SELECT value FROM meta WHERE key='capture_source'") or {"value": "microphone"})["value"],
                "auto_summary": bool(store.db.query_one("SELECT 1 FROM meta WHERE key='dsh_auto_summary'")),
                "recorder_alive": process_alive(row.get("pid")), "runner_alive": runner_is_alive,
                "live": live, "tasks": store.task_counts(), "segments": segments,
                "summary_retry": dict(store.db.query_one(
                    "SELECT state,available_at_utc,attempts FROM tasks WHERE state IN ('failed','dead') ORDER BY available_at_utc LIMIT 1") or {}),
                "summary_interval_ms": int((store.db.query_one("SELECT value FROM meta WHERE key='summary_interval_ms'") or {'value':config.get('segmenter',{}).get('provisional_interval_ms',30000)})['value']),
                "notes": [{**{k: n.get(k) for k in ("note_id", "mode", "target_id", "source_revision", "content_md", "content_json", "agent", "updated_utc")},
                           "stale_review":json.loads(n.get("content_json") or "{}").get("_review_epoch",0)!=current_review_epoch,
                           "version_count": note_versions.get((n['mode'], n.get('target_id') or ''), 1)} for n in notes],
                "agent": config.get("runner", {}).get("agent", "unknown"),
                "translation": translation,
                "translations": translations,
                "translation_target": target_lang,
                "output_dir": str(store.paths.root)}
    finally:
        store.db.close()


def summary_interval(cfg):
    try:
        value=json.loads((cfg.output_root/'_live'/'preferences.json').read_text(encoding='utf-8'))['summary_interval_seconds']
        if type(value) is int and 10<=value<=300: return value
    except (OSError,ValueError,KeyError,TypeError): pass
    return 30


def frame_ancestors(cfg) -> str:
    """CSP ``frame-ancestors`` source list for pages loaded from this panel.

    ``'self'`` alone blocks every other origin, which is why the DSH sidebar
    browser (an iframe on the DSH GUI port) rendered an empty frame: different
    port means different origin. Configured loopback origins are added, and so
    is the GUI that launched this process (``DSH_WEB_URL``), because that port
    is assigned dynamically and cannot be hard-coded in a config file.

    DSH NEXT (Desktop) no longer serves its shell over http: the window is
    ``dsh-app://app/`` on a registered custom scheme, so no loopback source can
    ever match its origin and the Meetings panel stayed blank. The scheme itself
    is therefore allowed, and ``DSH_FRAME_ANCESTOR`` adds a concrete origin for
    other desktop shells.
    """
    sources = ["'self'"]
    for origin in cfg.dashboard.embed_origins or []:
        origin = str(origin).strip()
        if origin and origin not in sources:
            sources.append(origin)
    parts = urlsplit(os.environ.get("DSH_WEB_URL", "").strip())
    if parts.scheme in ("http", "https") and parts.netloc:
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in sources:
            sources.append(origin)
    for source in ("dsh-app:", os.environ.get("DSH_FRAME_ANCESTOR", "").strip()):
        if source and source not in sources:
            sources.append(source)
    return " ".join(sources)


def external_link(value) -> str:
    """A public http(s) address that may be handed to the desktop's browser.

    The panel is framed by the DSH shell in a sandbox without ``allow-popups``, so
    ``target=_blank`` links are dropped by Chromium. Embedded panels therefore ask
    this local service to open the browser. Only public web addresses are accepted:
    no other schemes, no credentials, no loopback/local names and no whitespace.
    """
    if not isinstance(value, str):
        raise ValueError("请输入要打开的链接")
    text = value.strip()
    if not 2 < len(text) <= 2000 or any(char in text for char in "\r\n\t ") :
        raise ValueError("链接格式无效")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password:
        raise ValueError("只能打开 http/https 公开链接")
    host = (parts.hostname or "").strip(".").lower()
    if not host or host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith((".local", ".internal", ".localhost")):
        raise ValueError("只能打开公开链接，本机地址不受支持")
    return text


class Dashboard(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, cfg, token=None):
        self.cfg = cfg
        self.cfg.segmenter.provisional_interval_ms=summary_interval(cfg)*1000
        self.token = token or secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.active = None
        self.frame_ancestors = frame_ancestors(cfg)
        super().__init__(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    def start_recording(self, title="", consent=False, presenter="", report_title=None, session_id="",
                        source="microphone", language=None, dsh_auto_summary=False,
                        asr_backend=None, qwen_language=None, qwen_chunk_seconds=None,
                        word_timestamps=None):
        from .meeting_metadata import validate,save as save_metadata
        if source not in ("microphone", "system"):
            raise ValueError("Invalid recording source")
        if language not in (None, "zh", "en"):
            raise ValueError("Choose Chinese or English for this meeting")
        metadata=validate(presenter,report_title if report_title is not None else title) if report_title is not None or presenter else None
        target = str(session_id or "").strip()
        with self.lock:
            recording_cfg = copy.deepcopy(self.cfg)
            if asr_backend is not None:
                select_recording_backend(recording_cfg, asr_backend)
            if qwen_language is not None:
                if qwen_language not in ("inherit", "auto", "zh", "en"):
                    raise ValueError("无效的 Qwen 识别语言")
                recording_cfg.asr.qwen.language = qwen_language
            if qwen_chunk_seconds is not None:
                if type(qwen_chunk_seconds) not in (int, float) or not 0.5 <= qwen_chunk_seconds <= 4:
                    raise ValueError("Qwen 流式更新间隔须为 0.5 至 4 秒")
                recording_cfg.asr.qwen.chunk_seconds = qwen_chunk_seconds
            if word_timestamps is not None:
                if type(word_timestamps) is not bool:
                    raise ValueError("无效的 Whisper 词级时间戳设置")
                recording_cfg.asr.word_timestamps = word_timestamps
            recording_cfg.segmenter.provisional_interval_ms=summary_interval(self.cfg)*1000
            if language:
                recording_cfg.asr.language_hints = [language]
                recording_cfg.asr.azure.language = {"zh": "zh-CN", "en": "en-US"}[language]
                recording_cfg.notes.language = language
            if target:
                target_store = checked_store(self.cfg, target)
                try:
                    if target_store.session()["state"] != STATE_PLANNED:
                        raise ValueError("只有筹备中的会议可以从头开始录音")
                finally:
                    target_store.db.close()
            # Repeated clicks/calls reattach; also discover sessions after a dashboard restart.
            candidates = ([self.active] if self.active else [])
            candidates += [p.parent.name for p in self.cfg.output_root.glob("*/session.sqlite3")]
            for sid in dict.fromkeys(candidates):
                store = checked_store(self.cfg, sid)
                try:
                    row = store.session()
                    if row["state"] in ("starting", "recording", "draining") and process_alive(row.get("pid")):
                        if target and sid != target:
                            raise ValueError("已有会议正在录音，请先停止后再开始本会议")
                        existing_config = json.loads(row["config_json"])
                        existing_asr = existing_config.get("asr", {})
                        requested_asr = to_plain(recording_cfg.asr)
                        previous_source = (store.db.query_one("SELECT value FROM meta WHERE key='capture_source'") or {"value": "microphone"})["value"]
                        if source != previous_source or (language and existing_config.get("notes", {}).get("language") != language) or any(existing_asr.get(key) != requested_asr.get(key) for key in
                               ("backend", "model", "language_hints", "initial_prompt", "glossary_profile", "word_timestamps")) or (
                                   recording_cfg.asr.backend == QWEN_BACKEND and existing_asr.get("qwen") != requested_asr.get("qwen")):
                            raise ValueError("Active recording uses different ASR settings or audio source. Stop it before starting another recording.")
                        existing_runner = existing_config.get("runner", {})
                        requested_runner = to_plain(self.cfg.runner)
                        if (existing_runner.get("agent") != requested_runner.get("agent") or
                            (self.cfg.runner.agent == "openai_compatible" and
                             existing_runner.get("openai", {}) != requested_runner.get("openai", {}))):
                            raise ValueError("已有会议正在使用另一套总结配置，请先停止该会议，再开始新的模型总结会议。")
                        self.active = sid
                        self.start_translation(sid)
                        return {"ok": True, "session_id": sid, "reused": True, "state": row["state"]}
                finally:
                    store.db.close()
            # Validate the notes adapter before starting the microphone.
            from .agents import build_agent
            if self.cfg.runner.agent != "external":
                build_agent(recording_cfg)
            options = {}
            if source != "microphone":
                options["source"] = source
            if dsh_auto_summary and recording_cfg.runner.agent == "external":
                options["dsh_auto_summary"] = True
            if recording_cfg.asr.backend == QWEN_BACKEND:
                try:
                    QwenBackend(recording_cfg.asr.qwen).prepare()
                except AsrError as exc:
                    raise ValueError(f"Qwen3-ASR 尚未就绪，请先启动本地推理服务。{exc}") from exc
            info = start_session(
                recording_cfg,
                session_id=target or None,
                title=title if target else (title or ("Meeting" if language == "en" else "学术会议")),
                consent=consent,
                **options,
            )
            self.active = info["session_id"]
            try:
                if metadata:
                    store=checked_store(self.cfg,self.active)
                    try:save_metadata(store,*metadata,display_title=not bool(target))
                    finally:store.db.close()
                info["runner_pid"] = (0 if self.cfg.runner.agent == "external"
                                      else spawn_runner(self.cfg, self.active))
            except Exception:
                stop_session(self.cfg, self.active, wait=False)
                raise
            translated = self.start_translation(self.active)
            if translated:
                info["translation_pid"] = translated
            return {"ok": True, **info}

    def start_translation(self, sid, *, target_language=None, explicitly_enabled=False):
        """Start the translation worker when the configuration asks for it."""
        if (self.cfg.translation.backend or "none") == "none":
            return None
        if not self.cfg.translation.enabled and not explicitly_enabled:
            return None
        status = translation_status(self.cfg, sid)
        if status.get("alive"):
            return status.get("pid")
        try:
            return spawn_translator(self.cfg, sid, target_language=target_language)
        except Exception:
            return None

    def set_translation(self, sid, *, enabled: bool, target_language: str = ""):
        """Turn realtime translation on/off from the panel."""
        backend = (self.cfg.translation.backend or "none").lower()
        if enabled and backend == "none":
            raise ValueError(
                "未配置翻译后端。请在 config 里设置 translation.backend"
                "（whisper_translate 可离线译成英文，或 openai_compatible / cli）"
            )
        if enabled and backend == "whisper_translate":
            target = (target_language or self.cfg.translation.target_language or "en").lower()
            if not target.startswith("en"):
                raise ValueError("whisper_translate 只能译成英文；其他目标语言请配置 openai_compatible 或 cli 后端")
        if not enabled:
            return {"ok": True, **stop_translator(self.cfg, sid)}
        pid = self.start_translation(sid, target_language=target_language or None, explicitly_enabled=True)
        status = translation_status(self.cfg, sid)
        return {"ok": True, "pid": pid, **status}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log access tokens or transcript content.

    def respond(self, status, data, content_type="application/json; charset=utf-8"):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", f"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' blob:; media-src 'self' blob:; frame-ancestors {self.server.frame_ancestors}")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            return False
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            return False
        return secrets.compare_digest(self.headers.get("X-Conference-Token", ""), self.server.token)

    def do_GET(self):
        route = urlsplit(self.path)
        if route.path in ("/", "/app.js", "/notebook.js", "/assistant.js", "/workbench.js", "/workbench.css", "/review.js", "/embed.js", "/timeline.js", "/markdown.js", "/workspace.js", "/meeting-template.js"):
            if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
                return self.respond(403, {"error": "Invalid host"})
            name = "dashboard.html" if route.path == "/" else ("assistant.js" if route.path=="/assistant.js" else ("notebook.js" if route.path=="/notebook.js" else "dashboard.js"))
            if route.path in ("/workbench.js", "/workbench.css", "/review.js", "/embed.js", "/timeline.js", "/markdown.js", "/workspace.js", "/meeting-template.js"):
                name = route.path[1:]
            mime = "text/html; charset=utf-8" if route.path == "/" else ("text/css; charset=utf-8" if route.path.endswith(".css") else "text/javascript; charset=utf-8")
            return self.respond(200, Path(__file__).with_name(name).read_bytes(), mime)
        if not self.authorized():
            return self.respond(403, {"error": "请使用 Agent 返回的完整面板链接"})
        try:
            if route.path == "/api/info":
                with self.server.lock:
                    sessions = []
                    for p in self.server.cfg.output_root.glob("*/session.sqlite3"):
                        store = checked_store(self.server.cfg, p.parent.name)
                        try:
                            row = store.session()
                            item = {k: row.get(k) for k in ("session_id", "title", "state", "created_utc", "started_utc")}
                            from .meeting_metadata import read as report_metadata, meeting_clock
                            item.update(report_metadata(store))
                            moment, source = meeting_clock(row, item.get("scheduled_for", ""))
                            item["meeting_time"] = moment
                            item["meeting_time_source"] = source
                            if row["state"] in ("starting", "recording", "draining") and not process_alive(row.get("pid")):
                                item["state"] = "interrupted"
                            sessions.append(item)
                        finally:
                            store.db.close()
                    sessions.sort(key=_meeting_sort_key, reverse=True)
                cfg = self.server.cfg
                credential_ready = True
                if cfg.runner.agent == 'openai_compatible' and cfg.runner.openai.require_api_key:
                    from .credentials import credential_path
                    credential_ready = bool(os.environ.get(cfg.runner.openai.api_key_env)) or bool(
                        cfg.runner.openai.credential_name and credential_path(cfg.runner.openai.credential_name).exists())
                return self.respond(200, {"ok": True, "sessions": sessions, "active": self.server.active,
                    "summary_credential_ready": credential_ready,
                    "summary_interval_seconds": summary_interval(cfg),
                    "asr_defaults": {"backend": cfg.asr.backend, "whisper_model": cfg.asr.model,
                                     "word_timestamps": cfg.asr.word_timestamps,
                                     "qwen_language": cfg.asr.qwen.language,
                                     "qwen_chunk_seconds": cfg.asr.qwen.chunk_seconds},
                    "agent": cfg.runner.agent, "device": str(cfg.audio.device_id),
                    "language": cfg.notes.language, "system_audio_available": sys.platform == "win32",
                    "cloud_audio": cfg.privacy.upload_audio, "cloud_notes": cfg.privacy.allow_cloud_notes,
                    "translation_backend": cfg.translation.backend,
                    "translation_target": cfg.translation.target_language,
                    "output_root": str(cfg.output_root)})
            if route.path == '/api/audio':
                from .playback import audio_clip
                query=parse_qs(route.query)
                store=checked_store(self.server.cfg,query.get('session_id',[''])[0])
                try:
                    body=audio_clip(store,int(query.get('start_ms',['0'])[0]),int(query.get('end_ms',['60000'])[0]))
                    return self.respond(200,body,'audio/wav')
                finally:store.db.close()
            if route.path in ('/api/notebook','/api/notebook/image'):
                from . import notebook
                query=parse_qs(route.query)
                with self.server.lock:
                    store=checked_store(self.server.cfg,query.get('session_id',[''])[0])
                    try:
                        if route.path.endswith('/image'):
                            body,mime=notebook.image(store,query.get('id',[''])[0])
                            return self.respond(200,body,mime)
                        return self.respond(200,notebook.read(store))
                    finally:
                        store.db.close()
            if route.path == "/api/snapshot":
                sid = parse_qs(route.query).get("session_id", [""])[0]
                with self.server.lock:
                    result = snapshot(self.server.cfg, sid)
                return self.respond(200, result)
            return self.respond(404, {"error": "Not found"})
        except (ValueError, FileNotFoundError) as exc:
            return self.respond(400, {"error": str(exc)})
        except Exception as exc:
            return self.respond(500, {"error": str(exc)})

    def do_POST(self):
        if not self.authorized():
            # On Windows, closing with unread POST bytes can reset the socket
            # before the client sees our 403. Drain only a bounded normal body.
            previous_timeout=self.connection.gettimeout()
            try:
                size=int(self.headers.get('Content-Length','0'))
                if 0<size<=17*1024*1024:
                    deadline=time.monotonic()+.25
                    while size and time.monotonic()<deadline:
                        self.connection.settimeout(max(.001,deadline-time.monotonic()))
                        part=self.rfile.read1(min(size,65536))
                        if not part:break
                        size-=len(part)
            except (OSError,ValueError):pass
            finally:self.connection.settimeout(previous_timeout)
            return self.respond(403, {"error": "Invalid origin or token"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            route_path = urlsplit(self.path).path
            upload=route_path=='/api/notebook/image'
            maximum=17*1024*1024 if upload else (1000000 if route_path=='/api/notebook' or route_path.startswith('/api/review/') else 16384)
            if not 0 < size <= maximum:
                raise ValueError("Invalid body size")
            data = json.loads(self.rfile.read(size))
            route = urlsplit(self.path).path
            if route.startswith('/api/review/'):
                from . import review_layers as review
                with self.server.lock:
                    store=checked_store(self.server.cfg,data.get('session_id',''))
                    try:
                        if route=='/api/review/read':result=review.details(store,data.get('segment_ids'))
                        elif route=='/api/review/save':result=review.save_batch(store,data.get('edits'))
                        elif route=='/api/review/undo':result=review.undo(store,data.get('segment_id'),data.get('field'),data.get('revision'))
                        else:return self.respond(404,{'error':'Not found'})
                        if route in ('/api/review/save','/api/review/undo'):
                            changed=data.get('edits',[]) if route.endswith('/save') else [data]
                            if any(x.get('field')=='transcript' for x in changed):
                                translated=translation_status(self.server.cfg,store.session_id)
                                if translated.get('enabled'):self.server.start_translation(store.session_id,target_language=translated.get('target_language'),explicitly_enabled=True)
                        return self.respond(200,{'ok':True,**result})
                    except review.ReviewConflict as exc:return self.respond(409,{'error':str(exc)})
                    finally:store.db.close()
            if route == '/api/meeting/metadata':
                from .meeting_metadata import save as save_metadata
                with self.server.lock:
                    store=checked_store(self.server.cfg,data.get('session_id',''))
                    try:
                        # A meeting pre-created via "new meeting" keeps its
                        # explicit name even after recording starts.
                        from .meeting_metadata import is_planned_origin
                        planned = store.session()["state"] == STATE_PLANNED or is_planned_origin(store)
                        return self.respond(200,{'ok':True,'session_id':store.session_id,**save_metadata(store,data.get('presenter',''),data.get('report_title',''),display_title=not planned)})
                    finally:store.db.close()
            if route == '/api/meetings/create':
                with self.server.lock:
                    result = create_meeting(
                        self.server.cfg,
                        title=str(data.get('title', '')),
                        agenda=str(data.get('agenda', '')),
                        presenter=str(data.get('presenter', 'none')),
                        report_title=str(data.get('report_title', 'none')),
                        scheduled_for=str(data.get('scheduled_for', '')),
                        location=str(data.get('location', '')),
                    )
                return self.respond(200, {'ok': True, **result})
            if route == '/api/meetings/update':
                sid = str(data.get('session_id', ''))
                fields = {}
                for key in ('title', 'agenda', 'presenter', 'report_title', 'scheduled_for', 'location'):
                    if key in data:
                        fields[key] = data[key] if isinstance(data[key], str) else str(data[key] or '')
                with self.server.lock:
                    result = update_meeting(self.server.cfg, sid, **fields)
                return self.respond(200, {'ok': True, **result})
            if route == '/api/open-external':
                # The sandboxed panel iframe cannot open popups; the local service
                # asks the operating system for the user's default browser.
                import webbrowser
                target = external_link(data.get('url'))
                opened = bool(webbrowser.open(target, new=2))
                return self.respond(200 if opened else 500, {'ok': opened, 'url': target} if opened else {'error': '系统未能打开浏览器'})
            if route == '/api/notebook/export':
                from .notebook import combined_markdown
                with self.server.lock:
                    store=checked_store(self.server.cfg,data.get('session_id',''))
                    try:return self.respond(200,{'markdown':combined_markdown(store)})
                    finally:store.db.close()
            if route in ('/api/notebook','/api/notebook/image'):
                from . import notebook
                with self.server.lock:
                    store=checked_store(self.server.cfg,data.get('session_id',''))
                    try:
                        result=notebook.add_image(store,data) if route.endswith('/image') else notebook.save(store,data.get('text'),data.get('revision'))
                        return self.respond(200,result)
                    except notebook.NotebookConflict as exc:
                        return self.respond(409,{'error':str(exc)})
                    finally:
                        store.db.close()
            if route == '/api/summary/interval':
                seconds=data.get('seconds')
                if type(seconds) is not int or not 10<=seconds<=300:
                    raise ValueError('Summary interval must be an integer from 10 to 300 seconds')
                with self.server.lock:
                    control=None
                    if data.get('session_id'):
                        store=checked_store(self.server.cfg,data['session_id'])
                        try:
                            row=store.session()
                            if row['state'] in ('starting','recording') and process_alive(row.get('pid')):
                                control=store.push_control('summary_interval',{'seconds':seconds})
                        finally: store.db.close()
                    path=self.server.cfg.output_root/'_live'/'preferences.json'
                    path.parent.mkdir(parents=True,exist_ok=True)
                    temp=path.with_suffix('.tmp')
                    temp.write_text(json.dumps({'summary_interval_seconds':seconds}),encoding='utf-8')
                    temp.replace(path)
                    self.server.cfg.segmenter.provisional_interval_ms=seconds*1000
                    return self.respond(200,{'ok':True,'seconds':seconds,'control_id':control,'queued':control is not None})
            if route == "/api/start":
                return self.respond(200, self.server.start_recording(
                    str(data.get("report_title", data.get("title", "")))[:300], data.get("consent") is True,
                    presenter=data.get("presenter", ""), report_title=data.get("report_title"),
                    session_id=str(data.get("session_id") or ""), source=data.get("source", "microphone"),
                    language=data.get("language"), dsh_auto_summary=data.get("dsh_auto_summary") is True,
                    asr_backend=data.get("asr_backend"), qwen_language=data.get("qwen_language"),
                    qwen_chunk_seconds=data.get("qwen_chunk_seconds"),
                    word_timestamps=data.get("word_timestamps")))
            sid = data.get("session_id", "")
            if route == "/api/history/delete":
                if data.get("confirm_session_id") != sid:
                    raise ValueError("删除确认与所选会议不一致")
                with self.server.lock:
                    result = archive_session(self.server.cfg, sid)
                    if self.server.active == sid:
                        self.server.active = None
                return self.respond(200, {"ok": True, **result})
            store = checked_store(self.server.cfg, sid)
            store.db.close()
            if route in ('/api/summary/retry', '/api/summary/overview', '/api/assistant/query'):
                with self.server.lock:
                    store=checked_store(self.server.cfg,sid)
                    try:
                        from .db import utc_now
                        config=json.loads(store.session()['config_json'])
                        if config.get('runner',{}).get('agent')!='openai_compatible':
                            raise ValueError('This meeting does not use the independent API summary backend')
                        from .runner import Runner
                        from .agents import OfflineExtractiveAgent
                        session_cfg=load_config(store.paths.root/'config.snapshot.yaml')
                        if not session_cfg.privacy.allow_cloud_notes:
                            raise ValueError('当前会议未允许云端模型处理')
                        # Scheduling is local; only the independent worker calls the model.
                        scheduler=Runner(session_cfg,store,agent=OfflineExtractiveAgent())
                        if route == '/api/assistant/query':
                            from .meeting_assistant import enqueue
                            overview=enqueue(store,data.get('instruction'),data.get('request_id'),data.get('update_map') is True,data.get('mode','strict'))
                            created=[]
                        else:
                            overview=scheduler.request_overview() if route == '/api/summary/overview' else {}
                            created=scheduler.poll_and_enqueue(limit=scheduler.events.max_seq(sid)+1)
                        with store.db.tx() as conn:
                            count=conn.execute("UPDATE tasks SET state='pending', attempts=0, available_at_utc=?, lease_owner=NULL, lease_expires_utc=NULL WHERE session_id=? AND state IN ('failed','dead') AND (type LIKE 'note.%' OR type IN ('assistant.query','transcript.review'))",(utc_now(),sid)).rowcount
                        alive=runner_alive(store)
                        pid=None if alive else spawn_runner(self.server.cfg,sid)
                        result={'requeued':count,'enqueued':len(created),'runner_reused':alive,'runner_pid':pid,**overview}
                    finally:
                        store.db.close()
            elif route == "/api/stop":
                result = stop_session(self.server.cfg, sid, wait=False)
            elif route == "/api/asr/switch":
                backend = data.get("backend")
                if backend not in RECORDING_BACKENDS:
                    raise ValueError("无效的转录模型")
                store = checked_store(self.server.cfg, sid)
                try:
                    row = store.session()
                    if row["state"] != "recording" or not process_alive(row.get("pid")):
                        raise ValueError("只有录音中可以切换转录模型")
                    if row["asr_backend"] not in RECORDING_BACKENDS:
                        raise ValueError("这场录音的转录后端不支持切换")
                    result = {"control_id": store.push_control("asr_switch", {"backend": backend}), "queued": True}
                finally:
                    store.db.close()
            elif route == "/api/boundary":
                result = mark_boundary(self.server.cfg, sid, data.get("kind", "turn"))
            elif route == "/api/translation":
                result = self.server.set_translation(
                    sid,
                    enabled=data.get("enabled") is True,
                    target_language=str(data.get("target_language") or "")[:16],
                )
            else:
                return self.respond(404, {"error": "Not found"})
            return self.respond(200, {"ok": True, **result})
        except (ValueError, PermissionError, FileNotFoundError) as exc:
            return self.respond(400, {"error": str(exc)})
        except Exception as exc:
            return self.respond(500, {"error": str(exc)})


def _request(info, route, data=None):
    req = Request(info["origin"] + route,
                  data=json.dumps(data).encode() if data is not None else None,
                  headers={"X-Conference-Token": info["token"], "Content-Type": "application/json"})
    # This is always a loopback call; never route private transcripts via a system proxy.
    with build_opener(ProxyHandler({})).open(req, timeout=15) as response:
        return json.load(response)


def ensure_dashboard(cfg):
    cfg = copy.deepcopy(cfg)
    # Shorter windows / periodic drafts only for this live mode, not global config.
    cfg.asr.max_window_ms = min(cfg.asr.max_window_ms, 8000)
    cfg.segmenter.provisional_interval_ms = min(cfg.segmenter.provisional_interval_ms, 30000)
    cfg.runner.poll_wait_ms = min(cfg.runner.poll_wait_ms, 1000)
    # Shorter audio chunks make realtime translation appear sooner (the
    # translator can only cut a segment once its audio file exists) and reduce
    # what a crash can lose.
    cfg.audio.chunk_seconds = min(cfg.audio.chunk_seconds, 3)
    cfg.translation.poll_wait_ms = min(cfg.translation.poll_wait_ms, 1000)
    # A new implementation must not silently reuse a daemon with old Python code.
    fingerprint = Path(__file__).read_bytes() + Path(__file__).with_name("service.py").read_bytes()
    digest = hashlib.sha256(json.dumps(to_plain(cfg), sort_keys=True).encode() + fingerprint).hexdigest()[:16]
    runtime = cfg.output_root / "_live"
    runtime.mkdir(parents=True, exist_ok=True)
    ready = runtime / f"{digest}.json"
    # Windows byte-range lock serializes simultaneous Agent launches across processes.
    lock_path = runtime / f"{digest}.lock"
    with lock_path.open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        try:
            try:
                info = json.loads(ready.read_text(encoding="utf-8"))
                if info.get("audio_runtime") == "desktop":
                    _request(info, "/api/info")
                    return info
            except (OSError, ValueError):
                pass
            if restricted_windows_process():
                raise PermissionError(
                    "当前 Agent 沙箱可以枚举设备，但不能打开 Windows 麦克风。"
                    "请让 Agent 使用经批准的非沙箱 exec（require_escalated）运行同一 live 命令，"
                    "或由用户在终端运行 python -m scientific_conference live。无需管理员权限；"
                    "正常环境下启动一次后，后续沙箱调用可复用该本地服务。")
            config_path = runtime / f"{digest}.yaml"
            _write_config_snapshot(cfg, config_path)
            env = dict(os.environ)
            env["PYTHONPATH"] = str(project_root() / "src") + os.pathsep + env.get("PYTHONPATH", "")
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            instance = secrets.token_hex(16)
            with (runtime / "dashboard.log").open("a", encoding="utf-8") as log:
                proc = subprocess.Popen([sys.executable, "-m", "scientific_conference.dashboard",
                    "--config", str(config_path), "--ready", str(ready), "--instance", instance], env=env,
                    cwd=str(project_root()), stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kwargs)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError("面板启动失败，请查看 meetings/_live/dashboard.log")
                try:
                    info = json.loads(ready.read_text(encoding="utf-8"))
                    if info.get("instance") == instance:
                        _request(info, "/api/info")
                        return info
                except (OSError, ValueError):
                    pass
                time.sleep(.15)
            raise TimeoutError("面板尚未就绪，请重试")
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def launch_live(cfg, *, start=False, consent=False, title="", session_id=""):
    info = ensure_dashboard(cfg)
    result = {"ok": True}
    if start:
        result = _request(info, "/api/start", {"title": title, "consent": consent})
        session_id = result["session_id"]
    elif session_id:
        _request(info, "/api/snapshot?session_id=" + session_id)
    result["dashboard_url"] = info["origin"] + "/#token=" + info["token"] + ("&session=" + session_id if session_id else "")
    result["message"] = "请在 Agent 浏览器面板打开 dashboard_url；网页每秒刷新，不需要持续轮询聊天。"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--ready", required=True)
    parser.add_argument("--instance", default="")
    args = parser.parse_args()
    if restricted_windows_process():
        raise PermissionError("麦克风面板不能从受限 Windows 进程启动；请使用经批准的桌面运行环境。")
    # Detached instances receive an already resolved configuration snapshot.
    # Reapplying SC_* overrides could restore the legacy notes runner in DSH.
    server = Dashboard(load_config(args.config, env=not bool(args.instance)))
    ready = Path(args.ready)
    temp = ready.with_suffix(".tmp")
    temp.write_text(json.dumps({"origin": server.origin, "token": server.token, "pid": os.getpid(), "instance": args.instance, "audio_runtime": "desktop"}), encoding="utf-8")
    os.replace(temp, ready)
    server.serve_forever()


if __name__ == "__main__":
    main()
