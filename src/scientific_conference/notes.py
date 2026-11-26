"""Scientific note schema, validation and Markdown rendering.

The model (or the offline extractor) returns JSON; the host - not the model -
owns schema validation and rendering, as the design document requires.  A note
that fails validation is rejected and reported, never written half-parsed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from . import SCHEMA_VERSION

__all__ = [
    "NOTE_JSON_SCHEMA",
    "validate_note",
    "normalise_note",
    "render_markdown",
    "extract_json",
    "NoteError",
]

CLAIM_TYPES = {
    "reported_claim",  # what the speaker said
    "data_supported",  # observation backed by presented data
    "causal_interpretation",
    "hypothesis",
    "organizer_note",  # added by the note taker, not said in the room
}

MODE_TITLES = {
    "turn": "发言纪要",
    "talk": "报告纪要",
    "session": "会议总览",
}

NOTE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "findings"],
    "properties": {
        "summary": {"type": "string"},
        "no_scientific_content": {"type": "boolean"},
        "research_question": {"type": "object"},
        "methods": {"type": "object"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["claim"],
                "properties": {
                    "claim": {"type": "string"},
                    "value": {"type": ["string", "null"]},
                    "evidence": {"type": ["string", "null"]},
                    "source_segment_ids": {"type": "array", "items": {"type": "string"}},
                    "claim_type": {"type": "string", "enum": sorted(CLAIM_TYPES)},
                    "uncertainty": {"type": ["string", "null"]},
                },
            },
        },
        "novelty": {"type": "array"},
        "limitations": {"type": "array"},
        "reproducibility": {"type": "object"},
        "qa": {"type": "array"},
        "action_items": {"type": "array"},
        "uncertainties": {"type": "array"},
        "terminology": {"type": "array"},
        "evidence_index": {"type": "array"},
    },
}


class NoteError(ValueError):
    """Raised when a note payload cannot be accepted."""


# --------------------------------------------------------------------------
# parsing / validation
# --------------------------------------------------------------------------
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model response.

    Accepts a fenced ```json block, a bare object, or an object embedded in
    prose.  Raises :class:`NoteError` when nothing parses.
    """
    if not text or not text.strip():
        raise NoteError("empty agent response")
    candidates: list[str] = []
    for match in _FENCE.finditer(text):
        candidates.append(match.group(1).strip())
    candidates.append(text.strip())
    # last resort: the outermost {...}
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    raise NoteError("no JSON object found in the agent response")


def _type_ok(value: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_type_ok(value, e) for e in expected)
    return {
        "string": lambda v: isinstance(v, str),
        "boolean": lambda v: isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "null": lambda v: v is None,
    }.get(expected, lambda v: True)(value)


def validate_note(data: dict[str, Any]) -> list[str]:
    """Return a list of human-readable schema errors (empty == valid)."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["note payload must be an object"]
    for key in NOTE_JSON_SCHEMA["required"]:
        if key not in data:
            errors.append(f"missing required key: {key}")
    if "summary" in data and not isinstance(data["summary"], str):
        errors.append("summary must be a string")
    if "findings" in data:
        if not isinstance(data["findings"], list):
            errors.append("findings must be an array")
        else:
            for i, item in enumerate(data["findings"]):
                if not isinstance(item, dict):
                    errors.append(f"findings[{i}] must be an object")
                    continue
                if not str(item.get("claim", "")).strip():
                    errors.append(f"findings[{i}].claim is required")
                ctype = item.get("claim_type")
                if ctype is not None and ctype not in CLAIM_TYPES:
                    errors.append(f"findings[{i}].claim_type={ctype!r} is not one of {sorted(CLAIM_TYPES)}")
                ids = item.get("source_segment_ids")
                if ids is not None and not isinstance(ids, list):
                    errors.append(f"findings[{i}].source_segment_ids must be an array")
    for key in ("novelty", "limitations", "qa", "action_items", "uncertainties", "terminology", "evidence_index"):
        if key in data and not isinstance(data[key], list):
            errors.append(f"{key} must be an array")
    for key in ("research_question", "methods", "reproducibility"):
        if key in data and not isinstance(data[key], dict):
            errors.append(f"{key} must be an object")
    return errors


def normalise_note(data: dict[str, Any], defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fill in the envelope and the optional containers."""
    out: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "summary": "",
        "no_scientific_content": False,
        "research_question": {},
        "methods": {},
        "findings": [],
        "novelty": [],
        "limitations": [],
        "reproducibility": {},
        "qa": [],
        "action_items": [],
        "uncertainties": [],
        "terminology": [],
        "evidence_index": [],
    }
    out.update(defaults or {})
    out.update({k: v for k, v in (data or {}).items() if v is not None})

    findings = []
    for item in out.get("findings") or []:
        if not isinstance(item, dict):
            continue
        item.setdefault("claim_type", "reported_claim")
        item.setdefault("source_segment_ids", [])
        item.setdefault("value", None)
        item.setdefault("uncertainty", None)
        item.setdefault("evidence", None)
        findings.append(item)
    out["findings"] = findings
    return out


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def _fmt_range(start_ms: int | None, end_ms: int | None) -> str:
    if start_ms is None:
        return "-"
    return f"{_fmt_ms(start_ms)}–{_fmt_ms(end_ms if end_ms is not None else start_ms)}"


