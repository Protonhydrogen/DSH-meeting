"""Model Context Protocol adapter (stdio).

The adapter is a *client of the same business layer* the CLI uses: it never
holds the only copy of the recording state, so a session started from an MCP
client can be inspected from the command line and from the runner at the same
time.

Tool names follow the project contract (``audio.list_devices``,
``conference.start``, ...).  They are this project's own tools, not MCP
built-ins.

Diagnostics go to stderr; stdout carries protocol messages only.

Run it with::

    python -m scientific_conference.mcp_server --config config/local.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:  # mcp >= 2 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer
except Exception as exc:  # pragma: no cover - depends on the installed SDK
    raise SystemExit(
        "the Model Context Protocol SDK is not usable in this environment "
        f"({type(exc).__name__}: {exc}); install 'mcp' into the external venv"
    ) from exc

from . import __version__
from .config import Config, ConfigError, load_config
from .events import EventQueue
from .service import (
    create_meeting,
    export_session,
    get_transcript,
    list_sessions,
    mark_boundary,
    open_store,
    session_status,
    start_session,
    stop_session,
)


def build_server(cfg: Config) -> MCPServer:
    server = MCPServer(
        name="scientific-conference",
        title="学术会议实时录音与科研纪要",
        version=__version__,
        instructions=(
            "用户要求开始录音纪要时，优先调用 conference.live_start，并在 Agent 的浏览器面板打开返回的 dashboard_url。"
            "该入口自动启动 recorder；external 模式需宿主持续轮询事件、生成并保存纪要，其他模式由 runner 处理。"
            "本地麦克风录音、增量转写、轮次/报告边界事件与科研纪要。"
            "典型流程：audio.list_devices -> conference.start -> 轮询 conference.events_poll "
            "-> 读取 conference.get_transcript -> 按 skills/scientific-conference/SKILL.md 生成纪要 "
            "-> conference.save_notes -> conference.events_ack -> conference.stop。"
            "事件采用至少一次投递，只有在下游结果持久化后才能 ack。"
        ),
    )

    def safe(fn, *args, **kwargs) -> dict[str, Any]:
        """Run a business-layer call, reporting failures as data not exceptions."""
        try:
            result = fn(*args, **kwargs)
            return {"ok": True, **result} if isinstance(result, dict) else {"ok": True, "result": result}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # -- devices ---------------------------------------------------------
    @server.tool(name="audio.list_devices", description="列出可用录音设备及其支持的采样率")
    def list_devices_tool(probe_sample_rates: bool = True) -> dict[str, Any]:
        from .audio import audio_backend_status, list_devices

        status = audio_backend_status()
        devices: list[dict[str, Any]] = []
        if status.get("available"):
            try:
                devices = [d.to_dict() for d in list_devices(probe_sample_rates=probe_sample_rates)]
            except Exception as exc:
                status = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": bool(status.get("available")), "backend": status, "devices": devices}

    # -- session lifecycle ------------------------------------------------
    @server.tool(name="conference.live_start", description="一步开始录音和自动纪要，返回实时双栏面板 URL；重复调用复用活动会议")
    def live_start_tool(title: str = "", consent: bool = False) -> dict[str, Any]:
        from .dashboard import launch_live
        return safe(launch_live, cfg, start=True, consent=consent, title=title)

    @server.tool(name="conference.live_view", description="打开实时转写与纪要面板，不开启麦克风；可指定已有 session_id")
    def live_view_tool(session_id: str = "") -> dict[str, Any]:
        from .dashboard import launch_live
        return safe(launch_live, cfg, session_id=session_id)

    @server.tool(name="conference.start", description="启动一次录音会话，立即返回 session_id（后台进程录音）；session_id 可指向提前创建的筹备会议")
    def start_tool(
        title: str = "",
        agenda: str = "",
        device_id: str = "default",
        language_hints: str = "",
        asr_backend: str = "",
        output_dir: str = "",
        consent: bool = False,
        session_id: str = "",
        source: str = "microphone",
        wav_path: str = "",
    ) -> dict[str, Any]:
        local = _clone(cfg)
        if output_dir:
            local.project.output_root = output_dir
        if device_id:
            local.audio.device_id = device_id
        if language_hints:
            local.asr.language_hints = [h.strip() for h in language_hints.split(",") if h.strip()]
        if asr_backend:
            local.asr.backend = asr_backend
        info = start_session(
            local,
            session_id=session_id or None,
            title=title,
            agenda=agenda,
            consent=consent,
            source=source,
            wav_path=wav_path or None,
        )
        from .service import spawn_runner
        try:
            info["runner_pid"] = spawn_runner(local, info["session_id"])
        except Exception:
            stop_session(local, info["session_id"], wait=False)
            raise
        return {"ok": True, **info}

    @server.tool(name="conference.create_meeting", description="提前创建会议（只建记录与发言人目录，不打开麦克风、不录音）")
    def create_meeting_tool(
        title: str,
        agenda: str = "",
        presenter: str = "",
        report_title: str = "",
        scheduled_for: str = "",
        location: str = "",
    ) -> dict[str, Any]:
        return safe(create_meeting, cfg, title=title, agenda=agenda,
                    presenter=presenter or "none", report_title=report_title or "none",
                    scheduled_for=scheduled_for, location=location)

    @server.tool(name="conference.status", description="查询录音、转写、纪要、积压与错误状态")
    def status_tool(session_id: str) -> dict[str, Any]:
        return safe(session_status, cfg, session_id)

    @server.tool(name="conference.stop", description="停止采集并排空转写；返回处理状态")
    def stop_tool(session_id: str, wait: bool = True, timeout_s: float = 180.0) -> dict[str, Any]:
        return safe(stop_session, cfg, session_id, timeout_s=timeout_s, wait=wait)

    @server.tool(name="conference.mark_boundary", description="人工标记 turn/talk/session 边界")
    def boundary_tool(session_id: str, kind: str = "turn") -> dict[str, Any]:
        return safe(mark_boundary, cfg, session_id, kind)

    # -- events -----------------------------------------------------------
    @server.tool(name="conference.events_poll", description="按 seq 顺序拉取事件（至少一次投递，wait_ms<=25000）")
    def poll_tool(
        session_id: str,
        consumer_id: str,
        after_seq: int = -1,
        limit: int = 50,
        wait_ms: int = 0,
    ) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            store = open_store(cfg, session_id)
            queue = EventQueue(store.db)
            return queue.poll(
                session_id,
                consumer_id,
                after_seq=None if after_seq < 0 else after_seq,
                limit=limit,
                wait_ms=wait_ms,
            )

        return safe(run)

    @server.tool(name="conference.events_ack", description="确认连续完成的事件游标（只在结果已持久化后调用）")
    def ack_tool(session_id: str, consumer_id: str, through_seq: int) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            store = open_store(cfg, session_id)
            queue = EventQueue(store.db)
            cursor = queue.ack(session_id, consumer_id, through_seq)
            return {"session_id": session_id, "consumer_id": consumer_id, "acked_seq": cursor}

        return safe(run)

    # -- transcript and notes --------------------------------------------
    @server.tool(name="conference.get_transcript", description="读取指定版本范围内的稳定转写片段")
    def transcript_tool(
        session_id: str,
        turn_id: str = "",
        talk_id: str = "",
        include_superseded: bool = False,
    ) -> dict[str, Any]:
        return safe(
            get_transcript,
            cfg,
            session_id,
            turn_id=turn_id or None,
            talk_id=talk_id or None,
            include_superseded=include_superseded,
        )

    @server.tool(name="conference.save_notes", description="保存结构化纪要（旧版本结果不会覆盖新版本）")
    def save_notes_tool(
        session_id: str,
        mode: str,
        result_json: str,
        target_id: str = "",
        source_revision: int = 1,
        skill_version: str = "",
        idempotency_key: str = "",
        provisional: bool = False,
    ) -> dict[str, Any]:
        from . import SKILL_VERSION
        from .notes import normalise_note, render_markdown, validate_note

        try:
            payload = json.loads(result_json)
        except Exception as exc:
            return {"ok": False, "errors": [f"result_json is not valid JSON: {exc}"]}
        errors = validate_note(payload)
        if errors:
            return {"ok": False, "errors": errors}
        try:
            note = normalise_note(payload, defaults={"mode": mode, "target_id": target_id or None})
            markdown = render_markdown(note, evidence={})
            store = open_store(cfg, session_id)
            key = idempotency_key or (
                f"mcp:{session_id}:{mode}:{target_id}:r{source_revision}:{skill_version or SKILL_VERSION}"
            )
            note_id, created, rejected = store.save_note(
                mode=mode,
                target_id=target_id or None,
                source_revision=source_revision,
                content_json=note,
                content_md=markdown,
                agent="mcp-client",
                config_hash="mcp",
                idempotency_key=key,
                path=None,
                skill_version=skill_version or SKILL_VERSION,
                provisional=provisional,
            )
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if rejected:
            return {"ok": False, "rejected": rejected}
        return {"ok": True, "note_id": note_id, "created": created}

    # -- export and discovery --------------------------------------------
    @server.tool(name="conference.export", description="导出转写/事件/纪要索引到会议目录")
    def export_tool(session_id: str, format: str = "all") -> dict[str, Any]:
        return safe(export_session, cfg, session_id, format)

    @server.tool(name="conference.list_sessions", description="列出历史会议会话")
    def sessions_tool(limit: int = 20) -> dict[str, Any]:
        return safe(lambda: {"output_root": str(cfg.output_root), "sessions": list_sessions(cfg, limit=limit)})

    @server.tool(name="conference.skill_info", description="返回 scientific-conference 技能路径与版本")
    def skill_tool(include_body: bool = False) -> dict[str, Any]:
        from .skill import load_skill

        doc = load_skill(cfg.skill_path)
        info = doc.info()
        if include_body:
            info["body"] = doc.body
        return {"ok": True, **info}

    return server


def _clone(cfg: Config) -> Config:
    import copy

    return copy.deepcopy(cfg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scientific-conference-mcp", description="MCP stdio 适配层")
    parser.add_argument("--config", help="YAML 配置路径")
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse", "streamable-http"])
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    server = build_server(cfg)
    print(f"scientific-conference MCP server ({__version__}) starting on {args.transport}", file=sys.stderr)
    server.run(args.transport)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
