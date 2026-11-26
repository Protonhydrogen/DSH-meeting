#!/usr/bin/env python
"""Fetch the local models used by the recorder.

Everything lands under the configured external data directory
(or ``%SC_INSTALL_ROOT%``); nothing is written inside the project directory.

Usage
-----
    python scripts/fetch_models.py                # VAD model (small) only
    python scripts/fetch_models.py --whisper small
    python scripts/fetch_models.py --check        # report what is present
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

from scientific_conference.config import default_install_root

INSTALL_ROOT = default_install_root()
MODELS_DIR = Path(os.environ.get("SC_MODELS_DIR", INSTALL_ROOT / "models" / "scientific_conference"))
CACHE_DIR = INSTALL_ROOT / "caches" / "scientific_conference"
HF_HOME = INSTALL_ROOT / "cache" / "huggingface"

SILERO_URLS = (
    "https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx",
    "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx",
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  GET {url}")
    with urllib.request.urlopen(url, timeout=120) as resp, tmp.open("wb") as fh:
        while True:
            block = resp.read(1 << 20)
            if not block:
                break
            fh.write(block)
    os.replace(tmp, dest)


def fetch_vad(force: bool = False) -> Path:
    target = MODELS_DIR / "silero_vad.onnx"
    if target.exists() and not force:
        print(f"[vad] already present: {target} ({target.stat().st_size} bytes)")
        return target
    errors = []
    for url in SILERO_URLS:
        try:
            download(url, target)
            size = target.stat().st_size
            if size < 100_000:
                raise RuntimeError(f"suspiciously small model file ({size} bytes)")
            print(f"[vad] saved {target} ({size} bytes, sha256={sha256(target)[:16]}...)")
            return target
        except Exception as exc:  # try the next mirror
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
            print(f"  ! failed: {errors[-1]}")
    raise SystemExit("[vad] could not download the Silero VAD model:\n  " + "\n  ".join(errors))


def fetch_whisper(model: str, force: bool = False) -> Path:
    os.environ.setdefault("HF_HOME", str(HF_HOME))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from huggingface_hub import snapshot_download
    from faster_whisper.utils import _MODELS

    print(f"[asr] resolving faster-whisper model {model!r} into {HF_HOME}")
    path = snapshot_download(
        repo_id=_MODELS.get(model, f"Systran/faster-whisper-{model}"),
        cache_dir=str(HF_HOME),
        allow_patterns=["*.json", "*.txt", "*.bin", "*.model"],
    )
    print(f"[asr] ready: {path}")
    return Path(path)


def check() -> int:
    ok = True
    vad = MODELS_DIR / "silero_vad.onnx"
    print(f"models dir : {MODELS_DIR}")
    print(f"hf home    : {HF_HOME}")
    if vad.exists():
        print(f"vad        : OK  {vad} ({vad.stat().st_size} bytes)")
    else:
        print("vad        : MISSING (run fetch_models.py)")
        ok = False
    snapshots = HF_HOME / "models--Systran--faster-whisper-small" / "snapshots"
    if snapshots.exists() and any(snapshots.iterdir()):
        print(f"whisper    : OK  {snapshots}")
    else:
        print("whisper    : MISSING (run fetch_models.py --whisper small)")
        ok = False
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="fetch local models for scientific-conference")
    ap.add_argument("--whisper", metavar="MODEL", default=None, help="also fetch a faster-whisper model, e.g. small")
    ap.add_argument("--force", action="store_true", help="re-download even if present")
    ap.add_argument("--check", action="store_true", help="only report what is installed")
    args = ap.parse_args(argv)

    if args.check:
        return check()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fetch_vad(force=args.force)
    if args.whisper:
        fetch_whisper(args.whisper, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
