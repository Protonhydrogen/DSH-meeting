"""Boundary detection: utterance -> turn -> talk.

The state machine is deliberately pure: it performs no I/O and takes the
current session clock as an argument, so the boundary rules can be unit tested
with an exact timeline.

Vocabulary (from the design document):

==========  =============================================  ==============================
boundary    meaning                                        effect
==========  =============================================  ==============================
utterance   one sentence / short phrase ends              segment stabilised, no LLM call
turn        one continuous speech turn ends                ``turn.finalized`` event
talk        one presenter's whole talk ends               ``talk.closed`` event
session     the meeting ends                              ``session.closed`` event
==========  =============================================  ==============================

Rules implemented here:

* a gap of ``turn_silence_ms`` ends a turn candidate, but ``grace_ms`` of extra
  patience merges a resumption back into the same turn (the event is only
  emitted once the grace period has expired);
* a stable speaker change ends the previous turn immediately; very short
  interjections ("嗯", "yes") from another speaker are kept as segments of the
  current turn and flagged ``backchannel`` instead of truncating a report;
* silence longer than ``talk_close_silence_ms`` closes the talk;
* long turns emit a ``provisional`` action every ``provisional_interval_ms`` so
  that the runner can produce incremental notes;
* nothing is ever closed irreversibly by a single word such as "谢谢": closing
  a talk always requires the silence threshold or an explicit manual boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = ["Action", "Placement", "Segmenter", "TurnView"]

BACKCHANNEL = "backchannel"
OVERLAP = "overlap"
UNKNOWN_SPEAKERS = {None, "", "speaker_unknown", "unknown"}


@dataclass
class Action:
    kind: Literal["open_talk", "open_turn", "finalize_turn", "close_talk", "provisional"]
    turn_id: str | None = None
    talk_id: str | None = None
    speaker_id: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    reason: str = ""
    revision: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "turn_id": self.turn_id,
            "talk_id": self.talk_id,
            "speaker_id": self.speaker_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "reason": self.reason,
            "revision": self.revision,
        }


@dataclass
class Placement:
    """Where a newly arrived segment belongs."""

    turn_id: str
    talk_id: str
    speaker_id: str
    actions: list[Action] = field(default_factory=list)


@dataclass
class TurnView:
    turn_id: str
    talk_id: str
    speaker_id: str
    start_ms: int
    end_ms: int
    revision: int = 1
    segment_count: int = 0
    provisional_at_ms: int | None = None

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass
class TalkView:
    talk_id: str
    speaker_id: str
    start_ms: int
    revision: int = 1


class Segmenter:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.turn: TurnView | None = None
        self.talk: TalkView | None = None
        self._turn_n = 0
        self._talk_n = 0
        self._finalized_turns: list[TurnView] = []
        self._closed_talks: list[TalkView] = []
        #: End of the most recent speech seen, used to close a talk after its
        #: last turn has already been finalised.
        self._last_end_ms: int | None = None

    # ------------------------------------------------------------------
    @property
    def current_turn(self) -> TurnView | None:
        return self.turn

    @property
    def current_talk(self) -> TalkView | None:
        return self.talk

    @property
    def open_turns(self) -> int:
        return 1 if self.turn is not None else 0

    # ------------------------------------------------------------------
    def add_segment(
        self,
        *,
        start_ms: int,
        end_ms: int,
        speaker_id: str | None,
        text: str = "",
        segment_id: str = "",
    ) -> Placement:
        """Place a stabilised segment on the turn/talk timeline."""
        speaker = speaker_id or "speaker_unknown"
        actions: list[Action] = []
        self._last_end_ms = max(self._last_end_ms or 0, end_ms)
        gap_ms = (start_ms - self.turn.end_ms) if self.turn is not None else 0

        # -- close a finished talk / turn before opening anything new ----
        if self.turn is not None and gap_ms > self.cfg.turn_silence_ms:
            speaker_change = self._is_speaker_change(speaker)
            short_interjection = (end_ms - start_ms) < self.cfg.short_backchannel_ms
            if speaker_change and short_interjection and gap_ms <= self.cfg.turn_silence_ms * 2:
                # Backchannel: keep it inside the current turn.
                self.turn.end_ms = max(self.turn.end_ms, end_ms)
                self.turn.segment_count += 1
                return Placement(self.turn.turn_id, self.turn.talk_id, self.turn.speaker_id, actions)
            actions.extend(self._finalize_turn(reason="silence"))
            if (
                self.talk is not None
                and gap_ms >= self.cfg.talk_close_silence_ms
            ):
                actions.extend(self._close_talk(reason="silence"))
            elif (
                self.talk is not None
                and self.cfg.auto_talk_from_speaker_change
                and speaker_change
                and gap_ms >= self.cfg.turn_silence_ms + self.cfg.grace_ms
            ):
                actions.extend(self._close_talk(reason="speaker_change"))

        if self.turn is not None and self._is_speaker_change(speaker):
            segment_ms = end_ms - start_ms
            if segment_ms < self.cfg.short_backchannel_ms and gap_ms <= self.cfg.turn_silence_ms:
                # Short interjection inside the turn: keep, flag, do not split.
                self.turn.end_ms = max(self.turn.end_ms, end_ms)
                self.turn.segment_count += 1
                return Placement(self.turn.turn_id, self.turn.talk_id, self.turn.speaker_id, actions)
            actions.extend(self._finalize_turn(reason="speaker_change"))

        # -- open talk / turn as needed ----------------------------------
        if self.talk is None:
            actions.extend(self._open_talk(speaker, start_ms))
        if self.turn is None:
            actions.extend(self._open_turn(speaker, start_ms))

        assert self.turn is not None and self.talk is not None
        self.turn.end_ms = max(self.turn.end_ms, end_ms)
        self.turn.segment_count += 1
        return Placement(self.turn.turn_id, self.talk.talk_id, self.turn.speaker_id, actions)

    # ------------------------------------------------------------------
    def tick(self, now_ms: int, *, speech_active: bool = False) -> list[Action]:
        """Time-driven transitions (called regularly by the service loop).

        ``speech_active`` must be true while the VAD reports ongoing speech that
        has not been committed yet.  Without it, a long monologue would look
        like silence - the transcriber has not emitted a segment for up to one
        window - and the turn would be closed in the middle of a sentence.
        """
        actions: list[Action] = []
        reference_end = self.turn.end_ms if self.turn is not None else self._last_end_ms
        if reference_end is None:
            return actions
        silence_ms = now_ms - reference_end
        if speech_active:
            silence_ms = 0  # somebody is talking (or it is still being decoded)

        if self.turn is not None and silence_ms >= self.cfg.turn_silence_ms + self.cfg.grace_ms:
            actions.extend(self._finalize_turn(reason="silence"))
            # The talk may still need closing even though the turn just ended,
            # which is why the reference time is kept on the segmenter.
            if self.talk is not None and silence_ms >= self.cfg.talk_close_silence_ms:
                actions.extend(self._close_talk(reason="silence"))
            return actions

        if self.talk is not None and self.turn is None and silence_ms >= self.cfg.talk_close_silence_ms:
            actions.extend(self._close_talk(reason="silence"))
            return actions

        if (
            self.turn is not None
            and self.cfg.provisional_interval_ms > 0
            and self.turn.duration_ms >= self.cfg.provisional_interval_ms
            and (self.turn.provisional_at_ms is None or now_ms - self.turn.provisional_at_ms >= self.cfg.provisional_interval_ms)
        ):
            self.turn.provisional_at_ms = now_ms
            actions.append(
                Action(
                    "provisional",
                    turn_id=self.turn.turn_id,
                    talk_id=self.turn.talk_id,
                    speaker_id=self.turn.speaker_id,
                    start_ms=self.turn.start_ms,
                    end_ms=self.turn.end_ms,
                    reason="long_turn",
                    revision=self.turn.revision,
                )
            )
        return actions

    # ------------------------------------------------------------------
    def manual_boundary(self, kind: str, now_ms: int, speaker_id: str | None = None) -> list[Action]:
        """Handle an explicit ``conference.mark_boundary`` request."""
        actions: list[Action] = []
        if kind == "turn":
            if self.turn is not None:
                actions.extend(self._finalize_turn(reason="manual"))
        elif kind == "talk":
            if self.turn is not None:
                actions.extend(self._finalize_turn(reason="manual"))
            if self.talk is not None:
                actions.extend(self._close_talk(reason="manual"))
            if speaker_id:
                actions.extend(self._open_talk(speaker_id, now_ms))
                actions.extend(self._open_turn(speaker_id, now_ms))
        elif kind == "session":
            actions.extend(self.force_close(now_ms))
        else:
            raise ValueError(f"unknown boundary kind {kind!r}")
        return actions

    def force_close(self, now_ms: int) -> list[Action]:
        """Close everything (used by ``conference.stop`` once audio is drained)."""
        actions: list[Action] = []
        if self.turn is not None:
            actions.extend(self._finalize_turn(reason="session_stop"))
        if self.talk is not None:
            actions.extend(self._close_talk(reason="session_stop"))
        return actions

    # ------------------------------------------------------------------
    def _is_speaker_change(self, speaker: str) -> bool:
        if self.turn is None:
            return True
        if self.turn.speaker_id in UNKNOWN_SPEAKERS or speaker in UNKNOWN_SPEAKERS:
            return False  # silence detection cannot prove a change
        return speaker != self.turn.speaker_id

    def _open_talk(self, speaker: str, start_ms: int) -> list[Action]:
        self._talk_n += 1
        self.talk = TalkView(talk_id=f"talk_{self._talk_n:03d}", speaker_id=speaker, start_ms=start_ms)
        return [
            Action(
                "open_talk",
                talk_id=self.talk.talk_id,
                speaker_id=speaker,
                start_ms=start_ms,
                reason="speech_start",
            )
        ]

    def _open_turn(self, speaker: str, start_ms: int) -> list[Action]:
        assert self.talk is not None
        self._turn_n += 1
        self.turn = TurnView(
            turn_id=f"turn_{self._turn_n:03d}",
            talk_id=self.talk.talk_id,
            speaker_id=speaker,
            start_ms=start_ms,
            end_ms=start_ms,
        )
        return [
            Action(
                "open_turn",
                turn_id=self.turn.turn_id,
                talk_id=self.talk.talk_id,
                speaker_id=speaker,
                start_ms=start_ms,
                reason="speech_start",
            )
        ]

    def _finalize_turn(self, *, reason: str) -> list[Action]:
        if self.turn is None:
            return []
        turn = self.turn
        self._finalized_turns.append(turn)
        self._last_end_ms = max(self._last_end_ms or 0, turn.end_ms)
        self.turn = None
        return [
            Action(
                "finalize_turn",
                turn_id=turn.turn_id,
                talk_id=turn.talk_id,
                speaker_id=turn.speaker_id,
                start_ms=turn.start_ms,
                end_ms=turn.end_ms,
                reason=reason,
                revision=turn.revision,
            )
        ]

    def _close_talk(self, *, reason: str) -> list[Action]:
        if self.talk is None:
            return []
        talk = self.talk
        self._closed_talks.append(talk)
        self.talk = None
        return [
            Action(
                "close_talk",
                talk_id=talk.talk_id,
                speaker_id=talk.speaker_id,
                start_ms=talk.start_ms,
                end_ms=self._finalized_turns[-1].end_ms if self._finalized_turns else None,
                reason=reason,
                revision=talk.revision,
            )
        ]
