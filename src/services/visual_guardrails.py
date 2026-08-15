"""Editable visual guardrails for masterpiece historical cinematic paintings.

Loads:
- config/style_prompt.txt        → positive style lock
- config/visual_guardrails.txt   → negative / strip / append / boost / rewrite rules

Produces a sanitized positive prompt + negative prompt for ComfyUI.
Blocks silhouette/backlit-void compositions and boosts readable faces/costumes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from src.services.settings import CONFIG_DIR, load_style_hint

_COMMENT = re.compile(r"^\s*#")
_SECTION = re.compile(r"^\[([A-Z_]+)\]\s*$")
_REWRITE_LINE = re.compile(r"^(.+?)\s*=>\s*(.+)$")

# Heuristic: scene likely shows a face → inject early eye/face detail.
_FACE_HINT = re.compile(
    r"\b("
    r"face|faces|portrait|facial|woman|women|man|men|girl|boy|"
    r"person|people|lady|gentleman|queen|king|princess|prince|"
    r"character|figure|gaze|looking|staring|expression|eye|eyes|"
    r"soldier|soldiers|rider|riders|cavalry|army|noble|nobles|"
    r"assassin|crowd|officer|knight|knights|guard|guards|"
    r"richard|henry|tudor|york|boleyn|anne|cardinal|cleric"
    r")\b",
    re.IGNORECASE,
)

# Heuristic: scene mentions hands/fingers → inject hand-safety boost.
_HAND_HINT = re.compile(
    r"\b("
    r"hand|hands|finger|fingers|fist|palm|grasp|grasping|"
    r"holding|holds|held|clutching|reaching"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GuardrailBundle:
    style_lock: str
    negative: str
    strip_phrases: tuple[str, ...]
    append_rules: str
    face_boost: str
    hand_boost: str
    rewrites: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class GuardedPrompts:
    positive: str
    negative: str
    stripped: tuple[str, ...]
    original: str
    face_boost_applied: bool = False
    hand_boost_applied: bool = False
    rewritten: tuple[tuple[str, str], ...] = ()


def _non_comment_lines(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        if _COMMENT.match(line):
            continue
        out.append(line)
    return out


def load_guardrails(path: Path | None = None) -> GuardrailBundle:
    path = path or (CONFIG_DIR / "visual_guardrails.txt")
    sections: dict[str, list[str]] = {
        "NEGATIVE": [],
        "STRIP": [],
        "APPEND_RULES": [],
        "FACE_BOOST": [],
        "HAND_BOOST": [],
        "REWRITE": [],
    }
    current: str | None = None
    if path.exists():
        for raw in _non_comment_lines(path.read_text(encoding="utf-8")):
            m = _SECTION.match(raw.strip())
            if m:
                current = m.group(1)
                if current not in sections:
                    sections[current] = []
                continue
            if current is None:
                continue
            line = raw.strip().rstrip(",")
            if line:
                sections[current].append(line)

    def join_section(name: str) -> str:
        parts: list[str] = []
        for chunk in sections.get(name, []):
            parts.extend(p.strip() for p in chunk.split(",") if p.strip())
        # de-dupe preserve order
        seen: set[str] = set()
        ordered: list[str] = []
        for p in parts:
            key = p.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(p)
        return ", ".join(ordered)

    strip_raw = sections.get("STRIP", [])
    strip_phrases: list[str] = []
    for chunk in strip_raw:
        for p in chunk.split(","):
            p = p.strip()
            if p:
                strip_phrases.append(p)

    rewrites: list[tuple[str, str]] = []
    for chunk in sections.get("REWRITE", []):
        m = _REWRITE_LINE.match(chunk.strip())
        if m:
            old, new = m.group(1).strip(), m.group(2).strip()
            if old and new:
                rewrites.append((old, new))

    return GuardrailBundle(
        style_lock=load_style_hint(),
        negative=join_section("NEGATIVE"),
        strip_phrases=tuple(strip_phrases),
        append_rules=join_section("APPEND_RULES"),
        face_boost=join_section("FACE_BOOST"),
        hand_boost=join_section("HAND_BOOST"),
        rewrites=tuple(rewrites),
    )


def looks_like_face_scene(prompt: str) -> bool:
    """True when the scene prompt likely depicts a visible face/person."""
    return bool(_FACE_HINT.search(prompt or ""))


def looks_like_hand_scene(prompt: str) -> bool:
    """True when the scene prompt mentions hands / holding / fingers."""
    return bool(_HAND_HINT.search(prompt or ""))


def apply_guardrails(visual_prompt: str, *, bundle: GuardrailBundle | None = None) -> GuardedPrompts:
    """Sanitize a scene visual_prompt into (positive, negative) for image gen."""
    original = (visual_prompt or "").strip()
    if not original:
        raise ValueError("empty visual_prompt")

    g = bundle or load_guardrails()
    cleaned = original
    stripped: list[str] = []
    rewritten: list[tuple[str, str]] = []

    # Dangerous phrases → safer alternatives (before strip)
    for old, new in sorted(g.rewrites, key=lambda pair: len(pair[0]), reverse=True):
        pattern = re.compile(re.escape(old), re.IGNORECASE)
        if pattern.search(cleaned):
            cleaned = pattern.sub(new, cleaned)
            rewritten.append((old, new))

    # Longest phrases first so "extra fingers" beats "extra finger"
    for phrase in sorted(g.strip_phrases, key=len, reverse=True):
        pattern = re.compile(re.escape(phrase), re.IGNORECASE)
        if pattern.search(cleaned):
            stripped.append(phrase)
            cleaned = pattern.sub(" ", cleaned)

    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+,", ",", cleaned)
    cleaned = re.sub(r",\s*,+", ", ", cleaned)
    cleaned = re.sub(r"\bwith and\b", "with", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\band s\b", "and", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bwith holding\b", "holding", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" ,")

    # Early token boosts (SD pays more attention near the start).
    face_boost_applied = False
    hand_boost_applied = False
    head = cleaned

    if g.face_boost and looks_like_face_scene(original):
        already = "micro-expression" in cleaned.lower() or "furrowed brow" in cleaned.lower()
        if not already:
            head = f"{head}, {g.face_boost}"
            face_boost_applied = True

    if g.hand_boost and looks_like_hand_scene(original):
        already = "period-accurate hands" in head.lower() or "signet ring" in head.lower()
        if not already:
            head = f"{head}, {g.hand_boost}"
            hand_boost_applied = True

    # Style lock FIRST — Flux attends more to leading tokens (keeps illustrated look).
    parts: list[str] = []
    if g.style_lock:
        parts.append(g.style_lock)
    parts.append(head)
    if g.append_rules and g.append_rules.lower() not in head.lower():
        parts.append(g.append_rules)

    positive = ", ".join(p for p in parts if p)
    return GuardedPrompts(
        positive=positive,
        negative=g.negative,
        stripped=tuple(stripped),
        original=original,
        face_boost_applied=face_boost_applied,
        hand_boost_applied=hand_boost_applied,
        rewritten=tuple(rewritten),
    )
