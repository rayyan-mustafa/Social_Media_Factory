#!/usr/bin/env python3
"""Patch thumbnail_prompt.py: derive gold text from current title (no sticky SHE SURVIVED)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src" / "services" / "thumbnail_prompt.py"
TEST = ROOT / "tests" / "test_thumbnail_prompt_gold_text.py"

NEW_HELPERS_AND_PICK = r'''
_GOLD_STOPWORDS = frozenset(
    {
        "what",
        "if",
        "had",
        "has",
        "have",
        "been",
        "was",
        "were",
        "did",
        "does",
        "done",
        "never",
        "not",
        "the",
        "a",
        "an",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "and",
        "or",
        "but",
        "with",
        "from",
        "into",
        "over",
        "under",
        "than",
        "then",
        "that",
        "this",
        "these",
        "those",
        "their",
        "his",
        "her",
        "its",
        "our",
        "your",
        "who",
        "whom",
        "whose",
        "which",
        "when",
        "where",
        "why",
        "how",
        "would",
        "could",
        "should",
        "might",
        "must",
        "shall",
        "will",
        "can",
        "may",
        "s",
    }
)


def _title_content_words(title: str, *, max_words: int = 5) -> list[str]:
    """Meaningful title tokens for gold text / keyword overlap (≤5 by quality rules)."""
    words: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9']+", title or ""):
        w = raw.strip("'").lower()
        if len(w) < 3 or w in _GOLD_STOPWORDS:
            continue
        words.append(w)
        if len(words) >= max_words * 3:
            break
    return words


def _extract_gold_phrase(gold_text_line: str) -> str:
    text = (gold_text_line or "").strip()
    if ":" in text:
        text = text.split(":", 1)[1].strip()
    text = re.sub(r"[^A-Za-z0-9\s]", " ", text)
    return " ".join(text.split()).upper()


def _gold_shares_title_keywords(
    gold_phrase: str,
    title: str,
    *,
    min_shared: int = 1,
) -> bool:
    """True when generated thumb text shares ≥1 content keyword with the job title."""
    title_words = set(_title_content_words(title, max_words=12))
    gold_words = {
        w.lower()
        for w in re.findall(r"[A-Za-z0-9']+", gold_phrase or "")
        if len(w) >= 3 and w.lower() not in _GOLD_STOPWORDS
    }
    if not title_words or not gold_words:
        return False
    shared = title_words & gold_words
    return len(shared) >= min_shared


def _shortened_title_words(title: str, *, max_words: int = 5) -> str:
    """Fallback gold text: up to 5 content words from the current title."""
    words = _title_content_words(title, max_words=max_words)
    if not words:
        return ""
    # Prefer the most distinctive trailing content (often the counterfactual).
    take = words[-max_words:] if len(words) > max_words else words
    return " ".join(take).upper()


def _format_gold_line(phrase: str) -> str:
    words = [w for w in phrase.split() if w]
    n = len(words)
    label = "1 word" if n == 1 else f"{n} words"
    return f"{label} in large bold gold serif: {' '.join(words)}"


def _phrase_from_title_or_hook(text: str) -> str | None:
    """Derive ≤5 uppercase words from a title/hook, preferring the what-if clause."""
    raw = (text or "").strip()
    if not raw:
        return None
    m = re.search(r"what if\s+(.+?)(?:\?|$)", raw, re.IGNORECASE)
    clause = m.group(1) if m else raw
    phrase = _punchy_phrase(clause, max_words=5)
    if phrase:
        return phrase
    shortened = _shortened_title_words(raw, max_words=5)
    return shortened or None


def _ensure_gold_text_matches_title(
    gold_line: str,
    *,
    title: str,
    hook: str = "",
) -> tuple[str, float, bool]:
    """
    Guard: if gold text does not share keywords with the current title,
    regenerate from title (or shortened title words).
    Returns (gold_line, confidence, replaced).
    """
    title = (title or "").strip()
    phrase = _extract_gold_phrase(gold_line)
    if title and phrase and _gold_shares_title_keywords(phrase, title):
        return gold_line, 0.0, False

    for source in (title, hook):
        derived = _phrase_from_title_or_hook(source)
        if derived and (not title or _gold_shares_title_keywords(derived, title)):
            return _format_gold_line(derived), 0.86, True

    fallback = _shortened_title_words(title, max_words=5)
    if fallback:
        return _format_gold_line(fallback), 0.70, True
    if phrase:
        return gold_line, 0.0, False
    return _format_gold_line("WHAT IF"), 0.25, True


def _rewrite_prompt_gold_text(prompt: str, gold_line: str) -> str:
    """Replace Text Placement gold line in a filled template prompt."""
    lines = prompt.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        if re.match(r"(?i)^\s*text placement\s*$", line.strip()):
            i += 1
            # Skip blank lines after header, then replace first non-empty content line.
            while i < len(lines) and not lines[i].strip():
                out.append(lines[i])
                i += 1
            if i < len(lines):
                out.append(gold_line)
                i += 1
                continue
            out.append(gold_line)
            continue
        i += 1
    text = "\n".join(out)
    if gold_line not in text:
        # Fallback: replace any prior gold-serif line.
        text2, n = re.subn(
            r"(?im)^.*\bin large bold gold serif:.*$",
            gold_line,
            prompt,
            count=1,
        )
        if n:
            return text2
    return text


def _pick_gold_text(
    script: ScriptResult,
    corpus: str,
    char_id: str | None,
) -> tuple[str, float]:
    """Gold overlay text MUST come from the current title/hook — never a sticky default."""
    del corpus, char_id  # theme tables must not override title-derived text
    title = (script.title or script.topic or "").strip()
    hook = (script.hook or "").strip()

    for source in (title, hook, script.topic or ""):
        phrase = _phrase_from_title_or_hook(source)
        if not phrase:
            continue
        # Title-sourced phrases are always allowed; hook/topic need title overlap when title exists.
        if source != title and title and not _gold_shares_title_keywords(phrase, title):
            continue
        conf = 0.88 if source == title else 0.78
        return _format_gold_line(phrase), conf

    fallback = _shortened_title_words(title, max_words=5)
    if fallback:
        return _format_gold_line(fallback), 0.70

    return _format_gold_line("WHAT IF"), 0.30


def _punchy_phrase(text: str, *, max_words: int = 5) -> str | None:
    """Reduce a clause to 2–5 uppercase content words (quality: ≤5)."""
    cleaned = re.sub(
        r"\b(" + "|".join(sorted(_GOLD_STOPWORDS, key=len, reverse=True)) + r")\b",
        " ",
        text or "",
        flags=re.I,
    )
    words = [w for w in re.findall(r"[A-Za-z]+", cleaned) if len(w) > 2]
    if not words:
        return None
    if len(words) <= max_words:
        take = words
    elif len(words) >= 4:
        take = words[-max_words:]
    else:
        take = words[-2:]
    phrase = " ".join(take[:max_words]).upper()
    if len(phrase.split()) < 2:
        return None
    return phrase
'''

# Patch build_thumbnail_prompt to enforce guard after slots / openrouter
BUILD_OLD = '''    if mode == "openrouter":
        prompt = _openrouter_thumbnail_prompt(
            script, template=template, seed_title=seed_title, settings=s
        )
        return ThumbnailPromptResult(prompt=prompt, mode="openrouter")

    slots = _derive_slots_rule_based(script, corpus)
    if mode == "rules" or (mode == "auto" and slots.is_sufficient()):
        prompt = _fill_template(template, slots)
        return ThumbnailPromptResult(prompt=prompt, mode="rules", slots=slots)

    if mode == "auto":
        try:
            prompt = _openrouter_thumbnail_prompt(
                script, template=template, seed_title=seed_title, settings=s
            )
            return ThumbnailPromptResult(prompt=prompt, mode="openrouter", slots=slots)
        except OpenRouterError:
            # Last resort: return best-effort rules output
            prompt = _fill_template(template, slots)
            return ThumbnailPromptResult(prompt=prompt, mode="rules", slots=slots)

    prompt = _fill_template(template, slots)
    return ThumbnailPromptResult(prompt=prompt, mode="rules", slots=slots)
'''

BUILD_NEW = '''    title_for_guard = (seed_title or script.title or script.topic or "").strip()

    if mode == "openrouter":
        prompt = _openrouter_thumbnail_prompt(
            script, template=template, seed_title=seed_title, settings=s
        )
        prompt = _guard_openrouter_gold_text(
            prompt, title=title_for_guard, hook=script.hook or ""
        )
        return ThumbnailPromptResult(prompt=prompt, mode="openrouter")

    slots = _derive_slots_rule_based(script, corpus)
    slots = _apply_gold_title_guard(slots, title=title_for_guard, hook=script.hook or "")

    if mode == "rules" or (mode == "auto" and slots.is_sufficient()):
        prompt = _fill_template(template, slots)
        return ThumbnailPromptResult(prompt=prompt, mode="rules", slots=slots)

    if mode == "auto":
        try:
            prompt = _openrouter_thumbnail_prompt(
                script, template=template, seed_title=seed_title, settings=s
            )
            prompt = _guard_openrouter_gold_text(
                prompt, title=title_for_guard, hook=script.hook or ""
            )
            return ThumbnailPromptResult(prompt=prompt, mode="openrouter", slots=slots)
        except OpenRouterError:
            # Last resort: return best-effort rules output
            prompt = _fill_template(template, slots)
            return ThumbnailPromptResult(prompt=prompt, mode="rules", slots=slots)

    prompt = _fill_template(template, slots)
    return ThumbnailPromptResult(prompt=prompt, mode="rules", slots=slots)
'''
APPLY_GUARD_FUNCS = r'''
def _apply_gold_title_guard(
    slots: ThumbnailSlots,
    *,
    title: str,
    hook: str = "",
) -> ThumbnailSlots:
    new_line, conf_bump, replaced = _ensure_gold_text_matches_title(
        slots.gold_text, title=title, hook=hook
    )
    if not replaced:
        return slots
    slots.gold_text = new_line
    if conf_bump:
        slots.slot_confidence["gold_text"] = max(
            float(slots.slot_confidence.get("gold_text", 0.0)),
            conf_bump,
        )
        if slots.slot_confidence:
            slots.confidence = sum(slots.slot_confidence.values()) / len(slots.slot_confidence)
    return slots


def _guard_openrouter_gold_text(prompt: str, *, title: str, hook: str = "") -> str:
    m = re.search(r"(?im)^(.*\bin large bold gold serif:.*)$", prompt)
    current = m.group(1).strip() if m else ""
    new_line, _, replaced = _ensure_gold_text_matches_title(
        current, title=title, hook=hook
    )
    if not replaced:
        return prompt
    return _rewrite_prompt_gold_text(prompt, new_line)

'''

OPENROUTER_OLD = '''Include 2–4 words of gold thumbnail text (uppercase) in the Text Placement section.

Seed title (high CTR reference): {seed_title or script.title}
'''

OPENROUTER_NEW = '''Include 2–5 words of gold thumbnail text (uppercase) in the Text Placement section.
The gold text MUST be derived from THIS video's title (or hook) — never reuse a phrase from another video.
Do NOT use generic sticky phrases like "SHE SURVIVED" unless those exact words appear in the current title.

Seed title (high CTR reference): {seed_title or script.title}
'''


def _replace_pick_block(text: str) -> str:
    start = text.index("def _pick_gold_text(")
    end = text.index("def _pick_position(")
    # Keep _punchy_phrase inside the replacement; remove old _punchy_phrase too.
    # New block already includes _pick_gold_text + helpers that must sit BEFORE it?
    # Helpers should be inserted before _pick_gold_text; punchy is part of NEW_HELPERS_AND_PICK.
    # Actually NEW_HELPERS_AND_PICK includes helpers + _pick_gold_text + _punchy_phrase.
    # So we replace from _pick_gold_text through end of old _punchy_phrase (before _pick_position).
    return text[:start] + NEW_HELPERS_AND_PICK.lstrip("\n") + "\n\n" + text[end:]


def main() -> None:
    text = TARGET.read_text(encoding="utf-8")
    if "SHE SURVIVED" not in text and "_gold_shares_title_keywords" in text:
        print("already patched")
    else:
        if "def _pick_gold_text(" not in text:
            raise SystemExit("missing _pick_gold_text")
        text = _replace_pick_block(text)
        if BUILD_OLD not in text:
            raise SystemExit("BUILD_OLD block not found")
        text = text.replace(BUILD_OLD, BUILD_NEW, 1)
        if OPENROUTER_OLD not in text:
            raise SystemExit("OPENROUTER_OLD block not found")
        text = text.replace(OPENROUTER_OLD, OPENROUTER_NEW, 1)
        # Insert guard helpers before _pick_figure? Better before _fill_template or after _derive
        anchor = "def _fill_template(template: str, slots: ThumbnailSlots) -> str:"
        if "def _apply_gold_title_guard" not in text:
            if anchor not in text:
                raise SystemExit("anchor for guard funcs missing")
            text = text.replace(anchor, APPLY_GUARD_FUNCS.lstrip("\n") + "\n" + anchor, 1)
        TARGET.write_text(text, encoding="utf-8")
        print(f"patched {TARGET}")
        # Ensure no sticky default returns remain in gold-text selection.
        pick_start = text.index("def _pick_gold_text(")
        punchy_start = text.index("def _punchy_phrase(")
        pick_region = text[pick_start:punchy_start]
        for bad in ("SHE SURVIVED", "NEVER EXECUTED", "THE PLOT FAILED", "HISTORY CHANGED"):
            if bad in pick_region:
                raise SystemExit(f"sticky phrase still in _pick_gold_text: {bad}")
        # Return-path ban: no hardcoded gold defaults except WHAT IF fallback.
        if 'serif: SHE SURVIVED"' in text or "serif: SHE SURVIVED'" in text:
            raise SystemExit("hardcoded SHE SURVIVED return still present")

    TEST.write_text(
        '''"""Gold thumbnail text must track the current job title (no sticky SHE SURVIVED)."""

from __future__ import annotations

from src.domain.models import Outline, OutlineChapter, Scene, ScriptResult, ScriptValidation
from src.services.thumbnail_prompt import (
    _extract_gold_phrase,
    _gold_shares_title_keywords,
    _pick_gold_text,
    _shortened_title_words,
    build_thumbnail_prompt,
)


def _script(title: str, *, hook: str = "", topic: str | None = None) -> ScriptResult:
    topic = topic or title
    return ScriptResult(
        topic=topic,
        title=title,
        hook=hook or f"Opening beat for {title}",
        outline=Outline(
            title=title,
            chapters=[OutlineChapter(id=1, title="c1", target_sentences=1)],
        ),
        scenes=[Scene(index=0, text="narration survived historically in archives.", visual_prompt="portrait")],
        validation=ScriptValidation(
            ok=True,
            scene_count=1,
            min_scenes=1,
            max_scenes=12,
            target_scenes=5,
            estimated_duration_s=10.0,
        ),
    )


def test_gold_text_differs_for_two_titles_and_matches_keywords():
    t1 = "What If the Mongol Empire Reached the Atlantic Coast?"
    t2 = "What If the Roman Empire Never Fell?"
    g1, _ = _pick_gold_text(_script(t1), t1.lower() + " survived spared", None)
    g2, _ = _pick_gold_text(_script(t2), t2.lower() + " survived outlived", None)
    p1 = _extract_gold_phrase(g1)
    p2 = _extract_gold_phrase(g2)
    assert p1 != p2, (p1, p2)
    assert "SHE SURVIVED" not in p1
    assert "SHE SURVIVED" not in p2
    assert _gold_shares_title_keywords(p1, t1)
    assert _gold_shares_title_keywords(p2, t2)


def test_sticky_she_survived_rejected_by_guard_fallback():
    title = "What If Anne Boleyn Became Regent?"
    # Even with survival noise in corpus, text must track title (regent), not SHE SURVIVED.
    line, conf = _pick_gold_text(_script(title), "anne boleyn survived execution spared", "anne_boleyn")
    phrase = _extract_gold_phrase(line)
    assert "SHE SURVIVED" not in phrase
    assert _gold_shares_title_keywords(phrase, title)
    assert len(phrase.split()) <= 5
    assert conf >= 0.7


def test_shortened_title_words_cap():
    words = _shortened_title_words(
        "What If the Ottoman Empire Consolidated Control of the Mediterranean Trade Routes?",
        max_words=5,
    )
    assert 1 <= len(words.split()) <= 5


def test_build_rules_mode_no_she_survived_for_unrelated_title():
    title = "What If the Tudor Dynasty Fell in 1549?"
    result = build_thumbnail_prompt(_script(title), seed_title=title, mode="rules")
    assert "SHE SURVIVED" not in result.prompt
    assert result.slots is not None
    phrase = _extract_gold_phrase(result.slots.gold_text)
    assert _gold_shares_title_keywords(phrase, title)
''',
        encoding="utf-8",
    )
    print(f"wrote {TEST}")


if __name__ == "__main__":
    main()
