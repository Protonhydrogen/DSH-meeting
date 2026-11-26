"""Loading of the portable ``scientific-conference`` skill document."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import SKILL_VERSION

__all__ = ["SkillDocument", "load_skill", "SkillError"]

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class SkillError(RuntimeError):
    pass


@dataclass
class SkillDocument:
    path: Path
    text: str
    body: str
    frontmatter: dict[str, Any]
    version: str

    def info(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "name": self.frontmatter.get("name"),
            "version": self.version,
            "chars": len(self.body),
        }


def _parse_frontmatter(block: str) -> dict[str, Any]:
    """Tiny YAML subset parser (``key: value`` pairs only).

    The skill front matter is intentionally simple, so the recorder does not
    need PyYAML at note time - and a malformed block degrades to "no front
    matter" instead of breaking note generation.
    """
    out: dict[str, Any] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip('"').strip("'")
        out[key.strip()] = value
    return out


def load_skill(path: str | Path | None = None) -> SkillDocument:
    p = Path(path) if path else Path(__file__).resolve().parents[2] / "skills" / "scientific-conference" / "SKILL.md"
    if not p.exists():
        raise SkillError(f"skill document not found: {p}")
    text = p.read_text(encoding="utf-8")
    front: dict[str, Any] = {}
    body = text
    match = _FRONTMATTER.match(text)
    if match:
        front = _parse_frontmatter(match.group(1))
        body = text[match.end() :]
    version = str(front.get("version") or SKILL_VERSION)
    return SkillDocument(path=p, text=text, body=body.strip(), frontmatter=front, version=version)
