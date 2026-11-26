#!/usr/bin/env python
"""Fetch offline neural machine-translation models (Argos / CTranslate2).

The models are plain CTranslate2 packages shipped as ``.argosmodel`` archives
(a zip with ``model/`` plus a SentencePiece model).  They run fully offline on
CPU through the CTranslate2 already installed for this project - no PyTorch, no
network at meeting time.

Everything is stored under
the configured external data directory
(never inside the project directory, per AGENTS.md).

Usage
-----
    python scripts/fetch_translation_models.py --list
    python scripts/fetch_translation_models.py --pair zh-en --pair en-zh
    python scripts/fetch_translation_models.py --pair zh-en --check
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

from scientific_conference.config import default_install_root

INSTALL_ROOT = default_install_root()
MODELS_ROOT = Path(
    os.environ.get("SC_NMT_DIR", INSTALL_ROOT / "models" / "scientific_conference" / "nmt")
)
INDEX_URL = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"


def load_index() -> list[dict]:
    with urllib.request.urlopen(INDEX_URL, timeout=60) as response:
        return json.load(response)


def find_package(index: list[dict], src: str, tgt: str) -> dict:
    for entry in index:
        if entry.get("from_code") == src and entry.get("to_code") == tgt:
            return entry
    raise SystemExit(f"no package for {src}->{tgt} in the Argos index")


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  GET {url}")
    with urllib.request.urlopen(url, timeout=300) as response, tmp.open("wb") as fh:
        total = 0
        while True:
            block = response.read(1 << 20)
            if not block:
                break
            fh.write(block)
            total += len(block)
            print(f"\r  {total / 1e6:6.1f} MB", end="", flush=True)
    print()
    os.replace(tmp, dest)


def extract(archive: Path, target: Path) -> list[str]:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(target)
    return sorted(str(p.relative_to(target)) for p in target.rglob("*") if p.is_file())


def describe(target: Path) -> dict:
    """Summarise what a backend needs: the CT2 model dir and the SPM model(s)."""
    model_dir = target / "model"
    spm = sorted(
        [
            p
            for p in target.rglob("*")
            if p.is_file() and (p.suffix == ".model" or p.suffix == ".spm" or "sentencepiece" in p.name.lower())
        ]
        and [
            p
            for p in target.rglob("*")
            if p.is_file() and (p.suffix == ".model" or p.suffix == ".spm" or "sentencepiece" in p.name.lower())
        ],
        key=lambda p: p.name,
    )
    return {
        "root": str(target),
        "model_dir": str(model_dir) if model_dir.is_dir() else None,
        "has_model_bin": (model_dir / "model.bin").is_file() if model_dir.is_dir() else False,
        "spm": [str(p) for p in spm],
        "ready": model_dir.is_dir() and (model_dir / "model.bin").is_file() and bool(spm),
    }


def fetch_pair(src: str, tgt: str, *, force: bool = False, keep_archive: bool = False) -> dict:
    index = load_index()
    entry = find_package(index, src, tgt)
    target = MODELS_ROOT / f"{src}-{tgt}"
    info = describe(target)
    if info["ready"] and not force:
        print(f"[{src}->{tgt}] already present: {target}")
        return info

    url = entry["links"][0]
    archive = MODELS_ROOT / f"{entry['code']}-{entry['package_version']}.argosmodel"
    download(url, archive)
    files = extract(archive, target)
    print(f"[{src}->{tgt}] extracted {len(files)} files into {target}")
    if not keep_archive:
        archive.unlink(missing_ok=True)
    info = describe(target)
    info["files"] = files[:12]
    if not info["ready"]:
        print(f"[{src}->{tgt}] WARNING: layout not recognised; expected model/model.bin + *.spm")
    return info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fetch offline NMT models")
    parser.add_argument("--pair", action="append", default=[], metavar="SRC-TGT",
                        help="language pair, e.g. zh-en (repeatable)")
    parser.add_argument("--list", action="store_true", help="list available pairs")
    parser.add_argument("--check", action="store_true", help="only report what is installed")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--keep-archive", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.list:
        for entry in load_index():
            print(f"{entry['from_code']:>3} -> {entry['to_code']:<3}  {entry['from_name']} → {entry['to_name']}")
        return 0

    pairs = args.pair or ["zh-en", "en-zh"]
    results = {}
    if args.check:
        for pair in pairs:
            src, _, tgt = pair.partition("-")
            target = MODELS_ROOT / f"{src}-{tgt}"
            results[pair] = describe(target)
    else:
        MODELS_ROOT.mkdir(parents=True, exist_ok=True)
        for pair in pairs:
            src, _, tgt = pair.partition("-")
            if not src or not tgt:
                raise SystemExit(f"invalid pair {pair!r}; expected SRC-TGT")
            results[pair] = fetch_pair(src, tgt, force=args.force, keep_archive=args.keep_archive)

    if args.json:
        print(json.dumps({"models_root": str(MODELS_ROOT), "pairs": results}, ensure_ascii=False, indent=2))
    else:
        print(f"\nmodels root: {MODELS_ROOT}")
        for pair, info in results.items():
            mark = "OK " if info["ready"] else "-- "
            print(f"  {mark}{pair:8} {info['model_dir'] or ''}")
    return 0 if all(info["ready"] for info in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
