"""Local scheduler: turn persisted events into scientific notes.

Guarantees implemented here (design document sections 4.4 and 7):

* the task is persisted **before** the skill runs, and the event is only
  acknowledged **after** the note is stored;
* de-duplication key = ``session + target + revision + skill_version + config hash``,
  so a redelivered event never produces a second note;
* a processing lease prevents two runners from working on the same task;
* failures are retried with exponential backoff and end up in a dead-letter
  state instead of being dropped;
* a result derived from an older transcript revision is rejected rather than
  overwriting a newer note;
* a slow model never blocks the recorder - the runner is a separate process
  reading the same database.
"""

from __future__ import annotations

import json
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import SKILL_VERSION
from .agents import AgentAdapter, NoteRequest, OpenAiCompatibleAgent, build_agent
from .events import (
    NOTE_SAVED,
    PROVISIONAL_TICK,
    SESSION_CLOSED,
    TALK_CLOSED,
    TRANSCRIPT_REVISED,
    TURN_FINALIZED,
    EventQueue,
)
from .notes import NoteError, normalise_note, render_markdown, validate_note
from .skill import SkillDocument, load_skill
from .store import MODE_SESSION, MODE_TALK, MODE_TURN, STATE_CLOSED, STATE_FAILED, Store

__all__ = ["Runner", "TaskResult"]

#: Event type -> note mode.  ``None`` means "no note needed" (noop task).
EVENT_MODES: dict[str, str | None] = {
    TURN_FINALIZED: MODE_TURN,
    PROVISIONAL_TICK: MODE_TURN,
    TRANSCRIPT_REVISED: MODE_TURN,
    TALK_CLOSED: MODE_TALK,
    SESSION_CLOSED: MODE_SESSION,
    "session.summary_requested": MODE_SESSION,
    "session.started": None,
    "audio.gap": None,
    "speaker.updated": None,
    NOTE_SAVED: None,
    "session.error": None,
}


@dataclass
class TaskResult:
    task_id: str
    state: str
    note_id: str | None = None
    error: str | None = None


def _utc_plus(seconds: float) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def is_transient_error(exc):
    text=str(exc).lower()
    return any(marker in text for marker in (
        'endpoint unreachable', 'timed out', 'timeout', 'connection reset',
        'http 408', 'http 429', 'http 500', 'http 502', 'http 503', 'http 504'))


