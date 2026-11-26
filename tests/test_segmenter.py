"""The turn/talk boundary state machine.

Every test drives an exact timeline, so the documented rules are pinned down:
short pauses never split, a long pause closes a turn after the grace window,
a stable speaker change splits immediately, short interjections stay inside the
turn, and long silence (or a manual boundary) closes the talk.
"""

from __future__ import annotations

from scientific_conference.config import load_config
from scientific_conference.segmenter import Segmenter


def make_segmenter(**overrides) -> Segmenter:
    cfg = load_config(env=False)
    cfg.segmenter.turn_silence_ms = overrides.get("turn_silence_ms", 4000)
    cfg.segmenter.grace_ms = overrides.get("grace_ms", 2000)
    cfg.segmenter.short_backchannel_ms = overrides.get("short_backchannel_ms", 900)
    cfg.segmenter.talk_close_silence_ms = overrides.get("talk_close_silence_ms", 20000)
    cfg.segmenter.provisional_interval_ms = overrides.get("provisional_interval_ms", 90000)
    cfg.segmenter.auto_talk_from_speaker_change = overrides.get("auto_talk_from_speaker_change", False)
    return Segmenter(cfg.segmenter)


def kinds(actions):
    return [a.kind for a in actions]


def test_first_segment_opens_talk_and_turn():
    seg = make_segmenter()
    placement = seg.add_segment(start_ms=1000, end_ms=3000, speaker_id="speaker_01")
    assert kinds(placement.actions) == ["open_talk", "open_turn"]
    assert placement.turn_id == "turn_001"
    assert placement.talk_id == "talk_001"
    assert seg.current_turn is not None and seg.current_turn.end_ms == 3000


def test_short_pause_does_not_split():
    seg = make_segmenter()
    seg.add_segment(start_ms=0, end_ms=2000, speaker_id="speaker_01")
    placement = seg.add_segment(start_ms=2800, end_ms=4000, speaker_id="speaker_01")
    assert placement.actions == []
    assert seg.current_turn.turn_id == "turn_001"
    assert seg.current_turn.end_ms == 4000


def test_long_pause_splits_only_after_grace():
    seg = make_segmenter()
    seg.add_segment(start_ms=0, end_ms=2000, speaker_id="speaker_01")

    # still inside the grace window: no action
    assert seg.tick(7000) == []
    # past silence + grace: turn finalised, talk stays open
    actions = seg.tick(9000)
    assert kinds(actions) == ["finalize_turn"]
    assert actions[0].end_ms == 2000
    assert actions[0].reason == "silence"

    # new speech starts a fresh turn in the same talk
    placement = seg.add_segment(start_ms=9500, end_ms=11000, speaker_id="speaker_01")
    assert kinds(placement.actions) == ["open_turn"]
    assert placement.talk_id == "talk_001"
    assert seg.current_turn.turn_id == "turn_002"


def test_resumption_within_grace_merges():
    seg = make_segmenter()
    seg.add_segment(start_ms=0, end_ms=2000, speaker_id="speaker_01")
    placement = seg.add_segment(start_ms=5500, end_ms=7000, speaker_id="speaker_01")
    # 3500 ms gap: longer than turn_silence but inside silence + grace
    assert placement.actions == []
    assert seg.current_turn.turn_id == "turn_001"
    assert seg.current_turn.end_ms == 7000


def test_speaker_change_splits_immediately():
    seg = make_segmenter()
    seg.add_segment(start_ms=0, end_ms=4000, speaker_id="speaker_01")
    placement = seg.add_segment(start_ms=4300, end_ms=8000, speaker_id="speaker_02")
    assert kinds(placement.actions) == ["finalize_turn", "open_turn"]
    assert placement.turn_id == "turn_002"
    assert placement.talk_id == "talk_001"  # same talk: Q&A stays attached


