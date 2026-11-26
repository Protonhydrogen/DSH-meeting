"""Note parsing, schema validation and Markdown rendering."""

from __future__ import annotations

import pytest

from scientific_conference.notes import (
    NoteError,
    extract_json,
    normalise_note,
    note_filename,
    render_markdown,
    validate_note,
)


def test_extract_json_variants():
    payload = '{"summary": "s", "findings": []}'
    assert extract_json(payload)["summary"] == "s"
    assert extract_json(f"```json\n{payload}\n```")["summary"] == "s"
    assert extract_json(f"说明如下：\n{payload}\n以上。")["summary"] == "s"


def test_extract_json_failure():
    with pytest.raises(NoteError):
        extract_json("")
    with pytest.raises(NoteError):
        extract_json("没有 JSON 的普通文本")


def test_validate_requires_summary_and_findings():
    errors = validate_note({"findings": []})
    assert any("summary" in e for e in errors)
    assert validate_note({"summary": "s", "findings": []}) == []


def test_validate_claim_types_and_shapes():
    base = {"summary": "s", "findings": [{"claim": "c", "claim_type": "made_up"}]}
    assert any("claim_type" in e for e in validate_note(base))

    missing_claim = {"summary": "s", "findings": [{"claim": "  "}]}
    assert any("claim" in e for e in validate_note(missing_claim))

    bad_container = {"summary": "s", "findings": [], "qa": "not-a-list"}
    assert any("qa" in e for e in validate_note(bad_container))


def test_normalise_fills_defaults():
    note = normalise_note({"summary": "s", "findings": [{"claim": "c"}]})
    assert note["schema_version"] == "1.0"
    assert note["findings"][0]["claim_type"] == "reported_claim"
    assert note["findings"][0]["source_segment_ids"] == []
    assert note["no_scientific_content"] is False
    assert note["qa"] == []


def test_render_markdown_contains_evidence_and_sections():
    note = normalise_note(
        {
            "summary": "菌群与炎症相关",
            "mode": "turn",
            "heading": "turn_001",
            "source_revision": 2,
            "findings": [
                {
                    "claim": "多样性下降与 CRP 升高相关",
                    "value": "12%",
                    "source_segment_ids": ["seg_1"],
                    "claim_type": "data_supported",
                    "uncertainty": "未给置信区间",
                }
            ],
            "limitations": [{"text": "样本以东亚人群为主", "source_segment_ids": ["seg_2"]}],
            "qa": [{"asker": "speaker_02", "question": "样本量够吗？", "answerer": "speaker_01",
                    "answer": "功效分析支持两个亚组", "adequate": None}],
            "action_items": [{"who": "speaker_01", "what": "独立队列验证", "when": None, "explicit": True}],
            "terminology": [{"original": "十六S", "suggested": "16S", "reason": "转写误写",
                             "source_segment_ids": ["seg_1"]}],
        },
        defaults={"agent": "test", "skill_version": "1.0.0"},
    )
    md = render_markdown(note, evidence={"seg_1": {"start_ms": 0, "end_ms": 1500},
                                         "seg_2": {"start_ms": 2000, "end_ms": 3000}})
    assert "## 核心摘要" in md
    assert "## 主要结果与证据" in md
    assert "多样性下降与 CRP 升高相关" in md
    assert "seg_1 (00:00.0–00:01.5)" in md
    assert "未给置信区间" in md
    assert "样本以东亚人群为主" in md
    assert "问（speaker_02）" in md
    assert "独立队列验证" in md
    assert "十六S" in md
    assert "revision 2" in md


def test_render_markdown_handles_empty_content():
    note = normalise_note({"summary": "无科研内容", "no_scientific_content": True, "findings": []})
    md = render_markdown(note)
    assert "no_scientific_content" in md
    assert "未提及" in md


def test_note_filename():
    assert note_filename("turn", "turn_007", 3) == "turn_turn_007_r3.md"
    assert note_filename("session", None, 1) == "session_session_r1.md"
