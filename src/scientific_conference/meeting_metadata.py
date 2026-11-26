"""Editable report identity and meeting plan over existing session/meta storage."""
import re
from datetime import datetime

from .db import utc_now

SCHEDULED_KEY = "meeting:scheduled_for"
LOCATION_KEY = "meeting:location"
ORIGIN_KEY = "meeting:origin"


def validate(presenter,report_title):
    if not isinstance(presenter,str) or len(presenter)>120:raise ValueError('报告人最多 120 个字符')
    if not isinstance(report_title,str) or len(report_title)>300:raise ValueError('报告题目最多 300 个字符')
    if any(c in presenter+report_title for c in ('\n','\r','\x00')):raise ValueError('报告信息需为单行文本')
    return presenter.strip() or 'none',report_title.strip() or 'none'


def infer(original):
    text=re.sub(r'^\d{8}[-_ ]*','',original or '').strip()
    if text.startswith('OpeningRemarksby'):return text.removeprefix('OpeningRemarksby'),'none'
    first,sep,rest=text.partition(',')
    if re.match(r'Panel|Discussion|Poster|Closing|交互|实测|会议',first,re.I):return 'none',text or 'none'
    if sep:
        # Historical labels usually contain name,affiliation. Never turn the
        # affiliation into a scientific report title. Values remain editable.
        if re.search(r'Universit|Institute|College|ETH|\bCAS\b',rest,re.I):
            names=re.findall(r'(?<![A-Za-z])([A-Z][a-z]+[A-Z]{2,})(?=,|$)',text)
            names=names if len(names)>1 else [first.strip()]
            return ' / '.join(re.sub(r'(?<=[a-z])(?=[A-Z])',' ',name) for name in names),'none'
        return first.strip() or 'none',rest.strip() or 'none'
    if re.fullmatch(r"[A-Z][a-zA-Z.'’\-]+(?:\s+[A-Z][a-zA-Z.'’\-]+){1,4}",text):return text,'none'
    if re.fullmatch(r'[A-Z][a-z]+[A-Z]{2,}',text):return re.sub(r'(?<=[a-z])(?=[A-Z])',' ',text),'none'
    return 'none',text or 'none'


def validate_scheduled(value):
    """Accept an ISO-8601 local date/time; an empty plan clears the value."""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 40 or any(c in text for c in ("\n", "\r", "\x00")):
        raise ValueError("日期时间格式无效")
    try:
        datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("日期时间需为 ISO 8601，例如 2026-11-02T14:00") from None
    return text


def validate_location(value):
    text = str(value or "").strip()
    if len(text) > 200 or any(c in text for c in ("\n", "\r", "\x00")):
        raise ValueError("地点最多 200 个字符且需为单行文本")
    return text


def mark_planned(store):
    """Remember that this meeting was pre-created, so its name stays explicit."""
    with store.db.tx() as conn:
        conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (ORIGIN_KEY, "planned"))


def is_planned_origin(store):
    row = store.db.query_one("SELECT value FROM meta WHERE key=?", (ORIGIN_KEY,))
    return bool(row and row["value"] == "planned")


def read_schedule(store):
    """The plan only. It is never authoritative once recording has started."""
    values = {r["key"]: r["value"] for r in store.db.query(
        "SELECT key,value FROM meta WHERE key IN (?,?)", (SCHEDULED_KEY, LOCATION_KEY))}
    return {
        "scheduled_for": values.get(SCHEDULED_KEY, "") or "",
        "location": values.get(LOCATION_KEY, "") or "",
    }


def save_schedule(store, *, scheduled_for=None, location=None):
    """Persist the plan without ever seeding ``started_utc``."""
    current = read_schedule(store)
    next_scheduled = current["scheduled_for"] if scheduled_for is None else validate_scheduled(scheduled_for)
    next_location = current["location"] if location is None else validate_location(location)
    with store.db.tx() as conn:
        for key, value in ((SCHEDULED_KEY, next_scheduled), (LOCATION_KEY, next_location)):
            conn.execute("DELETE FROM meta WHERE key=?", (key,))
            if value:
                conn.execute("INSERT INTO meta(key,value) VALUES (?,?)", (key, value))
    return {"scheduled_for": next_scheduled, "location": next_location}


def meeting_clock(row, scheduled_for=""):
    """Authoritative meeting time.

    Pre-creating a meeting necessarily predates the conference, so
    ``created_utc`` must never be presented as the meeting time while a real
    recording exists.  ``started_utc`` (set by the recorder) wins; the
    schedule is a plan; creation time is only a fallback.
    """
    started = str(row.get("started_utc") or "").strip()
    if started:
        return started, "recording_start"
    planned = str(scheduled_for or "").strip()
    if planned:
        return planned, "scheduled"
    return str(row.get("created_utc") or ""), "created"


def read(store):
    row=store.session();title=row.get('title') or ''
    values={r['key']:r['value'] for r in store.db.query("SELECT key,value FROM meta WHERE key IN ('report:presenter','report:title','report:original_title')")}
    explicit='report:presenter' in values
    presenter,report_title=(values['report:presenter'],values.get('report:title','none')) if explicit else infer(title)
    return dict(presenter=presenter,report_title=report_title,original_title=values.get('report:original_title',title),metadata_inferred=not explicit,**read_schedule(store))


def save(store,presenter,report_title,*,display_title=True):
    """Persist reporter identity.

    ``display_title=False`` keeps the explicit meeting name (``sessions.title``)
    when a pre-created meeting also has a reporter/report title.  Legacy
    recording creation keeps the historical behaviour where the session title
    is the reporter display line.
    """
    presenter,report_title=validate(presenter,report_title)
    current=read(store)
    display=presenter if report_title=='none' else (report_title if presenter=='none' else presenter+' · '+report_title)
    with store.db.tx() as conn:
        conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES ('report:original_title',?)",(current['original_title'],))
        for key,value in [('report:presenter',presenter),('report:title',report_title)]:
            conn.execute('INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))
        if display_title:
            conn.execute('UPDATE sessions SET title=?,updated_utc=? WHERE session_id=?',(display,utc_now(),store.session_id))
        else:
            conn.execute('UPDATE sessions SET updated_utc=? WHERE session_id=?',(utc_now(),store.session_id))
    store.write_manifest()
    return read(store)
