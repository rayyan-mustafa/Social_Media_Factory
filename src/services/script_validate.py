"""Post-expand validation for spoken beats (retention / TTS safety)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from src.domain.models import ExpandedSentence

_VISUAL_MARKERS = (
    "final visual",
    "slow zoom",
    "camera",
    "shot should",
    "painterly still",
    "wide shot",
    "close-up shot",
    "national portrait gallery",
)

_YEAR_RE = re.compile(r"\b1[0-9]{3}\b|\b2[0-9]{3}\b")
_ROMAN_RE = re.compile(
    r"\b(Henry|Edward|Philip|Louis|Charles|Richard|Mary|Elizabeth)\s+[IVXLC]+\b",
    re.IGNORECASE,
)


@dataclass
class ChapterValidation:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_chapter_sentences(
    sentences: list[ExpandedSentence],
    *,
    chapter_id: int,
    prior_texts: list[str] | None = None,
    hook_chapter: bool = False,
) -> ChapterValidation:
    errors: list[str] = []
    warnings: list[str] = []
    prior = prior_texts or []

    for i, sent in enumerate(sentences):
        text = (sent.text or "").strip()
        if not text:
            errors.append(f"beat {i}: empty text")
            continue
        lower = text.lower()
        for marker in _VISUAL_MARKERS:
            if marker in lower:
                errors.append(f"beat {i}: visual marker '{marker}' in spoken text")
        if _YEAR_RE.search(text):
            warnings.append(f"beat {i}: raw year digits in text (sanitize at TTS)")
        if _ROMAN_RE.search(text):
            warnings.append(f"beat {i}: Roman numeral in text (sanitize at TTS)")

        for prior_text in prior[-40:]:
            ratio = SequenceMatcher(
                None,
                _norm(text),
                _norm(prior_text),
            ).ratio()
            if ratio > 0.70:
                errors.append(
                    f"beat {i}: >70% similar to prior beat (retention loop risk)"
                )
                break

    if hook_chapter and sentences:
        words = [len(s.text.split()) for s in sentences if s.text.strip()]
        if words:
            med = sorted(words)[len(words) // 2]
            if med > 14:
                warnings.append(
                    f"hook chapter median words={med}; prefer ≤14 for ~3s gallery beats"
                )

    # Treat warnings on digits as non-blocking; visual markers block
    ok = not errors
    return ChapterValidation(ok=ok, errors=errors, warnings=warnings)


def validation_retry_hint(errors: list[str]) -> str:
    if not errors:
        return ""
    return (
        "\n\nCORRECTION (retention validation failed):\n"
        + "\n".join(f"- {e}" for e in errors[:8])
        + "\nRewrite ONLY the spoken text fields. Never put camera/visual directions in text. "
        "Advance the timeline — do not repeat prior beats. "
        "Write years as English words (fifteen thirty-six). "
        "Write royal numerals as words (Henry the Eighth)."
    )


def _norm(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()