def test_short_backchannel_does_not_split():
    seg = make_segmenter()
    seg.add_segment(start_ms=0, end_ms=8000, speaker_id="speaker_01")
    placement = seg.add_segment(start_ms=8100, end_ms=8400, speaker_id="speaker_02")
    assert placement.actions == []
    assert placement.turn_id == "turn_001"
    assert placement.speaker_id == "speaker_01"
    assert seg.current_turn.end_ms == 8400


def test_unknown_speaker_never_splits():
    seg = make_segmenter()
    seg.add_segment(start_ms=0, end_ms=4000, speaker_id="speaker_01")
    placement = seg.add_segment(start_ms=4200, end_ms=6000, speaker_id="speaker_unknown")
    assert placement.actions == []
    assert placement.turn_id == "turn_001"


def test_talk_closes_after_long_silence():
    seg = make_segmenter()
    seg.add_segment(start_ms=0, end_ms=3000, speaker_id="speaker_01")
    actions = seg.tick(3000 + 4000 + 2000 + 1)
    assert kinds(actions) == ["finalize_turn"]
    actions = seg.tick(3000 + 25000)
    assert kinds(actions) == ["close_talk"]
    assert actions[0].reason == "silence"
    assert actions[0].talk_id == "talk_001"
    assert seg.current_talk is None


def test_manual_boundary_turn_and_talk():
    seg = make_segmenter()
    seg.add_segment(start_ms=0, end_ms=3000, speaker_id="speaker_01")
    actions = seg.manual_boundary("turn", 3500)
    assert kinds(actions) == ["finalize_turn"]
    assert actions[0].reason == "manual"

    seg.add_segment(start_ms=4000, end_ms=6000, speaker_id="speaker_01")
    actions = seg.manual_boundary("talk", 6500)
    assert kinds(actions) == ["finalize_turn", "close_talk"]
    assert seg.current_talk is None


def test_manual_talk_boundary_can_name_the_next_speaker():
    seg = make_segmenter()
    seg.add_segment(start_ms=0, end_ms=3000, speaker_id="speaker_01")
    actions = seg.manual_boundary("talk", 3500, speaker_id="speaker_02")
    assert kinds(actions) == ["finalize_turn", "close_talk", "open_talk", "open_turn"]
    assert seg.current_turn.speaker_id == "speaker_02"
    assert seg.current_talk.talk_id == "talk_002"


def test_provisional_tick_for_long_turns():
    seg = make_segmenter(provisional_interval_ms=1000)
    seg.add_segment(start_ms=0, end_ms=1500, speaker_id="speaker_01")
    actions = seg.tick(1600)
    assert kinds(actions) == ["provisional"]
    assert actions[0].revision == 1
    # not repeated on the next tick
    assert seg.tick(1700) == []
    assert kinds(seg.tick(3000)) == ["provisional"]


def test_speech_active_prevents_false_silence_close():
    """A long monologue must not be closed while audio is still being decoded."""
    seg = make_segmenter()
    seg.add_segment(start_ms=0, end_ms=3000, speaker_id="speaker_01")
    assert seg.tick(20000, speech_active=True) == []
    assert seg.current_turn is not None
    assert kinds(seg.tick(20000, speech_active=False)) == ["finalize_turn"]


def test_force_close_finalises_everything():
    seg = make_segmenter()
    seg.add_segment(start_ms=0, end_ms=5000, speaker_id="speaker_01")
    actions = seg.force_close(5200)
    assert kinds(actions) == ["finalize_turn", "close_talk"]
    assert seg.current_turn is None and seg.current_talk is None


def test_auto_talk_from_speaker_change_is_opt_in():
    seg = make_segmenter(auto_talk_from_speaker_change=True, talk_close_silence_ms=6000)
    seg.add_segment(start_ms=0, end_ms=3000, speaker_id="speaker_01")
    placement = seg.add_segment(start_ms=15000, end_ms=17000, speaker_id="speaker_02")
    assert kinds(placement.actions) == ["finalize_turn", "close_talk", "open_talk", "open_turn"]
    assert placement.talk_id == "talk_002"