def _fmt_ms(ms: int) -> str:
    ms = int(ms)
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}.{milli // 100:01d}"
    return f"{m:02d}:{s:02d}.{milli // 100:01d}"


def _evidence(table: dict[str, dict[str, Any]], ids: Iterable[str] | None) -> str:
    parts = []
    for sid in ids or []:
        row = table.get(sid)
        if row is None:
            parts.append(sid)
        else:
            parts.append(f"{sid} ({_fmt_range(row.get('start_ms'), row.get('end_ms'))})")
    return "; ".join(parts) if parts else "未关联"


def render_markdown(note: dict[str, Any], *, evidence: dict[str, dict[str, Any]] | None = None) -> str:
    """Render a validated note as Markdown following the project template."""
    if note.get('language') == 'en':
        return render_english(note, evidence or {})
    evidence = evidence or {}
    mode = note.get("mode", "turn")
    title = note.get("title") or MODE_TITLES.get(mode, "纪要")
    lines: list[str] = []
    lines.append(f"# {title}：{note.get('heading') or '未提供'}")
    lines.append("")
    lines.append("## 基本信息")
    lines.append(f"- 模式：{mode}")
    lines.append(f"- 报告人/发言人：{note.get('speaker_display') or note.get('speaker_id') or 'unknown'}")
    lines.append(f"- 时间范围：{_fmt_range(note.get('start_ms'), note.get('end_ms'))}")
    lines.append(f"- 来源版本：revision {note.get('source_revision', 1)}")
    lines.append(f"- 生成方式：{note.get('agent', 'unknown')} / skill {note.get('skill_version', '-')}")
    if note.get("no_scientific_content"):
        lines.append("- 标记：no_scientific_content（未发现科研内容）")
    lines.append("")

    lines.append("## 核心摘要")
    lines.append(note.get("summary") or "未提及")
    lines.append("")

    rq = note.get("research_question") or {}
    methods = note.get("methods") or {}
    lines.append("## 研究问题与方法")
    if rq or methods:
        if rq.get("question"):
            lines.append(f"- 研究问题：{rq['question']}")
        if rq.get("background_gap"):
            lines.append(f"- 背景缺口：{rq['background_gap']}")
        if rq.get("hypothesis"):
            lines.append(f"- 假设：{rq['hypothesis']}")
        if methods.get("design"):
            lines.append(f"- 研究设计：{methods['design']}")
        if methods.get("subjects"):
            lines.append(f"- 对象/材料/数据集：{methods['subjects']}")
        if methods.get("sample_size"):
            lines.append(f"- 样本规模：{methods['sample_size']}")
        if methods.get("controls"):
            lines.append(f"- 对照：{methods['controls']}")
        if methods.get("analysis"):
            lines.append(f"- 分析方法：{methods['analysis']}")
        if methods.get("metrics"):
            lines.append(f"- 评估指标：{methods['metrics']}")
    else:
        lines.append("未提及")
    lines.append("")

    lines.append("## 主要结果与证据")
    findings = note.get("findings") or []
    if findings:
        lines.append("| 结论 | 数值或限定条件 | 来源片段与时间 | 类型 | 不确定性 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for f in findings:
            claim = str(f.get("claim", "")).replace("|", "\\|")
            value = str(f.get("value") or "-").replace("|", "\\|")
            src = _evidence(evidence, f.get("source_segment_ids"))
            lines.append(
                f"| {claim} | {value} | {src} | {f.get('claim_type', 'reported_claim')} |"
                f" {f.get('uncertainty') or '-'} |"
            )
    else:
        lines.append("未提及")
    lines.append("")

    lines.append("## 创新、局限与待核实事项")
    novelty = note.get("novelty") or []
    limitations = note.get("limitations") or []
    uncertainties = note.get("uncertainties") or []
    repo = note.get("reproducibility") or {}
    if novelty:
        lines.append("**报告人主张的创新**")
        for item in novelty:
            lines.append(f"- {_item_text(item)}")
    if limitations:
        lines.append("")
        lines.append("**局限**")
        for item in limitations:
            lines.append(f"- {_item_text(item)}")
    if uncertainties:
        lines.append("")
        lines.append("**待核实事项（整理者观察）**")
        for item in uncertainties:
            lines.append(f"- {_item_text(item)}")
    if repo:
        lines.append("")
        lines.append("**可复现信息**")
        for key, label in (
            ("data", "数据"),
            ("code", "代码"),
            ("protocol", "协议/参数"),
            ("materials", "材料"),
            ("availability", "获取方式"),
        ):
            if repo.get(key):
                lines.append(f"- {label}：{repo[key]}")
    if not (novelty or limitations or uncertainties or repo):
        lines.append("未提及")
    lines.append("")

    lines.append("## 问答")
    qa = note.get("qa") or []
    if qa:
        for item in qa:
            if not isinstance(item, dict):
                lines.append(f"- {item}")
                continue
            asker = item.get("asker") or "unknown"
            answerer = item.get("answerer") or "unknown"
            lines.append(f"- **问（{asker}）**：{item.get('question') or '未提及'}")
            lines.append(f"  - **答（{answerer}）**：{item.get('answer') or '未提及'}")
            adequacy = item.get("adequate")
            adequacy_text = {True: "回答充分", False: "回答不充分"}.get(adequacy, "是否充分：未评估")
            lines.append(f"  - {adequacy_text}")
            if item.get("open_issues"):
                lines.append(f"  - 未解决：{item['open_issues']}")
    else:
        lines.append("未提及")
    lines.append("")

    lines.append("## 后续事项")
    actions = note.get("action_items") or []
    if actions:
        for item in actions:
            if isinstance(item, dict):
                who = item.get("who") or "未指定"
                what = item.get("what") or "未提及"
                when = item.get("when") or "未指定"
                tag = "" if item.get("explicit", True) else "（整理者建议）"
                lines.append(f"- {who}：{what}（时间：{when}）{tag}")
            else:
                lines.append(f"- {item}")
    else:
        lines.append("未提及")
    lines.append("")

    lines.append("## 术语与转写疑点")
    terms = note.get("terminology") or []
    if terms:
        for item in terms:
            if isinstance(item, dict):
                lines.append(
                    f"- 原文「{item.get('original', '')}」→ 建议「{item.get('suggested', '')}」"
                    f"；理由：{item.get('reason') or '未说明'}；位置：{_evidence(evidence, item.get('source_segment_ids'))}"
                )
            else:
                lines.append(f"- {item}")
    else:
        lines.append("未提及")
    lines.append("")

    lines.append("## 证据索引")
    index = note.get("evidence_index") or []
    if index:
        for item in index:
            if isinstance(item, dict):
                lines.append(
                    f"- {item.get('segment_id', '?')}：{_fmt_range(item.get('start_ms'), item.get('end_ms'))}"
                    f" — {item.get('note') or ''}"
                )
            else:
                lines.append(f"- {item}")
    else:
        lines.append("未提及")
    lines.append("")
    return "\n".join(lines)


def _item_text(item: Any) -> str:
    if isinstance(item, dict):
        text = item.get("text") or item.get("claim") or item.get("description") or json.dumps(item, ensure_ascii=False)
        ids = item.get("source_segment_ids") or []
        if ids:
            return f"{text}（证据：{', '.join(str(i) for i in ids)}）"
        return str(text)
    return str(item)


def note_filename(mode: str, target_id: str | None, revision: int) -> str:
    target = target_id or "session"
    return f"{mode}_{target}_r{revision}.md"


@dataclass
class NoteBundle:
    json_payload: dict[str, Any]
    markdown: str
    filename: str


def render_english(note, evidence):
    """English narrative; omit empty template sections."""
    lines=['# '+str(note.get('speaker_display') or note.get('heading') or 'Scientific notes'), '',
           ('Provisional' if note.get('provisional') else 'Summary')+' · '+_fmt_range(note.get('start_ms'),note.get('end_ms')), '',
           note.get('summary') or 'No scientific content.', '']
    if note.get('findings'):
        lines += ['## Findings','']
        for finding in note['findings']:
            lines.append('- '+str(finding.get('claim','')))
            refs=[]
            for sid in finding.get('source_segment_ids',[]):
                row=evidence.get(sid)
                refs.append(sid+(' ('+_fmt_range(row['start_ms'],row['end_ms'])+')' if row else ''))
            if refs: lines.append('  Evidence: '+ '; '.join(refs))
            if finding.get('uncertainty'): lines.append('  Uncertainty: '+str(finding['uncertainty']))
        lines.append('')
    for key,title in [('methods','Methods'),('limitations','Limitations'),('uncertainties','Uncertain terms and claims'),('qa','Questions and answers'),('action_items','Follow-up')]:
        value=note.get(key)
        if not value: continue
        lines += ['## '+title,'']
        if isinstance(value,dict):
            lines += ['- '+str(k)+': '+(v if isinstance(v,str) else json.dumps(v,ensure_ascii=False)) for k,v in value.items() if v]
        elif isinstance(value,list):
            lines += ['- '+(v if isinstance(v,str) else json.dumps(v,ensure_ascii=False)) for v in value]
        else: lines.append(str(value))
        lines.append('')
    return '\n'.join(lines)
