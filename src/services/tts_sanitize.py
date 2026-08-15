"""Sanitize scene narration text before Kokoro TTS.

Strips director/visual language, normalizes years and Roman numerals for speech,
and rejects visual_prompt leakage into spoken text.
"""

from __future__ import annotations

import re
from typing import Callable

# --- Visual / director language (case-insensitive) ---
_VISUAL_MARKERS = (
    "final visual",
    "slow zoom",
    "camera",
    "shot should",
    "national portrait gallery painting",
    "[visual:",
    "the shot ",
    "wide shot",
    "close-up shot",
    "close up shot",
    "tight close-up",
    "cinematic shot",
    "the frame ",
    "pull back",
    "pan across",
    "fade to",
    "cut to",
)

_VISUAL_PROMPT_PREFIXES = (
    "a painterly still",
    "same recurring",
    "painterly digital",
    "painterly still of",
    "still image of",
    "still of the",
)

_STAGE_DIRECTION_RE = re.compile(
    r"\[(?:pause|music|sfx|sound|visual|cut|fade|zoom)[^\]]*\]",
    re.IGNORECASE,
)

# Years with optional thousands comma: 1,536 or 1536
_YEAR_RE = re.compile(r"\b(\d{1,3}),?(\d{3})\b")

# Royal / proper-name Roman numerals: Henry VIII, Edward VI
_ROMAN_NAME_RE = re.compile(
    r"\b([A-Z][a-z]+(?:\s+(?:the|of|de|von)\s+)?[A-Z][a-z]+|[A-Z][a-z]+)\s+"
    r"([IVXLCDM]{1,6})\b"
)

_ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = (
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)

_ROMAN_ORDINALS: dict[str, str] = {
    "I": "the First",
    "II": "the Second",
    "III": "the Third",
    "IV": "the Fourth",
    "V": "the Fifth",
    "VI": "the Sixth",
    "VII": "the Seventh",
    "VIII": "the Eighth",
    "IX": "the Ninth",
    "X": "the Tenth",
    "XI": "the Eleventh",
    "XII": "the Twelfth",
    "XIII": "the Thirteenth",
    "XIV": "the Fourteenth",
    "XV": "the Fifteenth",
    "XVI": "the Sixteenth",
    "XVII": "the Seventeenth",
    "XVIII": "the Eighteenth",
    "XIX": "the Nineteenth",
    "XX": "the Twentieth",
    "XXI": "the Twenty-First",
    "XXII": "the Twenty-Second",
    "XXIII": "the Twenty-Third",
    "XXIV": "the Twenty-Fourth",
    "XXV": "the Twenty-Fifth",
    "XXX": "the Thirtieth",
    "XL": "the Fortieth",
    "L": "the Fiftieth",
}


def _int_to_words(n: int, *, spaced: bool = False) -> str:
    """Spell 0–99. Default hyphenated (TTS years); spaced for Weird Biology VO."""
    if n < 0 or n > 99:
        return str(n)
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    if ones == 0:
        return _TENS[tens]
    sep = " " if spaced else "-"
    return f"{_TENS[tens]}{sep}{_ONES[ones]}"


def _int_to_words_full(n: int, *, spaced: bool = True) -> str:
    """Spell non-negative integers up to 999_999 for VO (spaces, not hyphens)."""
    if n < 0:
        return str(n)
    if n <= 99:
        return _int_to_words(n, spaced=spaced)
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        if rest == 0:
            return f"{_ONES[hundreds]} hundred"
        return f"{_ONES[hundreds]} hundred {_int_to_words(rest, spaced=spaced)}"
    if n < 1_000_000:
        thousands, rest = divmod(n, 1000)
        head = _int_to_words_full(thousands, spaced=spaced)
        if rest == 0:
            return f"{head} thousand"
        return f"{head} thousand {_int_to_words_full(rest, spaced=spaced)}"
    return str(n)


def _year_to_speech(year: int) -> str:
    if year < 1000 or year > 2099:
        return str(year)
    hi = year // 100
    lo = year % 100
    hi_words = _int_to_words(hi)
    if lo == 0:
        return f"{hi_words} hundred"
    if lo < 10:
        return f"{hi_words} oh {_ONES[lo]}"
    return f"{hi_words} {_int_to_words(lo)}"


# Plain integers / decimals / percents (Weird Biology VO). Years handled first.
_PLAIN_NUMBER_RE = re.compile(
    r"(?<![\w.])(\d{1,6}(?:,\d{3})*|\d+)(?:\.(\d+))?(\s*%| percent)?(?![\w.])",
    re.IGNORECASE,
)
_STANDALONE_ROMAN_RE = re.compile(r"\b([IVXLCDM]{1,6})\b")

_ROMAN_CARDINALS: dict[str, str] = {
    "I": "one",
    "II": "two",
    "III": "three",
    "IV": "four",
    "V": "five",
    "VI": "six",
    "VII": "seven",
    "VIII": "eight",
    "IX": "nine",
    "X": "ten",
    "XI": "eleven",
    "XII": "twelve",
    "XIII": "thirteen",
    "XIV": "fourteen",
    "XV": "fifteen",
    "XVI": "sixteen",
    "XVII": "seventeen",
    "XVIII": "eighteen",
    "XIX": "nineteen",
    "XX": "twenty",
    "XXI": "twenty one",
    "XXII": "twenty two",
    "XXIII": "twenty three",
    "XXIV": "twenty four",
    "XXV": "twenty five",
    "XXX": "thirty",
    "XL": "forty",
    "L": "fifty",
    "C": "one hundred",
}