class Runner:
    def __init__(
        self,
        cfg,
        store: Store,
        *,
        consumer_id: str | None = None,
        agent: AgentAdapter | None = None,
        skill: SkillDocument | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.events = EventQueue(store.db)
        self.consumer_id = consumer_id or cfg.runner.consumer_id
        self.agent = agent or build_agent(cfg)
        if isinstance(self.agent, OpenAiCompatibleAgent):
            self.agent.usage_callback = lambda stats: self.store.log("info", "model usage", stats)
        self.skill = skill or load_skill(cfg.skill_path)
        self.owner = f"{self.consumer_id}:{id(self):x}"
        self.notes_dir = store.paths.notes if store.paths else Path(".")
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.processed = 0
        self.failed = 0
        self.skipped = 0

    # ------------------------------------------------------------------
    # event -> task
    # ------------------------------------------------------------------
    def poll_and_enqueue(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        batch = self.events.poll(
            self.store.session_id,
            self.consumer_id,
            limit=limit or (self.events.max_seq(self.store.session_id) + 1 if self.store.session_state() in (STATE_CLOSED, STATE_FAILED) else self.cfg.runner.max_events_per_cycle),
            wait_ms=0,
        )
        created: list[dict[str, Any]] = []
        for event in batch["events"]:
            task_id, is_new = self._enqueue_for_event(event)
            if is_new:
                created.append({"seq": event["seq"], "task_id": task_id, "type": event["type"]})
        return created

    def _enqueue_for_event(self, event: dict[str, Any]) -> tuple[str, bool]:
        etype = event.get("type", "")
        mode = EVENT_MODES.get(etype, None)
        if etype == PROVISIONAL_TICK and self.store.session_state() in (STATE_CLOSED, STATE_FAILED):
            # Closed meetings are represented by final turn/talk/session events.
            # Retire old unleased provisional work without erasing its audit row.
            with self.store.db.tx() as conn:
                conn.execute("UPDATE tasks SET state='done', last_error='Superseded by final meeting tasks' WHERE session_id=? AND event_seq=? AND state IN ('pending','failed','dead')", (self.store.session_id, event['seq']))
            mode = None
        if mode is None:
            key = f"noop:{event['seq']}"
            return self.store.enqueue_task(
                type="noop", mode=MODE_TURN, target_id=None, source_revision=1,
                payload={"event": event}, idempotency_key=key, event_seq=event["seq"],
            )

        if mode == MODE_TURN:
            target_id = event.get("turn_id")
            revision = int(event.get("revision", 1))
        elif mode == MODE_TALK:
            target_id = event.get("talk_id")
            revision = int(event.get("revision", 1))
        else:
            target_id = None
            revision = int(event.get("revision", 1))

        key = ":".join(
            [
                "note",
                str(event.get("session_id")),
                mode,
                str(target_id),
                f"r{revision}",
                self.skill.version,
                self.cfg.notes_config_hash(),
            ]
        )
        provisional = etype == PROVISIONAL_TICK or bool(event.get("provisional"))
        if etype == "session.summary_requested":
            key += ":manual:" + event["snapshot_key"]
        if provisional:
            key += f":prov{event.get('end_ms')}:event{event['seq']}"
        return self.store.enqueue_task(
            type=f"note.{mode}",
            mode=mode,
            target_id=target_id,
            source_revision=revision,
            payload={"event": event, "provisional": provisional},
            idempotency_key=key,
            event_seq=event["seq"],
        )

    def request_overview(self):
        """Persist an idempotent overview request for the currently saved text."""
        from .review_layers import signature, epoch
        segments = self.store.segments(include_superseded=False)
        if not segments:
            raise ValueError('还没有可总结的转写内容，请等待第一段转写。')
        provisional = self.store.session_state() not in (STATE_CLOSED, STATE_FAILED)
        digest = hashlib.sha256(json.dumps([(x['segment_id'], signature(x)) for x in segments], ensure_ascii=False).encode()).hexdigest()[:24]
        key = f"{digest}:{epoch(self.store)}:{provisional}:{self.cfg.notes_config_hash()}"
        event, _ = self.events.append(self.store.session_id, 'session.summary_requested', {
            'revision': 1, 'snapshot_key': key, 'provisional': provisional,
            'segment_ids': [x['segment_id'] for x in segments],
            'end_ms': max(x['end_ms'] for x in segments),
        }, dedupe_key='overview:'+key)
        task_id, created = self._enqueue_for_event(event)
        return {'task_id':task_id, 'created':created, 'provisional':provisional, 'end_ms':event['end_ms']}

    # ------------------------------------------------------------------
    # task execution
    # ------------------------------------------------------------------
    def process_tasks(self, *, limit: int | None = None) -> list[TaskResult]:
        tasks = self.store.lease_tasks(self.owner, limit or self.cfg.runner.max_events_per_cycle, self.cfg.runner.lease_seconds)
        results: list[TaskResult] = []
        for task in tasks:
            if task["type"] == "noop":
                self.store.complete_task(task["task_id"])
                results.append(TaskResult(task["task_id"], "done"))
                self.skipped += 1
                continue
            results.append(self._execute(task))
        return results

    def _execute(self, task: dict[str, Any]) -> TaskResult:
        task_id = task["task_id"]
        try:
            if task['type'] == 'transcript.review':
                from .correction import review
                review(self.store, self.agent, self.store.segments())
                self.store.complete_task(task_id)
                return TaskResult(task_id, 'done')
            if task['type'] == 'assistant.query':
                from .meeting_assistant import execute
                if not self.cfg.privacy.allow_cloud_notes:
                    raise ValueError('当前会议未允许云端模型处理')
                execute(self.store,self.agent,task)
                return TaskResult(task_id,'done')
            is_provisional = bool(json.loads(task["payload"] or "{}").get("provisional"))
            request = self._build_request(task)
            previous = self.store.active_note(task['mode'], task['target_id'])
            if is_provisional and previous and request.source_signatures:
                old = json.loads(previous['content_json'])
                if old.get('_source_signatures') == request.source_signatures:
                    self.store.complete_task(task_id, previous['note_id'])
                    return TaskResult(task_id, 'done', previous['note_id'])
            note_json, raw = self.agent.generate(request)
            errors = validate_note(note_json)
            if errors:
                raise NoteError("; ".join(errors[:5]))
            payload = self._finalise(task, request, note_json, raw)
            note_id, created, rejected = self.store.save_note(
                mode=task["mode"],
                target_id=task["target_id"],
                source_revision=int(task["source_revision"]),
                content_json=payload["content_json"],
                content_md=payload["markdown"],
                agent=self.agent.name,
                config_hash=self.cfg.notes_config_hash(),
                idempotency_key=task["idempotency_key"],
                path=str(payload["path"]),
                skill_version=self.skill.version,
                provisional=is_provisional,
            )
            if rejected:
                self.store.fail_task(task_id, rejected, retry_at=None, dead=True)
                self.store.log("warning", f"stale note rejected: {rejected}")
                return TaskResult(task_id, "dead", error=rejected)
            if created:
                payload['path'].write_text(payload['markdown'], encoding='utf-8')
                self.events.append(
                    self.store.session_id,
                    NOTE_SAVED,
                    {
                        "note_id": note_id,
                        "mode": task["mode"],
                        "target_id": task["target_id"],
                        "source_revision": int(task["source_revision"]),
                        "agent": self.agent.name,
                        "skill_version": self.skill.version,
                        "path": str(payload["path"]),
                    },
                    dedupe_key=f"note.saved:{note_id}",
                )
            self.store.complete_task(task_id, note_id)
            self.processed += 1
            self.store.log(
                "info",
                f"note {task['mode']} {task['target_id']} saved",
                {"note_id": note_id, "created": created},
            )
            return TaskResult(task_id, "done", note_id)
        except Exception as exc:
            attempts = int(task.get("attempts", 0)) + 1
            # Transient transport / overload errors must recover even after a long outage.
            transient = is_transient_error(exc)
            dead = not transient and attempts >= self.cfg.runner.max_attempts
            delay = min(120.0, self.cfg.runner.backoff_base_s * (2 ** min(attempts - 1, 6)))
            self.store.fail_task(
                task_id,
                f"{type(exc).__name__}: {exc}",
                retry_at=_utc_plus(delay),
                dead=dead,
            )
            self.failed += 1
            self.store.log(
                "error",
                f"task {task_id} failed (attempt {attempts})",
                {"error": f"{type(exc).__name__}: {exc}", "dead": dead},
            )
            return TaskResult(task_id, "dead" if dead else "failed", error=str(exc))

    # ------------------------------------------------------------------
    def _build_request(self, task: dict[str, Any]) -> NoteRequest:
        from .review_layers import epoch,signature
        review_epoch=epoch(self.store)
        mode = task["mode"]
        target_id = task["target_id"]
        payload = json.loads(task["payload"])
        event = payload.get("event", {})
        provisional = bool(payload.get("provisional"))
        revision = int(task["source_revision"])

        if mode == MODE_TURN:
            segments = self.store.segments(turn_id=target_id, include_superseded=False)
            turn = self.store.turn(target_id) or {}
            talk = self.store.talk(turn.get("talk_id")) if turn.get("talk_id") else None
            context = {
                "turn_id": target_id,
                "talk_id": turn.get("talk_id"),
                "talk_title": (talk or {}).get("title"),
                "speaker_id": turn.get("speaker_id"),
                "speaker_display": self.store.speaker_name(turn.get("speaker_id")),
                "boundary_reason": event.get("boundary_reason"),
                "provisional": provisional,
                "session_title": self.store.session().get("title"),
                "agenda": self.store.session().get("agenda"),
            }
            heading = f"turn {target_id}"
        elif mode == MODE_TALK:
            segments = self.store.segments(talk_id=target_id, include_superseded=False)
            talk = self.store.talk(target_id) or {}
            contexts = []
            for turn in self.store.turns():
                if turn["talk_id"] != target_id:
                    continue
                note = self.store.active_note(MODE_TURN, turn["turn_id"])
                if note:
                    contexts.append(
                        {
                            "turn_id": turn["turn_id"],
                            "summary": json.loads(note["content_json"]).get("summary"),
                            "revision": note["source_revision"],
                        }
                    )
            context = {
                "talk_id": target_id,
                "title": talk.get("title"),
                "speaker_id": talk.get("speaker_id"),
                "speaker_display": self.store.speaker_name(talk.get("speaker_id")),
                "turn_notes": contexts,
                "session_title": self.store.session().get("title"),
                "agenda": self.store.session().get("agenda"),
            }
            heading = talk.get("title") or f"talk {target_id}"
        else:
            segments = self.store.segments(include_superseded=False)
            talk_notes = []
            for note in self.store.notes(mode=MODE_TALK, active_only=True):
                content = json.loads(note["content_json"])
                talk_notes.append(
                    {
                        "talk_id": note["target_id"],
                        "summary": content.get("summary"),
                        "findings": [f.get("claim") for f in content.get("findings", [])][:5],
                    }
                )
            context = {
                "talk_notes": talk_notes,
                "speakers": self.store.speakers(),
                "session_title": self.store.session().get("title"),
                "agenda": self.store.session().get("agenda"),
                "duration_ms": self.store.audio_stats()["duration_ms"],
            }
            heading = self.store.session().get("title") or self.store.session_id

        if event.get('type') == 'session.summary_requested':
            included = set(event['segment_ids'])
            segments = [x for x in segments if x['segment_id'] in included]
            context['summary_through_ms'] = event['end_ms']
            # Later report summaries must not leak into an earlier snapshot.
            context.pop('talk_notes', None)
        from .quality import suspect_segment
        excluded=[s for s in segments if suspect_segment(s)]
        segments=[s for s in segments if not suspect_segment(s)]
        for s in excluded:
            translations={lang:text for lang,text in s.get('confirmed_translations',{}).items() if text.strip()}
            if translations:
                lang=self.cfg.notes.language if self.cfg.notes.language in translations else sorted(translations)[0]
                # Use only the verified translation as evidence; keep flagged ASR out.
                segments.append(dict(s,text=translations[lang],raw_text='',summary_text_source='manual_translation:'+lang))
        segments.sort(key=lambda s:(s['start_ms'],s['end_ms'],s['segment_id']))
        if excluded:
            context['excluded_anomalies']=[{k:s[k] for k in ('segment_id','start_ms','end_ms')} for s in excluded]
            context.pop('talk_notes',None)
            context.pop('turn_notes',None)
        context['review_epoch']=review_epoch
        if any(s.get('review_revision') for s in segments):
            context.pop('turn_notes',None);context.pop('talk_notes',None)
        instructions = ""
        if provisional:
            instructions = "这是截至当前已保存转写的阶段性总结，请标注为 provisional，不要假定演讲已结束，也不要给出最终结论。"
        if mode == MODE_TALK:
            instructions += "\n本模式要合并同一报告人整场报告的全部轮次与关联问答，并保留矛盾与限定条件。"
        if mode == MODE_SESSION:
            instructions += "\n本模式要生成整场会议总览与待跟进事项。"

        signatures = {s['segment_id']: hashlib.sha256(signature(s).encode()).hexdigest()[:16] for s in segments}
        if mode == MODE_TURN and self.cfg.runner.agent != 'offline_extractive':
            previous = self.store.active_note(mode, target_id)
            if previous:
                old = json.loads(previous['content_json'])
                old_signatures = old.get('_source_signatures', {})
                # Revisions invalidate prior prose instead of propagating stale claims.
                if not excluded and old_signatures and all(signatures.get(k)==v for k,v in old_signatures.items()):
                    context['previous_note'] = {k:old[k] for k in ('summary','findings','limitations','uncertainties','methods') if k in old}
                    evidence_ids = {i for f in old.get('findings',[]) for i in f.get('source_segment_ids',[])}
                    segments = [s for s in segments if s['segment_id'] not in old_signatures or s['segment_id'] in evidence_ids]
                    instructions += '\nUpdate the previous note into one coherent current note. Retain still-supported findings, add only new information, and do not repeat the same point. Previously cited segments are included for evidence; they are not new results.'
            from .glossary import contextual_terms
            context['vocabulary_hints_not_evidence'] = {
                'scope':'General chemistry and life sciences',
                'terms':contextual_terms(' '.join(s['text'] for s in segments)),
            }
        if epoch(self.store)!=review_epoch:raise RuntimeError("Human review changed while preparing summary; retry")
        return NoteRequest(
            mode=mode,
            session_id=self.store.session_id,
            target_id=target_id,
            source_revision=revision,
            transcript=segments,
            context=context,
            skill_text=self.skill.body,
            skill_version=self.skill.version,
            language=self.cfg.notes.language,
            instructions=instructions.strip(),
            provisional=provisional,
            source_signatures=signatures,
        )

    def _finalise(self, task: dict[str, Any], request: NoteRequest, note_json: dict[str, Any], raw: str) -> dict[str, Any]:
        note_json["_review_epoch"]=request.context.get("review_epoch",0)
        evidence = {
            seg["segment_id"]: {"start_ms": seg["start_ms"], "end_ms": seg["end_ms"]}
            for seg in request.transcript
        }
        if note_json.get('mind_map') is not None:
            from .meeting_assistant import validate_map
            note_json['mind_map'] = validate_map(note_json['mind_map'], set(evidence))
        # Only keep evidence references that really exist in the supplied input.
        for finding in note_json.get("findings") or []:
            ids = finding.get("source_segment_ids") or []
            finding["source_segment_ids"] = [i for i in ids if i in evidence]
        mode = task["mode"]
        target_id = task["target_id"]
        revision = int(task["source_revision"])
        turn = self.store.turn(target_id) if mode == MODE_TURN else None
        talk = self.store.talk(target_id) if mode in (MODE_TURN, MODE_TALK) else None
        if mode == MODE_TALK:
            talk = self.store.talk(target_id)

        note = normalise_note(
            note_json,
            defaults={
                "mode": mode,
                "session_id": self.store.session_id,
                "target_id": target_id,
                "source_revision": revision,
                "_review_epoch":request.context.get("review_epoch",0),
                "skill_version": self.skill.version,
                "agent": self.agent.name,
                "language": self.cfg.notes.language,
                "provisional": bool(task["payload"] and json.loads(task["payload"]).get("provisional")),
                "title": self.store.session().get("title") or "",
                "heading": (talk or {}).get("title") or request.context.get("heading") or target_id,
                "speaker_id": (turn or talk or {}).get("speaker_id") if (turn or talk) else None,
                "speaker_display": request.context.get("speaker_display"),
                "start_ms": (turn or talk or {}).get("start_ms") if (turn or talk) else None,
                "end_ms": (turn or talk or {}).get("end_ms") if (turn or talk) else None,
            },
        )
        note['language'] = request.language
        note['_source_signatures'] = request.source_signatures
        note['summary_through_ms'] = max((m['end_ms'] for m in evidence.values()), default=0)
        if not note.get("evidence_index"):
            note["evidence_index"] = [
                {"segment_id": sid, "start_ms": meta["start_ms"], "end_ms": meta["end_ms"]}
                for sid, meta in evidence.items()
            ]
        markdown = render_markdown(note, evidence=evidence)
        from .notes import note_filename

        filename = note_filename(mode, target_id, revision)
        review_version=request.context.get('review_epoch',0)
        snapshot=hashlib.sha256(task['idempotency_key'].encode()).hexdigest()[:12]
        filename=Path(filename).stem+f"_review{review_version}_{snapshot}.md"
        path = self.notes_dir / filename
        return {"content_json": note, "markdown": markdown, "path": path}

    # ------------------------------------------------------------------
    # ack
    # ------------------------------------------------------------------
    def ack_contiguous(self) -> int:
        """Advance the cursor over every *consecutive* event whose task is done.

        The cursor only ever moves across a prefix in which every event has a
        completed task.  An event that has not been polled yet, or whose task is
        still pending or dead, stops the walk - otherwise a batch failure could
        silently lose work.
        """
        cursor = self.events.cursor(self.store.session_id, self.consumer_id)
        max_seq = self.events.max_seq(self.store.session_id)
        last_ok = cursor
        seq = cursor
        while seq < max_seq:
            seq += 1
            row = self.store.db.query_one(
                "SELECT state FROM tasks WHERE session_id=? AND event_seq=?",
                (self.store.session_id, seq),
            )
            if row is None or row["state"] != "done":
                break
            last_ok = seq
        if last_ok > cursor:
            self.events.ack(self.store.session_id, self.consumer_id, last_ok)
        return last_ok

    # ------------------------------------------------------------------
    def run_cycle(self) -> dict[str, Any]:
        from .correction import clean_repetitions
        segments = self.store.segments()
        clean_repetitions(self.store, segments)
        if (segments and self.cfg.runner.agent == 'openai_compatible'
                and self.cfg.privacy.allow_cloud_notes and hasattr(self.agent, 'complete_json')):
            digest = hashlib.sha256(json.dumps([(s['segment_id'], s.get('raw_text', s['text'])) for s in segments]).encode()).hexdigest()
            # Separate durable task: summary deduplication/failure cannot drop proofreading.
            self.store.enqueue_task(type='transcript.review', mode=MODE_TURN,
                target_id=None, source_revision=1, payload={},
                idempotency_key='transcript.review:v8:'+digest)
        created = self.poll_and_enqueue()
        results = self.process_tasks()
        cursor = self.ack_contiguous()
        return {
            "enqueued": len(created),
            "processed": len(results),
            "cursor": cursor,
            "lag": self.events.lag(self.store.session_id, self.consumer_id),
            "tasks": self.store.task_counts(),
        }

    def run_forever(self, *, follow: bool = False, max_idle_cycles: int = 0, on_cycle=None) -> int:
        """Consume events until the session is closed and everything is done."""
        idle = 0
        while True:
            summary = self.run_cycle()
            if on_cycle is not None:
                on_cycle(summary)
            state = self.store.session_state()
            pending = self.store.task_counts().get("pending", 0) + self.store.task_counts().get("failed", 0)
            lag = summary["lag"]
            if summary["enqueued"] or summary["processed"]:
                idle = 0
            else:
                idle += 1
            if state in (STATE_CLOSED, STATE_FAILED) and lag == 0 and pending == 0:
                return 0
            if not follow and idle >= max(1, max_idle_cycles):
                return 0
            # A quiet meeting is still active: keep waiting until it closes.
            time.sleep(self.cfg.runner.poll_wait_ms / 1000.0)
