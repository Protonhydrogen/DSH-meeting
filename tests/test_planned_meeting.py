"""Contract tests for pre-created (``planned``) meetings and pre-meeting dossiers.

Written from ``tmp/pre_meeting_contract_20261023.md``.  A planned meeting only
creates the record/database/directory: no microphone, no recorder process and
no ``config.snapshot.yaml``; later "start recording" reuses the same session id
and keeps the title, agenda, report identity, schedule, location and speaker
dossiers.  The meeting time of a session is ``started_utc`` when it was really
recorded, otherwise the planned ``scheduled_for``, otherwise ``created_utc``.

Nothing here starts a real recorder: ``service.spawn_service`` is either
monkeypatched to a fake PID or to a failing stub, and no microphone is opened.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

import pytest

from scientific_conference import cli, service
from scientific_conference.dashboard import _request, snapshot
from scientific_conference.meeting_metadata import read as read_metadata
from scientific_conference.store import STATE_PLANNED, Store, StoreError

from test_dashboard import dashboard  # noqa: F401  (shared HTTP integration fixture)

ID_RE = re.compile(r"^spk_[a-f0-9]{8}$")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def planned(cfg, **kwargs) -> dict:
    kwargs.setdefault("title", "筹备会议")
    return service.create_meeting(cfg, **kwargs)


def opened(cfg, session_id: str) -> Store:
    return Store.open(cfg.output_root, session_id)


def manifest_of(cfg, session_id: str) -> dict:
    path = cfg.output_root / session_id / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def make_recorded(cfg, session_id: str, *, started_utc: str, created_utc: str | None = None) -> Store:
    """A session that really recorded; started_utc is the authoritative time."""
    store = Store.create(
        cfg.output_root, session_id, cfg,
        title="已录音会议", state="recording", created_utc=created_utc,
    )
    with store.db.tx() as conn:
        conn.execute(
            "UPDATE sessions SET started_utc=?, state='recording' WHERE session_id=?",
            (started_utc, session_id),
        )
    store.set_pid(os.getpid())
    store.write_manifest()
    return store


def cli_config(tmp_path: Path) -> tuple[Path, Path]:
    output_root = tmp_path / "meetings"
    path = tmp_path / "cli.yaml"
    path.write_text(
        f"project:\n  output_root: '{output_root.as_posix()}'\nprivacy:\n  require_consent: false\n",
        encoding="utf-8",
    )
    return path, output_root


def call_tool(server, name: str, arguments: dict) -> dict:
    """Invoke one MCP tool the way a client would and decode its JSON answer."""
    result = asyncio.run(server.call_tool(name, arguments))
    return json.loads(result.content[0].text)


# ======================================================================
# service.create_meeting
# ======================================================================
def test_create_meeting_is_planned_and_starts_no_recording(cfg, monkeypatch):
    monkeypatch.setattr(
        service, "spawn_service", lambda *a, **k: pytest.fail("create_meeting 不得启动录音进程")
    )
    info = service.create_meeting(
        cfg,
        title="  提前创建的会议  ",
        agenda="议程第一项",
        scheduled_for="2026-11-02T14:00",
        location="Room A",
    )
    session_id = info["session_id"]
    root = cfg.output_root / session_id
    assert info["state"] == STATE_PLANNED
    assert info["title"] == "提前创建的会议"
    assert info["scheduled_for"] == "2026-11-02T14:00"
    assert info["location"] == "Room A"
    assert info["output_dir"] == str(root)

    # The record exists ...
    assert (root / "session.sqlite3").is_file()
    assert (root / "manifest.json").is_file()
    # ... but nothing was recorded.
    assert not (root / "config.snapshot.yaml").exists()
    assert not list((root / "audio").glob("*.wav"))
    assert not (root / "service.log").exists()

    store = opened(cfg, session_id)
    try:
        row = store.session()
        assert row["state"] == STATE_PLANNED
        assert row["started_utc"] is None  # 计划时间不得写进录音开始时间
        assert row["ended_utc"] is None
        assert row["pid"] is None
        assert bool(row["consent"]) is False
        assert row["title"] == "提前创建的会议"
        assert row["agenda"] == "议程第一项"
        metadata = read_metadata(store)
        assert metadata["scheduled_for"] == "2026-11-02T14:00"
        assert metadata["location"] == "Room A"
    finally:
        store.db.close()

    manifest = manifest_of(cfg, session_id)
    assert manifest["state"] == STATE_PLANNED
    assert manifest["title"] == "提前创建的会议"
    assert manifest["agenda"] == "议程第一项"
    assert manifest["scheduled_for"] == "2026-11-02T14:00"
    assert manifest["location"] == "Room A"
    assert manifest["started_utc"] is None


def test_create_meeting_keeps_the_meeting_name_next_to_report_identity(cfg):
    """会议名称独立于报告人/报告题目：sessions.title 保持会议名称。"""
    info = service.create_meeting(cfg, title="秋季报告", presenter="Dr. Li", report_title="Topic")
    session_id = info["session_id"]
    store = opened(cfg, session_id)
    try:
        assert store.session()["title"] == "秋季报告"
        metadata = read_metadata(store)
        assert metadata["presenter"] == "Dr. Li"
        assert metadata["report_title"] == "Topic"
        assert info["title"] == "秋季报告"
        assert info["presenter"] == "Dr. Li"
        assert info["report_title"] == "Topic"
    finally:
        store.db.close()
    assert snapshot(cfg, session_id)["title"] == "秋季报告"
    assert manifest_of(cfg, session_id)["title"] == "秋季报告"


@pytest.mark.parametrize("title", ["", "   ", "x" * 301])
def test_create_meeting_requires_a_title_of_1_to_300_characters(cfg, title):
    with pytest.raises(ValueError):
        service.create_meeting(cfg, title=title)
    assert not list(cfg.output_root.glob("*/session.sqlite3"))


def test_create_meeting_validates_schedule_and_location(cfg):
    with pytest.raises(ValueError):
        service.create_meeting(cfg, title="会议", scheduled_for="2026/11/02 14:00")
    with pytest.raises(ValueError):
        service.create_meeting(cfg, title="会议", location="L" * 201)
    with pytest.raises(ValueError):
        service.create_meeting(cfg, title="会议", location="Room\nA")
    assert not list(cfg.output_root.glob("*/session.sqlite3"))
    # An empty plan is valid and means "not scheduled yet".
    info = service.create_meeting(cfg, title="会议", scheduled_for="", location="")
    assert info["scheduled_for"] == "" and info["location"] == ""


def test_create_meeting_with_only_a_title_keeps_the_title(cfg):
    info = service.create_meeting(cfg, title="只有标题的会议")
    session_id = info["session_id"]
    store = opened(cfg, session_id)
    try:
        assert store.session()["title"] == "只有标题的会议"
        assert store.session_state() == STATE_PLANNED
    finally:
        store.db.close()
    assert snapshot(cfg, session_id)["title"] == "只有标题的会议"


# ======================================================================
# snapshot() of a planned meeting
# ======================================================================
def test_snapshot_of_planned_meeting_is_empty_but_carries_the_plan(cfg):
    info = planned(cfg, scheduled_for="2026-11-02T14:00", location="Room A")
    snap = snapshot(cfg, info["session_id"])
    assert snap["ok"] is True
    assert snap["state"] == STATE_PLANNED
    assert snap["title"] == "筹备会议"
    assert snap["segments"] == []
    assert snap["notes"] == []
    assert "speakers" not in snap
    assert snap["scheduled_for"] == "2026-11-02T14:00"
    assert snap["location"] == "Room A"
    # 计划时间只是“计划”，不是录音开始时间
    assert snap["meeting_time"] == "2026-11-02T14:00"
    assert snap["meeting_time_source"] == "scheduled"


def test_snapshot_meeting_time_prefers_started_utc(cfg):
    info = planned(cfg, scheduled_for="2026-11-02T14:00")
    session_id = info["session_id"]
    store = opened(cfg, session_id)
    try:
        store.set_state("recording")  # the recorder writes started_utc
        started_utc = store.session()["started_utc"]
        assert started_utc, "set_state('recording') 必须写入 started_utc"
    finally:
        store.db.close()
    snap = snapshot(cfg, session_id)
    assert snap["meeting_time"] == started_utc
    assert snap["meeting_time_source"] == "recording_start"


def test_snapshot_meeting_time_falls_back_to_created_utc(cfg):
    info = planned(cfg)
    store = opened(cfg, info["session_id"])
    try:
        created_utc = store.session()["created_utc"]
    finally:
        store.db.close()
    snap = snapshot(cfg, info["session_id"])
    assert snap["meeting_time"] == created_utc
    assert snap["meeting_time_source"] == "created"


# ======================================================================
# service.update_meeting
# ======================================================================
def test_update_meeting_title_reaches_snapshot_and_manifest(cfg):
    info = planned(cfg)
    session_id = info["session_id"]
    updated = service.update_meeting(cfg, session_id, title="改名后的会议")
    assert updated["title"] == "改名后的会议"
    assert snapshot(cfg, session_id)["title"] == "改名后的会议"
    assert manifest_of(cfg, session_id)["title"] == "改名后的会议"
    listed = {item["session_id"]: item for item in service.list_sessions(cfg)}
    assert listed[session_id]["title"] == "改名后的会议"


def test_update_meeting_report_identity(cfg):
    info = planned(cfg, title="秋季报告")
    session_id = info["session_id"]
    updated = service.update_meeting(cfg, session_id, presenter="李四", report_title="新报告题目")
    assert updated["presenter"] == "李四"
    assert updated["report_title"] == "新报告题目"
    snap = snapshot(cfg, session_id)
    assert snap["presenter"] == "李四"
    assert snap["report_title"] == "新报告题目"
    assert snap["title"] == "秋季报告"  # 改报告信息不得覆盖会议名称
    store = opened(cfg, session_id)
    try:
        assert read_metadata(store)["presenter"] == "李四"
        assert store.session()["title"] == "秋季报告"
    finally:
        store.db.close()


def test_update_meeting_schedule_location_and_agenda(cfg):
    info = planned(cfg, scheduled_for="2026-11-02T14:00", location="Room A", agenda="旧议程")
    session_id = info["session_id"]
    updated = service.update_meeting(
        cfg, session_id,
        scheduled_for="2026-11-03T09:30", location="Room B", agenda="新议程",
    )
    assert updated["scheduled_for"] == "2026-11-03T09:30"
    assert updated["location"] == "Room B"
    snap = snapshot(cfg, session_id)
    assert snap["scheduled_for"] == "2026-11-03T09:30"
    assert snap["location"] == "Room B"
    assert snap["meeting_time"] == "2026-11-03T09:30"
    assert snap["meeting_time_source"] == "scheduled"
    manifest = manifest_of(cfg, session_id)
    assert manifest["agenda"] == "新议程"
    assert manifest["scheduled_for"] == "2026-11-03T09:30"
    assert manifest["location"] == "Room B"
    listed = {item["session_id"]: item for item in service.list_sessions(cfg)}
    assert listed[session_id]["scheduled_for"] == "2026-11-03T09:30"
    assert listed[session_id]["location"] == "Room B"

    # 空串表示清空
    cleared = service.update_meeting(cfg, session_id, scheduled_for="", location="")
    assert cleared["scheduled_for"] == "" and cleared["location"] == ""
    assert snapshot(cfg, session_id)["scheduled_for"] == ""
    assert manifest_of(cfg, session_id).get("scheduled_for") in ("", None)


def test_update_meeting_requires_an_existing_session(cfg):
    with pytest.raises((ValueError, FileNotFoundError, StoreError)):
        service.update_meeting(cfg, "sess_does_not_exist", title="改名")
    assert not (cfg.output_root / "sess_does_not_exist").exists()  # 不得顺手创建会议


def test_update_meeting_rejects_remaking_an_active_recording(cfg):
    info = planned(cfg)
    session_id = info["session_id"]
    store = opened(cfg, session_id)
    try:
        store.set_state("recording")
        store.set_pid(os.getpid())
    finally:
        store.db.close()
    for change in (
        {"title": "改名"},
        {"agenda": "新议程"},
        {"scheduled_for": "2026-11-03T09:30"},
        {"location": "Room B"},
    ):
        with pytest.raises(ValueError):
            service.update_meeting(cfg, session_id, **change)
    # 报告人 / 报告题目仍可修改（与既有录音中的元数据编辑一致）
    allowed = service.update_meeting(cfg, session_id, presenter="王五", report_title="进行中改题目")
    assert allowed["presenter"] == "王五"
    assert allowed["report_title"] == "进行中改题目"


# ======================================================================
# speaker directory before recording
# ======================================================================


# ======================================================================
# meeting time ordering
# ======================================================================
def test_list_sessions_orders_by_meeting_time(cfg, monkeypatch):
    monkeypatch.setattr('scientific_conference.store.utc_now', lambda: '2026-10-23T00:00:00.000Z')
    early = planned(cfg, title="早场", scheduled_for="2026-11-02T14:00")
    late = planned(cfg, title="晚场", scheduled_for="2026-12-15T09:00")
    planless = planned(cfg, title="无计划")
    recorded = make_recorded(
        cfg, "sess_recorded_future",
        started_utc="2027-01-01T00:00:00.000Z",
        created_utc="2026-01-01T00:00:00.000Z",
    )
    try:
        order = [item["session_id"] for item in service.list_sessions(cfg, limit=20)]
    finally:
        recorded.db.close()
    expected = [recorded.session_id, late["session_id"], early["session_id"], planless["session_id"]]
    assert order[:4] == expected

    by_id = {item["session_id"]: item for item in service.list_sessions(cfg)}
    assert by_id[early["session_id"]]["state"] == STATE_PLANNED
    assert by_id[early["session_id"]]["scheduled_for"] == "2026-11-02T14:00"


# ======================================================================
# service.start_session(session_id=planned)
# ======================================================================


def test_start_session_rejects_unknown_session_id(cfg, monkeypatch):
    """契约：显式 session_id 必须是已存在的 planned 会议，否则 ValueError。"""
    monkeypatch.setattr(
        service, "spawn_service", lambda *a, **k: pytest.fail("未知会话不得启动录音")
    )
    with pytest.raises(ValueError) as err:
        service.start_session(cfg, session_id="sess_does_not_exist")
    assert "筹备" in str(err.value)
    assert not (cfg.output_root / "sess_does_not_exist").exists()


def test_start_session_requires_a_planned_session(cfg, monkeypatch):
    monkeypatch.setattr(
        service, "spawn_service", lambda *a, **k: pytest.fail("非筹备会议不得启动录音")
    )
    store = Store.create(cfg.output_root, "sess_closed", cfg, title="已结束会议")
    store.set_state("closed")
    store.db.close()
    with pytest.raises(ValueError) as err:
        service.start_session(cfg, session_id="sess_closed")
    assert "筹备" in str(err.value)


# ======================================================================
# service.archive_session(planned)
# ======================================================================
def test_archive_session_deletes_a_planned_meeting(cfg):
    info = planned(cfg)
    session_id = info["session_id"]
    result = service.archive_session(cfg, session_id)
    assert result["deleted"] is True
    assert result["recoverable"] is True
    assert not (cfg.output_root / session_id).exists()
    assert list((cfg.output_root / "_trash").glob(session_id + "--*"))


# ======================================================================
# HTTP API
# ======================================================================
def test_http_create_meeting_returns_planned_without_recording(cfg, dashboard, monkeypatch):
    _, http = dashboard
    monkeypatch.setattr(service, "spawn_service", lambda *a, **k: pytest.fail("HTTP create 不得启动录音"))
    monkeypatch.setattr(
        "scientific_conference.dashboard.start_session",
        lambda *a, **k: pytest.fail("HTTP create 不得调用 start_session"),
    )
    response = _request(http, "/api/meetings/create", {
        "title": "网页筹备会议", "agenda": "议程", "presenter": "Dr. Zhang",
        "report_title": "Some Topic", "scheduled_for": "2026-11-02T14:00", "location": "Room A",
    })
    assert response["ok"] is True
    assert response["state"] == STATE_PLANNED
    session_id = response["session_id"]
    assert session_id
    assert response["title"] == "网页筹备会议"
    assert response["scheduled_for"] == "2026-11-02T14:00"
    assert response["location"] == "Room A"
    assert response["output_dir"] == str(cfg.output_root / session_id)
    assert (cfg.output_root / session_id / "session.sqlite3").is_file()
    assert not (cfg.output_root / session_id / "config.snapshot.yaml").exists()
    snap = _request(http, "/api/snapshot?session_id=" + session_id)
    assert snap["title"] == "网页筹备会议"
    assert snap["presenter"] == "Dr. Zhang"
    assert snap["report_title"] == "Some Topic"

    info = _request(http, "/api/info")
    item = next(s for s in info["sessions"] if s["session_id"] == session_id)
    assert item["state"] == STATE_PLANNED
    assert item["scheduled_for"] == "2026-11-02T14:00"
    assert "speaker_count" not in item
    assert item["meeting_time"] == "2026-11-02T14:00"
    assert item["meeting_time_source"] == "scheduled"


def test_http_create_meeting_rejects_invalid_input(cfg, dashboard):
    _, http = dashboard
    for body in ({"title": ""}, {"title": "x" * 301}, {"title": "会议", "scheduled_for": "not-a-date"}):
        with pytest.raises(HTTPError) as err:
            _request(http, "/api/meetings/create", body)
        assert err.value.code == 400
    assert not list(cfg.output_root.glob("*/session.sqlite3"))


def test_http_update_meeting(cfg, dashboard):
    _, http = dashboard
    created = _request(http, "/api/meetings/create", {"title": "网页筹备会议"})
    session_id = created["session_id"]

    renamed = _request(http, "/api/meetings/update", {"session_id": session_id, "title": "网页改名"})
    assert renamed["ok"] is True
    assert renamed["session_id"] == session_id
    assert renamed["title"] == "网页改名"

    metadata = _request(http, "/api/meetings/update", {
        "session_id": session_id, "presenter": "李四", "report_title": "新报告题目",
    })
    assert metadata["presenter"] == "李四"
    assert metadata["report_title"] == "新报告题目"
    assert metadata["title"] == "网页改名"  # 会议名称不被报告信息覆盖

    schedule = _request(http, "/api/meetings/update", {
        "session_id": session_id, "scheduled_for": "2026-11-03T09:30",
        "location": "Room B", "agenda": "新议程",
    })
    assert schedule["scheduled_for"] == "2026-11-03T09:30"
    assert schedule["location"] == "Room B"

    snap = _request(http, "/api/snapshot?session_id=" + session_id)
    assert snap["scheduled_for"] == "2026-11-03T09:30"
    assert snap["location"] == "Room B"
    assert snap["presenter"] == "李四"
    assert snap["meeting_time"] == "2026-11-03T09:30"
    assert snap["meeting_time_source"] == "scheduled"
    manifest = manifest_of(cfg, session_id)
    assert manifest["scheduled_for"] == "2026-11-03T09:30"
    assert manifest["location"] == "Room B"
    assert manifest["agenda"] == "新议程"


def test_http_update_meeting_rejects_unknown_session(cfg, dashboard):
    _, http = dashboard
    with pytest.raises(HTTPError) as err:
        _request(http, "/api/meetings/update", {"session_id": "sess_missing", "title": "x"})
    assert err.value.code == 400


def test_http_meeting_metadata_keeps_the_planned_meeting_name(cfg, dashboard):
    """筹备会议的 /api/meeting/metadata 只改报告信息，不改会议名称。"""
    _, http = dashboard
    created = _request(http, "/api/meetings/create", {"title": "秋季报告"})
    session_id = created["session_id"]
    result = _request(http, "/api/meeting/metadata", {
        "session_id": session_id, "presenter": "王五", "report_title": "新报告题目",
    })
    assert result["ok"] is True
    assert result["presenter"] == "王五"
    assert result["report_title"] == "新报告题目"
    snap = _request(http, "/api/snapshot?session_id=" + session_id)
    assert snap["title"] == "秋季报告"
    assert snap["presenter"] == "王五"


def test_http_start_legacy_path_uses_the_report_display_title(cfg, dashboard, monkeypatch):
    """未带 session_id 的旧路径（直接新建录音）保持 title=报告显示名。"""
    _, http = dashboard
    monkeypatch.setattr(service, "spawn_service", lambda *a, **k: 434343)
    monkeypatch.setattr("scientific_conference.dashboard.spawn_runner", lambda *a, **k: 0)
    result = _request(http, "/api/start", {
        "title": "组会", "presenter": "Dr. Li", "report_title": "Topic", "consent": True,
    })
    assert result["session_id"]
    assert not result.get("reused")
    store = opened(cfg, result["session_id"])
    try:
        assert store.session()["title"] == "Dr. Li · Topic"
        metadata = read_metadata(store)
        assert metadata["presenter"] == "Dr. Li"
        assert metadata["report_title"] == "Topic"
    finally:
        store.db.close()


def test_http_update_meeting_rejects_active_recording_changes(cfg, dashboard):
    _, http = dashboard
    created = _request(http, "/api/meetings/create", {"title": "进行中的会议"})
    session_id = created["session_id"]
    store = opened(cfg, session_id)
    try:
        store.set_state("recording")
        store.set_pid(os.getpid())
    finally:
        store.db.close()
    for change in (
        {"title": "改名"},
        {"agenda": "新议程"},
        {"scheduled_for": "2026-11-03T09:30"},
        {"location": "Room B"},
    ):
        with pytest.raises(HTTPError) as err:
            _request(http, "/api/meetings/update", {"session_id": session_id, **change})
        assert err.value.code == 400, change
    allowed = _request(http, "/api/meetings/update", {
        "session_id": session_id, "presenter": "王五", "report_title": "进行中改题目",
    })
    assert allowed["ok"] is True












def test_http_start_reuses_the_planned_meeting(cfg, dashboard, monkeypatch):
    _, http = dashboard
    created = _request(http, "/api/meetings/create", {
        "title": "待开始会议", "scheduled_for": "2026-11-02T14:00", "location": "Room A",
        "agenda": "议程第一项",
    })
    session_id = created["session_id"]
    monkeypatch.setattr(service, "spawn_service", lambda *a, **k: 424242)
    monkeypatch.setattr("scientific_conference.dashboard.spawn_runner", lambda *a, **k: 0)
    result = _request(http, "/api/start", {"session_id": session_id, "consent": True})
    assert result["session_id"] == session_id
    assert result["state"] in ("starting", "recording")
    assert not result.get("reused")
    root = cfg.output_root / session_id
    assert (root / "config.snapshot.yaml").is_file()
    store = opened(cfg, session_id)
    try:
        row = store.session()
        assert row["pid"] == 424242
        assert row["title"] == "待开始会议"
        assert row["agenda"] == "议程第一项"
        metadata = read_metadata(store)
        assert metadata["scheduled_for"] == "2026-11-02T14:00"
        assert metadata["location"] == "Room A"
    finally:
        store.db.close()


def test_http_start_with_session_id_refuses_a_second_recording(cfg, dashboard, monkeypatch):
    _, http = dashboard
    created = _request(http, "/api/meetings/create", {"title": "待开始会议"})
    session_id = created["session_id"]
    active = Store.create(cfg.output_root, "sess_already_recording", cfg, title="正在录音")
    active.set_state("recording")
    active.set_pid(os.getpid())
    active.db.close()
    monkeypatch.setattr(
        service, "spawn_service", lambda *a, **k: pytest.fail("已有活动录音时不得再启动")
    )
    with pytest.raises(HTTPError) as err:
        _request(http, "/api/start", {"session_id": session_id, "consent": True})
    assert err.value.code == 400




def test_http_info_orders_sessions_by_meeting_time(cfg, dashboard, monkeypatch):
    monkeypatch.setattr('scientific_conference.store.utc_now', lambda: '2026-10-23T00:00:00.000Z')
    _, http = dashboard
    early = service.create_meeting(cfg, title="早场", scheduled_for="2026-11-02T14:00")
    late = service.create_meeting(cfg, title="晚场", scheduled_for="2026-12-15T09:00")
    planless = service.create_meeting(cfg, title="无计划")
    recorded = make_recorded(
        cfg, "sess_recorded_future",
        started_utc="2027-01-01T00:00:00.000Z",
        created_utc="2026-01-01T00:00:00.000Z",
    )
    recorded.db.close()
    recorded_mid = make_recorded(
        cfg, "sess_recorded_mid",
        started_utc="2026-12-20T08:00:00.000Z",
        created_utc="2027-06-01T00:00:00.000Z",
    )
    recorded_mid.db.close()

    items = _request(http, "/api/info")["sessions"]
    order = [item["session_id"] for item in items]
    # 按会议时间降序：两场录音按 started_utc 交错，计划会议按 scheduled_for。
    expected = [
        recorded.session_id, recorded_mid.session_id,
        late["session_id"], early["session_id"], planless["session_id"],
    ]
    assert order[:5] == expected

    by_id = {item["session_id"]: item for item in items}
    assert by_id[recorded.session_id]["meeting_time"] == "2027-01-01T00:00:00.000Z"
    assert by_id[recorded.session_id]["meeting_time_source"] == "recording_start"
    assert by_id[recorded_mid.session_id]["meeting_time"] == "2026-12-20T08:00:00.000Z"
    assert by_id[recorded_mid.session_id]["meeting_time_source"] == "recording_start"
    assert by_id[early["session_id"]]["meeting_time"] == "2026-11-02T14:00"
    assert by_id[early["session_id"]]["meeting_time_source"] == "scheduled"
    assert by_id[planless["session_id"]]["meeting_time_source"] == "created"
    assert "speaker_count" not in by_id[early["session_id"]]


# ======================================================================
# CLI
# ======================================================================
def test_cli_new_creates_a_planned_meeting_without_recording(capsys, tmp_path, monkeypatch):
    config, output_root = cli_config(tmp_path)
    monkeypatch.setattr(
        service, "spawn_service", lambda *a, **k: pytest.fail("conference new 不得启动录音")
    )
    code = cli.main([
        "--config", str(config), "--json", "new",
        "--title", "命令行筹备会议", "--agenda", "议程",
        "--presenter", "Dr. Li", "--report-title", "Some Topic",
        "--scheduled-for", "2026-11-02T14:00", "--location", "Room A",
    ])
    out = capsys.readouterr().out
    assert code == 0, out
    data = json.loads(out)
    assert data["state"] == STATE_PLANNED
    root = output_root / data["session_id"]
    assert (root / "session.sqlite3").is_file()
    assert not (root / "config.snapshot.yaml").exists()
    store = Store.open(output_root, data["session_id"])
    try:
        assert data["title"] == "命令行筹备会议"
        assert store.session()["title"] == "命令行筹备会议"
        assert read_metadata(store)["presenter"] == "Dr. Li"
        assert read_metadata(store)["report_title"] == "Some Topic"
        assert read_metadata(store)["scheduled_for"] == "2026-11-02T14:00"
        assert read_metadata(store)["location"] == "Room A"
    finally:
        store.db.close()


def test_cli_new_help_states_it_does_not_record():
    parser = cli.build_parser()
    subcommands = next(
        action for action in parser._actions
        if getattr(action, "choices", None) and "new" in action.choices
    )
    text = parser.format_help() + subcommands.choices["new"].format_help()
    assert "new" in text
    # 帮助里必须能看出“只创建会议记录、不开始录音”
    assert "记录" in text
    assert "不" in text
    assert "录音" in text or "麦克风" in text


def test_cli_start_with_session_id_reuses_the_planned_meeting(capsys, tmp_path, monkeypatch):
    config, output_root = cli_config(tmp_path)
    assert cli.main([
        "--config", str(config), "--json", "new", "--title", "命令行筹备会议",
        "--scheduled-for", "2026-11-02T14:00",
    ]) == 0
    created = json.loads(capsys.readouterr().out)
    session_id = created["session_id"]

    monkeypatch.setattr(service, "spawn_service", lambda *a, **k: 424242)
    monkeypatch.setattr(cli, "wait_until_ready", lambda *a, **k: "recording")
    code = cli.main([
        "--config", str(config), "--json", "start",
        "--session-id", session_id, "--consent", "--runner", "none",
    ])
    out = capsys.readouterr().out
    assert code == 0, out
    started = json.loads(out)
    assert started["session_id"] == session_id
    assert started["state"] == "recording"
    root = output_root / session_id
    assert (root / "config.snapshot.yaml").is_file()
    store = Store.open(output_root, session_id)
    try:
        row = store.session()
        assert row["pid"] == 424242
        assert row["title"] == "命令行筹备会议"
        assert read_metadata(store)["scheduled_for"] == "2026-11-02T14:00"
    finally:
        store.db.close()


# ======================================================================
# MCP
# ======================================================================




def test_mcp_start_reuses_a_planned_session(cfg, monkeypatch):
    from scientific_conference.mcp_server import build_server

    server = build_server(cfg)
    created = call_tool(server, "conference.create_meeting", {"title": "MCP 待开始会议"})
    session_id = created["session_id"]
    monkeypatch.setattr(service, "spawn_service", lambda *a, **k: 424242)
    monkeypatch.setattr(service, "spawn_runner", lambda *a, **k: 0)
    started = call_tool(server, "conference.start", {"session_id": session_id, "consent": True})
    assert started["ok"] is True
    assert started["session_id"] == session_id
    store = opened(cfg, session_id)
    try:
        assert store.session()["title"] == "MCP 待开始会议"
        assert store.session()["pid"] == 424242
    finally:
        store.db.close()
