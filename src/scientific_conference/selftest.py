"""End-to-end self test with synthesised, ground-truthed meeting audio.

``conference selftest`` builds a small multi-speaker "meeting" with Windows SAPI
voices (two or three different voices, controlled silence gaps, numbers and
terminology in the text), runs the *real* pipeline over it - capture -> VAD ->
incremental ASR -> turn/talk boundaries -> events -> runner -> notes - and
reports measurable numbers:

* character error rate (CER) of the local ASR against the known text
* speaker labelling accuracy of the diarizer (best label matching)
* turn boundary precision/recall against the designed turn structure
* ASR latency percentiles and the implied subtitle / note latency
* audio integrity (recorded milliseconds vs source milliseconds, drops, gaps)
* event, task and note accounting, plus de-duplication behaviour

The result is written to ``docs/04-acceptance.md`` style Markdown (in the
workdir by default) so the numbers can be pasted into the project record.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import SKILL_VERSION, __version__
from .agents import OfflineExtractiveAgent
from .asr import build_backend
from .db import utc_now
from .recorder import Recorder, RecorderConfig
from .runner import Runner
from .service import open_store
from .store import Store

__all__ = ["run_selftest", "make_test_audio", "list_sapi_voices"]


# --------------------------------------------------------------------------
# synthesised meeting definition
# --------------------------------------------------------------------------
@dataclass
class Utterance:
    speaker: str
    gap_ms: int
    text: str
    voice: str = "female"


#: A short academic talk followed by a Q&A block.  Numbers are written the way
#: the recogniser is expected to render them ("12%", "0.001", "482"), so the CER
#: measures recognition rather than number spelling - the synthesiser reads them
#: aloud as Chinese numerals in any case.
#:
#: The second speaker asks in English, which exercises the mixed-language path
#: and gives the diarizer two genuinely different voices.
SCRIPT: list[Utterance] = [
    Utterance("speaker_a", 0, "各位老师同学好，今天汇报的题目是肠道菌群与代谢标志物的关联研究。"),
    Utterance("speaker_a", 500, "我们提出的假设是，菌群多样性下降与炎症标志物升高呈正相关。"),
    Utterance("speaker_a", 400, "方法上采用16S核糖体测序，纳入三个独立队列，共482名受试者。"),
    Utterance("speaker_a", 900, "主要终点是C反应蛋白，次要终点包括白细胞介素6和体重指数。"),
    Utterance("speaker_a", 350, "结果显示多样性指数每下降一个标准差，C反应蛋白升高12%。"),
    Utterance("speaker_a", 600, "这个关联在三个队列中都成立，合并后的p值小于0.001。"),
    Utterance("speaker_a", 400, "我们还做了孟德尔随机化分析，效应方向与观察性结果一致。"),
    Utterance("speaker_a", 800, "局限在于样本以东亚人群为主，饮食问卷依赖回忆，可能存在测量误差。"),
    Utterance(
        "speaker_b",
        5200,
        "How many subjects were included, and is the sample size sufficient for subgroup analysis?",
        voice="en_male",
    ),
    Utterance("speaker_a", 700, "我们做了功效分析，主要终点在80%功效下可以支持两个亚组。"),
    Utterance("speaker_b", 600, "Will the data and the questionnaire be publicly available?", voice="en_male"),
    Utterance("speaker_a", 500, "代码会随论文公开，问卷因为伦理限制只能提供变量字典。"),
    Utterance("speaker_a", 450, "下一步计划是在独立队列中做前瞻性验证。"),
]


def list_sapi_voices() -> list[str]:
    """Names of the installed Windows SAPI voices."""
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def _pick_voice(voices: list[str], kind: str, used: set[str]) -> str | None:
    """Pick a SAPI voice for a speaker, honouring the requested language."""
    zh = [v for v in voices if any(k in v for k in ("Huihui", "Yaoyao", "Kangkang", "Xiaoxiao", "Yunxi", "Chinese"))]
    en = [v for v in voices if any(k in v for k in ("Zira", "David", "Mark", "Hazel", "Guy", "English"))]
    if kind == "en_male":
        ordered = [v for v in en if any(k in v for k in ("David", "Mark", "Guy"))] + en
    elif kind == "en_female":
        ordered = [v for v in en if any(k in v for k in ("Zira", "Hazel"))] + en
    else:
        ordered = zh + en
    for voice in ordered:
        if voice not in used:
            return voice
    return ordered[0] if ordered else (voices[0] if voices else None)


def _pitch_shift(pcm: np.ndarray, sample_rate: int, factor: float) -> np.ndarray:
    """Shift the pitch (and shorten the clip) by resampling.

    Used to turn one TTS voice into a second, clearly different voice without
    needing a second Chinese voice pack.  ``factor > 1`` raises the pitch.
    """
    from .audio import resample_fixed

    x = np.asarray(pcm, dtype=np.float32) / 32768.0
    y = resample_fixed(x, int(sample_rate * factor), sample_rate)
    return (np.clip(y, -1.0, 1.0) * 32767).astype(np.int16)


_LAST_TTS_ERROR: str | None = None


def _run_tts_batch(spec_path: Path, manifest_path: Path) -> dict[str, Any] | None:
    """Synthesise every utterance with ``scripts/make_test_audio.ps1``."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "make_test_audio.ps1"
    if not script.exists():
        return None
    try:
        proc = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(script), "-SpecPath", str(spec_path), "-ManifestPath", str(manifest_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
    except Exception as exc:
        global _LAST_TTS_ERROR
        _LAST_TTS_ERROR = f"{type(exc).__name__}: {exc}"
        return None
    if not manifest_path.exists():
        _LAST_TTS_ERROR = (proc.stderr or proc.stdout or "no manifest produced")[:600]
        return None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        _LAST_TTS_ERROR = f"manifest parse failed: {exc}"
        return None


def _synth_utterance(text: str, voice: str | None, out_path: Path, rate: int = 0) -> bool:
    global _LAST_TTS_ERROR
    escaped = text.replace("'", "''")
    voice_line = f"$s.SelectVoice('{voice}');" if voice else ""
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"{voice_line}"
        f"$s.Rate = {int(rate)};"
        f"$s.SetOutputToWaveFile('{out_path}');"
        f"$s.Speak('{escaped}');"
        "$s.Dispose();"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    except Exception as exc:
        _LAST_TTS_ERROR = f"{type(exc).__name__}: {exc}"
        return False
    if proc.returncode != 0:
        _LAST_TTS_ERROR = (proc.stderr or proc.stdout or "unknown error")[:600]
        return False
    if not out_path.exists() or out_path.stat().st_size <= 1000:
        _LAST_TTS_ERROR = f"no audio produced for {out_path.name}"
        return False
    return True


def make_test_audio(out_path: Path, *, workdir: Path, script: list[Utterance] | None = None) -> dict[str, Any]:
    """Synthesise the meeting audio and return the ground truth.

    Falls back to a formant "speech-like" signal per speaker when SAPI is not
    available; in that case ``tts=False`` is reported and the ASR metrics are
    marked as not meaningful.
    """
    from .audio import read_wav, write_wav_int16

    items = script or SCRIPT
    workdir.mkdir(parents=True, exist_ok=True)
    voices = list_sapi_voices()
    by_speaker: dict[str, str | None] = {}
    pitch_by_speaker: dict[str, float] = {}
    used: set[str] = set()
    sample_rate = 16000
    pieces: list[np.ndarray] = []
    truth: list[dict[str, Any]] = []
    cursor = 0
    tts_ok = bool(voices)
    handed_out = 0

    # Plan voices first.  Each speaker gets the first still-unused voice that
    # matches the requested language; when no separate voice is left, the
    # primary voice is reused pitch-shifted, which stays intelligible for ASR
    # while being acoustically a different talker.
    speakers: list[str] = []
    kinds: dict[str, str] = {}
    for item in items:
        if item.speaker not in speakers:
            speakers.append(item.speaker)
            kinds[item.speaker] = item.voice
    for i, speaker in enumerate(speakers):
        voice = _pick_voice(voices, kinds[speaker], used)
        reused = voice in used
        by_speaker[speaker] = voice
        if voice:
            used.add(voice)
        if reused:
            pitch_by_speaker[speaker] = round(1.18 + 0.12 * i, 3)

    specs = []
    for i, item in enumerate(items):
        factor = pitch_by_speaker.get(item.speaker, 1.0)
        specs.append(
            {
                "text": item.text,
                "voice": by_speaker.get(item.speaker),
                "rate": 0 if factor == 1.0 else -2,
                "out": str(workdir / f"utt_{i:03d}.wav"),
            }
        )
    manifest = None
    if tts_ok:
        spec_path = workdir / "tts_spec.json"
        spec_path.write_text(json.dumps({"items": specs}, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest = _run_tts_batch(spec_path, workdir / "tts_manifest.json")
        if manifest is None or not manifest.get("ok", False):
            tts_ok = False
        else:
            produced = {str(it.get("out")): int(it.get("bytes") or 0) for it in manifest.get("items", [])}
            missing = [s["out"] for s in specs if produced.get(s["out"], 0) <= 1000]
            if missing:
                _LAST_TTS_ERROR = f"{len(missing)} utterance(s) produced no audio, e.g. {Path(missing[0]).name}"
                tts_ok = False

    for i, item in enumerate(items):
        cursor += item.gap_ms
        pcm: np.ndarray | None = None
        if tts_ok:
            wav = workdir / f"utt_{i:03d}.wav"
            ok = wav.exists() and wav.stat().st_size > 1000
            if not ok:
                ok = _synth_utterance(
                    item.text,
                    by_speaker.get(item.speaker),
                    wav,
                    rate=specs[i]["rate"],
                )
            if ok:
                data, rate_hz = read_wav(wav)
                sample_rate = rate_hz
                mono = data if data.ndim == 1 else data.mean(axis=1)
                pcm = (np.clip(mono, -1, 1) * 32767).astype(np.int16)
                factor = pitch_by_speaker.get(item.speaker, 1.0)
                if factor != 1.0:
                    pcm = _pitch_shift(pcm, sample_rate, factor)
            else:
                tts_ok = False
        if pcm is None:
            pcm = _formant_utterance(item.text, item.speaker, sample_rate, seed=i)

        start_ms = cursor
        duration_ms = int(pcm.size / sample_rate * 1000)
        end_ms = start_ms + duration_ms
        # silence that separates this utterance from the previous one
        pieces.append(np.zeros(int(item.gap_ms / 1000.0 * sample_rate), dtype=np.int16))
        pieces.append(pcm)
        truth.append(
            {
                "index": i,
                "speaker": item.speaker,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": item.text,
                "voice": by_speaker.get(item.speaker) if tts_ok else None,
                "pitch_factor": pitch_by_speaker.get(item.speaker, 1.0),
            }
        )
        cursor = end_ms

    pieces.append(np.zeros(int(1.0 * sample_rate), dtype=np.int16))
    audio = np.concatenate([p for p in pieces if p.size]) if any(p.size for p in pieces) else np.zeros(sample_rate, dtype=np.int16)
    write_wav_int16(out_path, audio, sample_rate)
    return {
        "path": str(out_path),
        "sample_rate": sample_rate,
        "duration_ms": int(audio.size / sample_rate * 1000),
        "tts": tts_ok,
        "tts_error": _LAST_TTS_ERROR if not tts_ok else None,
        "voices": voices,
        "speaker_voices": {k: v for k, v in by_speaker.items()},
        "truth": truth,
    }


def _formant_utterance(text: str, speaker: str, sample_rate: int, *, seed: int = 0) -> np.ndarray:
    """Speech-like filler used only to keep the timing test meaningful."""
    rng = np.random.default_rng(seed)
    base = 120.0 if speaker.endswith("b") else 210.0
    duration_s = max(0.8, min(8.0, len(text) * 0.16))
    n = int(duration_s * sample_rate)
    t = np.arange(n) / sample_rate
    f0 = base * (1.0 + 0.05 * np.sin(2 * np.pi * 1.7 * t))
    phase = 2 * np.pi * np.cumsum(f0) / sample_rate
    sig = sum(np.sin(k * phase) / k for k in (1, 2, 3, 4, 5))
    # syllable-rate amplitude modulation makes it look like speech to the VAD
    sig *= 0.5 + 0.5 * np.clip(np.sin(2 * np.pi * 4.0 * t), 0, 1)
    sig += rng.standard_normal(n) * 0.01
    sig = sig / (np.max(np.abs(sig)) + 1e-9) * 0.35
    return (sig * 32767).astype(np.int16)


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------
def _normalise_text(text: str) -> str:
    import unicodedata

    out = []
    for ch in unicodedata.normalize("NFKC", str(text)):
        if ch.isspace():
            continue
        if unicodedata.category(ch).startswith("P"):
            continue
        out.append(ch.lower())
    return "".join(out)


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def cn_numbers_to_digits(text: str) -> str:
    """Convert Chinese numerals to Arabic digits (for fair CER comparison)."""
    out = []
    i = 0
    while i < len(text):
        if text[i] in _CN_DIGITS or text[i] in _CN_UNITS:
            j = i
            while j < len(text) and (text[j] in _CN_DIGITS or text[j] in _CN_UNITS):
                j += 1
            token = text[i:j]
            out.append(str(_parse_cn_number(token)) if token else "")
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _parse_cn_number(token: str) -> int:
    total, section, number = 0, 0, 0
    for ch in token:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if number == 0:
                number = 1
            section += number * unit
            number = 0
    return total + section + number


def edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _best_label_mapping(pred: list[str], truth: list[str]) -> dict[str, str]:
    """Greedy best matching between predicted labels and ground-truth speakers."""
    pairs: dict[tuple[str, str], int] = {}
    for p, t in zip(pred, truth):
        pairs[(p, t)] = pairs.get((p, t), 0) + 1
    mapping: dict[str, str] = {}
    used_truth: set[str] = set()
    for (p, t), count in sorted(pairs.items(), key=lambda kv: -kv[1]):
        if p in mapping or t in used_truth:
            continue
        mapping[p] = t
        used_truth.add(t)
    return mapping


class _TimedBackend:
    """Wrap an ASR backend to measure per-window wall time."""

    def __init__(self, inner):
        self.inner = inner
        self.name = inner.name
        self.durations: list[float] = []
        self.audio_ms: list[float] = []

    def transcribe(self, samples, sample_rate, **kwargs):
        start = time.perf_counter()
        try:
            return self.inner.transcribe(samples, sample_rate, **kwargs)
        finally:
            self.durations.append(time.perf_counter() - start)
            self.audio_ms.append(len(samples) / float(sample_rate) * 1000.0)

    def close(self):
        return self.inner.close()

    def info(self):
        return self.inner.info()


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, int(math.ceil(pct / 100.0 * len(ordered))) - 1))
    return ordered[k]


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------
def run_selftest(
    cfg,
    *,
    workdir: str | None = None,
    keep: bool = True,
    asr: str = "faster_whisper",
    wav: str | None = None,
    diarization: str = "none",
) -> dict[str, Any]:
    started = time.time()
    import copy

    cfg = copy.deepcopy(cfg)
    root = Path(workdir) if workdir else (cfg.output_root / "_selftest")
    root.mkdir(parents=True, exist_ok=True)
    audio_dir = root / "audio_src"
    audio_dir.mkdir(parents=True, exist_ok=True)

    if wav:
        from .audio import read_wav

        data, rate = read_wav(wav)
        mono = data if data.ndim == 1 else data.mean(axis=1)
        ground = {
            "path": str(wav),
            "sample_rate": rate,
            "duration_ms": int(mono.size / rate * 1000),
            "tts": False,
            "truth": [],
            "voices": [],
            "speaker_voices": {},
        }
    else:
        ground = make_test_audio(audio_dir / "selftest_meeting.wav", workdir=audio_dir)

    session_id = f"selftest_{time.strftime('%Y%m%d_%H%M%S')}"
    # Configure the run: local ASR, chosen diarizer, tight-but-realistic boundaries.
    cfg.project.output_root = str(root / "meetings")
    cfg.asr.backend = asr
    cfg.diarization.backend = diarization
    cfg.asr.model = cfg.asr.model or "small"
    cfg.segmenter.turn_silence_ms = 3000
    cfg.segmenter.grace_ms = 1500
    cfg.segmenter.talk_close_silence_ms = 4000
    cfg.segmenter.provisional_interval_ms = 60000
    cfg.privacy.require_consent = False

    store = Store.create(
        Path(cfg.project.output_root),
        session_id,
        cfg,
        device_id="file",
        device_name="selftest wav",
        sample_rate=ground["sample_rate"],
        channels=1,
        consent=True,
        title="自测会议（合成音频）",
    )

    timed: _TimedBackend | None = None
    if asr == "mock":
        from .asr import MockAsrBackend

        script = [
            {
                "start_ms": item["start_ms"],
                "end_ms": item["end_ms"],
                "text": item["text"],
                "speaker_id": item["speaker"],
            }
            for item in ground["truth"]
        ]
        backend = MockAsrBackend(script=script)
    else:
        timed = _TimedBackend(build_backend(cfg))
        backend = timed

    rec_cfg = RecorderConfig(
        session_id=session_id,
        output_root=Path(cfg.project.output_root),
        source="file",
        wav_path=ground["path"],
        realtime=False,
        tick_ms=100,
    )
    recorder = Recorder(cfg, store, rec_cfg, backend_factory=lambda: backend)
    record_start = time.perf_counter()
    code = recorder.run()
    record_wall_s = time.perf_counter() - record_start

    store_ro = open_store(cfg, session_id)
    runner = Runner(cfg, store_ro, agent=OfflineExtractiveAgent(), consumer_id="selftest")
    runner_start = time.perf_counter()
    runner_code = runner.run_forever(follow=True, max_idle_cycles=3)
    runner_wall_s = time.perf_counter() - runner_start

    metrics = collect_metrics(cfg, session_id, ground, timed, recorder, record_wall_s, runner_wall_s)
    metrics["exit_codes"] = {"recorder": code, "runner": runner_code}

    # --- duplicate delivery / idempotency check -------------------------
    before = len(store_ro.notes(active_only=False))
    runner.run_cycle()
    runner.run_cycle()
    after = len(store_ro.notes(active_only=False))
    metrics["idempotency"] = {
        "notes_before": before,
        "notes_after_repeat_cycles": after,
        "duplicates": after - before,
        "ok": after == before,
    }

    # --- audio integrity: chunk files re-readable -----------------------
    metrics["audio_integrity"] = check_audio_integrity(store_ro)

    ok_flags = [
        metrics["events"].get("turn.finalized", 0) >= 2,
        metrics["notes"]["active"] >= 2,
        metrics["idempotency"]["ok"],
        metrics["audio_integrity"]["ok"],
        metrics["audio"]["dropped_blocks"] == 0,
    ]
    if ground["truth"] and asr != "mock":
        ok_flags.append((metrics["asr"].get("cer") or 1.0) < 0.35)
    if ground["truth"] and diarization != "none":
        accuracy = metrics["diarization"].get("accuracy_estimate")
        ok_flags.append(accuracy is not None and accuracy >= 0.6)
    metrics["ok"] = all(ok_flags)
    metrics["ok_flags"] = ok_flags

    report = write_report(root, cfg, session_id, ground, metrics)
    metrics["report_path"] = str(report)
    metrics["session_id"] = session_id
    metrics["workdir"] = str(root)
    metrics["elapsed_s"] = round(time.time() - started, 1)
    metrics["versions"] = {"package": __version__, "skill": SKILL_VERSION, "python": sys.version.split()[0]}
    (root / "selftest_result.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    if not keep:
        shutil.rmtree(root / "audio_src", ignore_errors=True)
    return metrics


def collect_metrics(cfg, session_id: str, ground: dict[str, Any], timed, recorder: Recorder, record_wall_s: float, runner_wall_s: float) -> dict[str, Any]:
    store = open_store(cfg, session_id)
    segments = store.segments()
    turns = store.turns()
    talks = store.talks()
    events = store.db.query("SELECT type, COUNT(*) AS n FROM events WHERE session_id=? GROUP BY type", (session_id,))
    event_counts = {row["type"]: int(row["n"]) for row in events}
    notes = store.notes(active_only=True)
    audio = recorder.audio_stats()

    predicted_text = "".join(seg["text"] for seg in segments)
    truth_text = "".join(item["text"] for item in ground["truth"])
    cer = None
    if ground["truth"]:
        a = cn_numbers_to_digits(_normalise_text(truth_text))
        b = cn_numbers_to_digits(_normalise_text(predicted_text))
        cer = round(edit_distance(a, b) / max(1, len(a)), 4)

    # speaker labelling accuracy
    speaker_acc = None
    speaker_map: dict[str, str] = {}
    if ground["truth"] and segments:
        pred_labels, true_labels = [], []
        for seg in segments:
            mid = (seg["start_ms"] + seg["end_ms"]) / 2
            match = next((t for t in ground["truth"] if t["start_ms"] <= mid <= t["end_ms"]), None)
            if match is None:
                continue
            pred_labels.append(str(seg["speaker_id"] or "unknown"))
            true_labels.append(match["speaker"])
        if pred_labels:
            speaker_map = _best_label_mapping(pred_labels, true_labels)
            correct = sum(1 for p, t in zip(pred_labels, true_labels) if speaker_map.get(p) == t)
            speaker_acc = round(correct / len(pred_labels), 4)

    # turn boundary comparison
    truth_turns = _truth_turns(ground["truth"], cfg.segmenter.turn_silence_ms)
    boundary = _compare_turns(truth_turns, turns)

    durations = getattr(timed, "durations", []) or []
    audio_ms = getattr(timed, "audio_ms", []) or []
    asr_stats: dict[str, Any] = {"backend": cfg.asr.backend, "windows": len(durations)}
    if durations:
        asr_stats.update(
            {
                "p50_s": round(_percentile(durations, 50), 3),
                "p95_s": round(_percentile(durations, 95), 3),
                "max_s": round(max(durations), 3),
                "audio_p50_ms": round(_percentile(audio_ms, 50), 0),
                "rtf_p50": round(_percentile(durations, 50) / max(0.001, _percentile(audio_ms, 50) / 1000.0), 3),
                "estimated_subtitle_latency_p95_s": round(
                    _percentile(durations, 95) + cfg.asr.utterance_silence_ms / 1000.0, 2
                ),
            }
        )
    asr_stats["cer"] = cer

    note_summaries = []
    for note in notes:
        content = json.loads(note["content_json"])
        note_summaries.append(
            {
                "note_id": note["note_id"],
                "mode": note["mode"],
                "target_id": note["target_id"],
                "revision": note["source_revision"],
                "findings": len(content.get("findings", [])),
                "qa": len(content.get("qa", [])),
                "path": note["path"],
                "summary": str(content.get("summary", ""))[:120],
            }
        )

    return {
        "audio": {**audio, "source_duration_ms": ground["duration_ms"], "realtime_factor": round(ground["duration_ms"] / 1000.0 / max(0.01, record_wall_s), 1)},
        "record_wall_s": round(record_wall_s, 2),
        "runner_wall_s": round(runner_wall_s, 2),
        "transcript": {
            "segments": len(segments),
            "text_chars": len(predicted_text),
            "sample": predicted_text[:400],
        },
        "turns": {"count": len(turns), "finalized": sum(1 for t in turns if t["state"] == "finalized")},
        "talks": {
            "count": len(talks),
            "items": [
                {"talk_id": t["talk_id"], "speaker_id": t["speaker_id"], "start_ms": t["start_ms"], "end_ms": t["end_ms"]}
                for t in talks
            ],
        },
        "events": event_counts,
        "notes": {"active": len(notes), "items": note_summaries},
        "asr": asr_stats,
        "diarization": {
            "backend": cfg.diarization.backend,
            "accuracy_estimate": speaker_acc,
            "label_mapping": speaker_map,
        },
        "boundaries": boundary,
        "ground_truth": {
            "tts": ground["tts"],
            "speakers": sorted({item["speaker"] for item in ground["truth"]}),
            "voices": ground.get("speaker_voices"),
            "utterances": len(ground["truth"]),
        },
    }


def _truth_turns(truth: list[dict[str, Any]], turn_silence_ms: int) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for item in truth:
        if current is None or item["speaker"] != current["speaker"] or item["start_ms"] - current["end_ms"] > turn_silence_ms:
            if current is not None:
                turns.append(current)
            current = {"speaker": item["speaker"], "start_ms": item["start_ms"], "end_ms": item["end_ms"]}
        else:
            current["end_ms"] = item["end_ms"]
    if current is not None:
        turns.append(current)
    return turns


def _compare_turns(truth: list[dict[str, Any]], predicted: list[dict[str, Any]], tolerance_ms: int = 2500) -> dict[str, Any]:
    matched = 0
    details = []
    used: set[int] = set()
    for t in truth:
        best = None
        for i, p in enumerate(predicted):
            if i in used:
                continue
            delta_start = abs(int(p["start_ms"]) - t["start_ms"])
            delta_end = abs(int(p["end_ms"]) - t["end_ms"])
            if delta_start <= tolerance_ms and delta_end <= tolerance_ms:
                best = (i, delta_start, delta_end)
                break
        if best:
            used.add(best[0])
            matched += 1
            details.append({"truth": t, "predicted": predicted[best[0]], "ok": True})
        else:
            details.append({"truth": t, "predicted": None, "ok": False})
    precision = matched / max(1, len(predicted))
    recall = matched / max(1, len(truth))
    return {
        "ground_truth_turns": len(truth),
        "predicted_turns": len(predicted),
        "matched": matched,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "tolerance_ms": tolerance_ms,
        "details": details,
    }


def check_audio_integrity(store: Store) -> dict[str, Any]:
    from .audio import wav_info

    assert store.paths is not None
    chunks = store.chunks()
    problems: list[str] = []
    total_frames = 0
    total_ms = 0
    for chunk in chunks:
        if not chunk["path"]:
            continue
        path = store.paths.root / chunk["path"]
        if not path.exists():
            problems.append(f"missing chunk file {chunk['path']}")
            continue
        info = wav_info(path)
        if info.frames != chunk["frames"]:
            problems.append(f"frame mismatch in {chunk['path']}: file={info.frames} db={chunk['frames']}")
        total_frames += info.frames
        total_ms += info.duration_ms
    expected_ms = store.audio_stats()["duration_ms"]
    if abs(total_ms - expected_ms) > 50:
        problems.append(f"duration mismatch: files={total_ms}ms db={expected_ms}ms")
    return {
        "chunks": len(chunks),
        "total_frames": total_frames,
        "total_ms": total_ms,
        "db_duration_ms": expected_ms,
        "problems": problems,
        "ok": not problems,
    }


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def write_report(root: Path, cfg, session_id: str, ground: dict[str, Any], metrics: dict[str, Any]) -> Path:
    path = root / "selftest_report.md"
    lines: list[str] = []
    lines.append(f"# 自测报告：{session_id}")
    lines.append("")
    lines.append(f"- 生成时间（UTC）：{utc_now()}")
    lines.append(f"- 包版本：{__version__}；技能版本：{SKILL_VERSION}")
    lines.append(f"- ASR 后端：{cfg.asr.backend}（{cfg.asr.model}，device={cfg.asr.device}）")
    lines.append(f"- 说话人后端：{cfg.diarization.backend}")
    lines.append(f"- 音频：{ground['path']}（{ground['duration_ms'] / 1000:.1f}s，TTS={'是' if ground['tts'] else '否（合成音）'}）")
    if ground.get("tts_error"):
        lines.append(f"- TTS 失败原因：`{ground['tts_error']}`")
    lines.append("")
    lines.append("## 1. 音频与实时性")
    lines.append("")
    audio = metrics["audio"]
    lines.append(f"- 源音频时长：{audio['source_duration_ms'] / 1000:.1f}s；入库时长：{audio['duration_ms'] / 1000:.1f}s")
    lines.append(f"- 分块：{audio['chunks']} 个；字节：{audio['bytes']}；缺口：{audio['gap_ms']}ms；丢块：{audio['dropped_blocks']}")
    lines.append(f"- 处理速度：{audio['realtime_factor']}× 实时（录音处理 {metrics['record_wall_s']}s）")
    lines.append("")
    lines.append("## 2. 转写质量")
    lines.append("")
    asr = metrics["asr"]
    if ground["truth"]:
        lines.append(f"- 字符错误率 CER：{asr.get('cer')}")
    else:
        lines.append("- CER：未评估（使用外部音频且无标注文本）")
    lines.append(f"- 稳定片段数：{metrics['transcript']['segments']}；总字符：{metrics['transcript']['text_chars']}")
    if asr.get("windows"):
        lines.append(f"- ASR 窗口：{asr['windows']}；单窗耗时 p50={asr['p50_s']}s / p95={asr['p95_s']}s（RTF p50={asr['rtf_p50']}）")
        lines.append(f"- 估计字幕延迟 p95（= 静音判定 {cfg.asr.utterance_silence_ms}ms + ASR p95）：{asr['estimated_subtitle_latency_p95_s']}s")
    lines.append("")
    lines.append("```text")
    lines.append(metrics["transcript"]["sample"] or "(空)")
    lines.append("```")
    lines.append("")
    lines.append("## 3. 轮次与报告边界")
    lines.append("")
    b = metrics["boundaries"]
    lines.append(
        f"- 标注轮次 {b['ground_truth_turns']} 个，识别轮次 {b['predicted_turns']} 个，"
        f"命中 {b['matched']} 个（容差 {b['tolerance_ms']}ms）"
    )
    lines.append(f"- 精确率 {b['precision']}，召回率 {b['recall']}")
    lines.append(f"- 报告（talk）数：{metrics['talks']['count']}")
    lines.append("")
    lines.append("## 4. 说话人")
    lines.append("")
    d = metrics["diarization"]
    lines.append(f"- 后端：{d['backend']}")
    lines.append(f"- 标注映射：{d['label_mapping']}")
    lines.append(f"- 估计准确率（最近片段归属）：{d['accuracy_estimate']}")
    lines.append("")
    lines.append("## 5. 事件、任务与纪要")
    lines.append("")
    lines.append(f"- 事件计数：{metrics['events']}")
    lines.append(f"- 纪要：{metrics['notes']['active']} 篇")
    for item in metrics["notes"]["items"]:
        lines.append(
            f"  - {item['mode']} {item['target_id']} r{item['revision']}：结论 {item['findings']} 条，问答 {item['qa']} 组"
        )
    lines.append(
        f"- 幂等性：重复投递后新增纪要 {metrics['idempotency']['duplicates']} 篇"
        f"（{'通过' if metrics['idempotency']['ok'] else '失败'}）"
    )
    lines.append("")
    lines.append("## 6. 音频完整性")
    lines.append("")
    ai = metrics["audio_integrity"]
    lines.append(f"- 分块 {ai['chunks']} 个，重读 {ai['total_ms']}ms（{'一致' if ai['ok'] else '不一致'}）")
    if ai["problems"]:
        lines.append(f"- 问题：{ai['problems']}")
    lines.append("")
    lines.append("## 7. 结论")
    lines.append("")
    lines.append(f"- 自测总体：{'通过' if metrics.get('ok') else '未通过'}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
