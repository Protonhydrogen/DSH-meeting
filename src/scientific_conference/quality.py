"""Reuse the existing repetition guard at display and summary boundaries."""
from .correction import REPETITION, REPETITION_MARKER, repetition_loop


def suspect_segment(segment):
    if segment.get("human_confirmed"):return False
    if 'repetition_suspected' in (segment.get('flags') or []):return True
    text=segment.get('raw_text') or segment.get('text') or ''
    return bool(REPETITION_MARKER in segment.get('text','') or REPETITION.search(text)
        or repetition_loop(text,start_ms=segment.get('start_ms'),end_ms=segment.get('end_ms')))