def _normalize_years(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        year = int(m.group(1) + m.group(2))
        return _year_to_speech(year)

    return _YEAR_RE.sub(repl, text)


def _normalize_roman_numerals(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        roman = m.group(2).upper()
        ordinal = _ROMAN_ORDINALS.get(roman)
        if ordinal:
            return f"{name} {ordinal}"
        return m.group(0)

    return _ROMAN_NAME_RE.sub(repl, text)


def _sentence_is_visual_only(sentence: str) -> bool:
    lower = sentence.lower().strip()
    if not lower:
        return True
    if any(lower.startswith(p) for p in _VISUAL_PROMPT_PREFIXES):
        return True
    hits = sum(1 for m in _VISUAL_MARKERS if m in lower)
    if hits >= 1 and any(
        p in lower
        for p in (
            "final visual",
            "slow zoom",
            "shot should",
            "national portrait gallery",
            "[visual:",
        )
    ):
        return True
    # Strong camera/shot direction with little narrative content
    if hits >= 2:
        return True
    if lower.startswith("the final visual") or lower.startswith("a final visual"):
        return True
    return False


def _strip_visual_sentences(text: str, warnings: list[str]) -> str:
    # Split on sentence boundaries while keeping some structure
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    kept: list[str] = []
    for part in parts:
        if not part.strip():
            continue
        if _sentence_is_visual_only(part):
            warnings.append(f"dropped visual-only sentence: {part[:80]}…")
            continue
        cleaned = part
        for marker in _VISUAL_MARKERS:
            if marker in cleaned.lower():
                # Try to remove clause containing the marker
                pattern = re.compile(
                    r"[^.!?]*" + re.escape(marker) + r"[^.!?]*[.!?]?\s*",
                    re.IGNORECASE,
                )
                new = pattern.sub("", cleaned).strip()
                if new and new != cleaned:
                    warnings.append(f"stripped visual clause containing '{marker}'")
                    cleaned = new
        if cleaned.strip():
            kept.append(cleaned.strip())
    return " ".join(kept).strip()


def _reject_visual_prompt_leakage(text: str, warnings: list[str]) -> str:
    lower = text.lower().strip()
    for prefix in _VISUAL_PROMPT_PREFIXES:
        if lower.startswith(prefix):
            warnings.append(f"rejected visual_prompt leakage (starts with '{prefix}')")
            return ""
    return text


def sanitize_scene_text(text: str) -> tuple[str, list[str]]:
    """Return (cleaned text, warnings). Empty text means scene should be dropped."""
    warnings: list[str] = []
    raw = (text or "").strip()
    if not raw:
        return "", warnings

    cleaned = _STAGE_DIRECTION_RE.sub("", raw)
    cleaned = _reject_visual_prompt_leakage(cleaned, warnings)
    if not cleaned:
        return "", warnings

    cleaned = _strip_visual_sentences(cleaned, warnings)
    if not cleaned:
        return "", warnings

    cleaned = _normalize_years(cleaned)
    cleaned = _normalize_roman_numerals(cleaned)

    # Collapse whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned, warnings


def sanitize_for_tts(text: str) -> str:
    """Convenience wrapper — returns cleaned text only (may be empty)."""
    cleaned, _ = sanitize_scene_text(text)
    return cleaned


def _fraction_digits_to_words(frac: str) -> str:
    return " ".join(_ONES[int(ch)] for ch in frac if ch.isdigit())


def _normalize_plain_numbers(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        whole_raw = m.group(1).replace(",", "")
        frac = m.group(2)
        pct = m.group(3)
        try:
            whole = int(whole_raw)
        except ValueError:
            return m.group(0)
        # Leave 4-digit years to _normalize_years (already run when used via
        # spell_numbers_for_vo); if still present, spell as full number.
        spoken = _int_to_words_full(whole, spaced=True)
        if frac:
            spoken = f"{spoken} point {_fraction_digits_to_words(frac)}"
        if pct:
            spoken = f"{spoken} percent"
        return spoken

    return _PLAIN_NUMBER_RE.sub(repl, text)


def _normalize_standalone_romans(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        roman = m.group(1).upper()
        # Skip ambiguous single "I" mid-sentence (pronoun) — only map known cards
        if roman == "I":
            return m.group(0)
        return _ROMAN_CARDINALS.get(roman, m.group(0))

    return _STANDALONE_ROMAN_RE.sub(repl, text)


def spell_numbers_for_vo(text: str) -> str:
    """Spell years, plain integers/percents, and Roman numerals for VO scripts.

    Uses spaced compound numbers (twenty six) per Weird Human Biology rules.
    Romans become cardinals (IV → four), not royal ordinals.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    cleaned = _normalize_years(raw)
    cleaned = _normalize_roman_cardinals_named(cleaned)
    cleaned = _normalize_plain_numbers(cleaned)
    cleaned = _normalize_standalone_romans(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _normalize_roman_cardinals_named(text: str) -> str:
    """Henry VIII / Type IV → Henry eight / Type four (cardinals for VO)."""

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        roman = m.group(2).upper()
        card = _ROMAN_CARDINALS.get(roman)
        if card:
            return f"{name} {card}"
        return m.group(0)

    return _ROMAN_NAME_RE.sub(repl, text)
