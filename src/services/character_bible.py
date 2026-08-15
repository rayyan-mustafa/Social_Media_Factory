"""Character bible — lock recurring figure appearance across scenes.

Loads config/character_bible.json. Matches aliases in scene text / visual_prompt,
injects locked appearance strings, and tracks per-job usage for consistency.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.services.settings import CONFIG_DIR, ROOT


@dataclass(frozen=True)
class CharacterLock:
    id: str
    display_name: str
    aliases: tuple[str, ...]
    lock: str
    seed_salt: int = 0
    priority: int = 0


@dataclass
class CharacterMatch:
    characters: list[CharacterLock] = field(default_factory=list)
    inject: str = ""
    seed_offset: int = 0

    @property
    def ids(self) -> list[str]:
        return [c.id for c in self.characters]


@dataclass
class CharacterTracker:
    """Accumulates which locked characters appeared in a job."""

    style_id: str
    bible_path: str
    scenes: dict[str, list[str]] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=dict)

    def record(self, scene_index: int, character_ids: list[str]) -> None:
        if not character_ids:
            return
        key = str(scene_index)
        self.scenes[key] = list(character_ids)
        for cid in character_ids:
            self.totals[cid] = self.totals.get(cid, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "style_id": self.style_id,
            "bible_path": self.bible_path,
            "totals": dict(sorted(self.totals.items())),
            "scenes": {k: self.scenes[k] for k in sorted(self.scenes, key=int)},
        }

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def _bible_path() -> Path:
    return CONFIG_DIR / "character_bible.json"


def load_character_bible(path: Path | None = None) -> tuple[str, list[CharacterLock], list[str]]:
    path = path or _bible_path()
    if not path.exists():
        return "none", [], []
    raw = json.loads(path.read_text(encoding="utf-8"))
    style_id = str(raw.get("style_id") or "unknown")
    refs = [str(x) for x in (raw.get("reference_images") or [])]
    chars: list[CharacterLock] = []
    for item in raw.get("characters") or []:
        aliases = tuple(
            a.strip().lower()
            for a in (item.get("aliases") or [])
            if isinstance(a, str) and a.strip()
        )
        lock = (item.get("lock") or "").strip()
        cid = (item.get("id") or "").strip()
        if not cid or not lock or not aliases:
            continue
        chars.append(
            CharacterLock(
                id=cid,
                display_name=str(item.get("display_name") or cid),
                aliases=aliases,
                lock=lock,
                seed_salt=int(item.get("seed_salt") or 0),
                priority=int(item.get("priority") or 0),
            )
        )
    chars.sort(key=lambda c: (-c.priority, c.id))
    return style_id, chars, refs


def match_characters(
    *texts: str,
    bible: list[CharacterLock] | None = None,
) -> CharacterMatch:
    """Find locked characters mentioned in any of the texts."""
    if bible is None:
        _, bible, _ = load_character_bible()
    hay = " ".join(t for t in texts if t).lower()
    if not hay.strip() or not bible:
        return CharacterMatch()

    found: list[CharacterLock] = []
    for ch in bible:
        for alias in sorted(ch.aliases, key=len, reverse=True):
            # word-boundary-ish match; allow punctuation around alias
            pattern = re.compile(
                rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])",
                re.IGNORECASE,
            )
            if pattern.search(hay):
                found.append(ch)
                break

    if not found:
        return CharacterMatch()

    # Highest priority first; de-dupe by id
    seen: set[str] = set()
    ordered: list[CharacterLock] = []
    for ch in sorted(found, key=lambda c: (-c.priority, c.id)):
        if ch.id in seen:
            continue
        seen.add(ch.id)
        ordered.append(ch)

    inject = "; ".join(c.lock for c in ordered)
    seed_offset = sum(c.seed_salt for c in ordered) % 1_000_000
    return CharacterMatch(characters=ordered, inject=inject, seed_offset=seed_offset)


def resolve_ref_paths(refs: list[str]) -> list[str]:
    out: list[str] = []
    for r in refs:
        p = Path(r)
        if not p.is_absolute():
            p = ROOT / p
        if p.exists():
            out.append(str(p.resolve()))
    return out
