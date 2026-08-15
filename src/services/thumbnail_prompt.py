"""Thumbnail prompt generation — rule-based template fill with OpenRouter fallback."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from src.domain.models import ScriptResult
from src.services.character_bible import CharacterLock, load_character_bible, match_characters
from src.services.openrouter import OpenRouterClient, OpenRouterError
from src.services.settings import PROMPTS_DIR, ROOT, Settings, get_settings

ThumbnailPromptMode = Literal["rules", "openrouter", "auto"]
THUMBNAIL_TEMPLATE_NAME = "thumbnail_template.txt"

_PLACEHOLDER = re.compile(r"\[([^\]]+)\]")

# Generic fallbacks — low-confidence signals when rules cannot specialize.
_GENERIC_FIGURE = "a notable historical figure"
_GENERIC_EXPRESSION = "intense and commanding"
_GENERIC_BACKGROUND = "dim historical chamber with dramatic shadows"
_GENERIC_MYSTERY = "aged parchment with a broken wax seal"

# Era / keyword tables
_ERA_BACKGROUNDS: list[tuple[tuple[str, ...], str]] = [
    (("tudor", "henry", "anne boleyn", "elizabeth", "english reformation"), "candlelit Tudor palace chamber"),
    (("roman", "caesar", "empire", "legion"), "marble forum at dusk with torchlight"),
    (("medieval", "castle", "knight", "crusade"), "torchlit medieval castle hall"),
    (("napoleon", "revolution", "paris", "18th century"), "candlelit French salon with gilt mirrors"),
    (("viking", "norse", "longship"), "misty fjord hall with firelight"),
    (("egypt", "pharaoh", "nile"), "torchlit stone temple chamber"),
]

_EXPRESSION_RULES: list[tuple[tuple[str, ...], str]] = [
    (("surviv", "outliv", "never execut", "escaped", "spared"), "defiant and haunted"),
    (("execut", "behead", "died", "death", "fallen"), "tragic and defiant"),
    (("poison", "plot", "betray", "conspir", "assassin"), "wary and haunted"),
    (("war", "battle", "siege", "invasion"), "fierce and resolute"),
    (("what if", "alternate", "counterfactual"), "defiant and haunted"),
]

_MYSTERY_RULES: list[tuple[tuple[str, ...], str]] = [
    (("execut", "warrant", "behead", "tower"), "sealed / cracked wax EXECUTION WARRANT"),
    (("poison", "toxin", "fever", "physician"), "sealed vial beside a physician's hidden journal"),
    (("letter", "document", "journal", "diary"), "sealed royal letter with a broken wax seal"),
    (("crown", "succession", "heir", "throne"), "golden crown resting on a blood-stained cushion"),
    (("sword", "blade", "war", "battle"), "blood-stained sword half-hidden in shadow"),
    (("marriage", "wedding", "betrothal"), "torn marriage contract with a wax seal"),
]

_FIGURE_DESCRIPTORS: dict[str, str] = {
    "anne_boleyn": "Tudor queen, French hood, pearl headdress, dark elegant gown",
    "henry_viii": "Tudor king, short beard, jeweled flat cap, forest-green velvet doublet",
    "cromwell": "Tudor statesman, dark practical doublet, calculating gaze",
    "cardinal_cleric": "senior cleric, red zucchetto, black cassock with red piping",
    "tudor_guard": "Tudor royal guard in polished armor",
    "tudor_noble": "Tudor noble in rich velvet and gold embroidery",
}


class ThumbnailPromptError(RuntimeError):
    pass


@dataclass
class ThumbnailSlots:
    historical_figure: str
    figure_descriptor: str
    expression: str
    position: str  # LEFT | RIGHT
    text_side: str  # RIGHT | LEFT
    background: str
    mystery_object: str
    gold_text: str  # full Text Placement line
    confidence: float = 0.0
    matched_character_id: str | None = None
    slot_confidence: dict[str, float] = field(default_factory=dict)

    def is_sufficient(self, *, min_confidence: float = 0.62) -> bool:
        if self.confidence < min_confidence:
            return False
        if self.historical_figure.lower() in {
            _GENERIC_FIGURE.lower(),
            "historical figure",
            "unknown figure",
        }:
            return False
        generic_hits = sum(
            1
            for val in (
                self.expression,
                self.background,
                self.mystery_object,
            )
            if val.lower()
            in {
                _GENERIC_EXPRESSION.lower(),
                _GENERIC_BACKGROUND.lower(),
                _GENERIC_MYSTERY.lower(),
            }
        )
        return generic_hits <= 1


@dataclass
class ThumbnailPromptResult:
    prompt: str
    mode: str  # rules | openrouter
    slots: ThumbnailSlots | None = None


def resolve_thumbnail_template_path(
    *,
    channel: str | None = None,
    template_path: Path | str | None = None,
) -> Path:
    """Resolve thumbnail template: explicit path → channel prompts_dir → default.

    Only channels with ``sheet_channels[].prompts_dir`` (e.g. napping_historian)
    get a channel overlay. Napstorian / unset always use
    ``config/prompts/thumbnail_template.txt`` — never a retention-profile
    prompts_dir that lacks a thumbnail template.
    """
    if template_path is not None:
        return Path(template_path)
    ch = (channel or os.getenv("SCRIPT_CHANNEL") or "").strip()
    if ch:
        try:
            from src.agents.sheet_channels import channel_prompts_dir

            override = channel_prompts_dir(ch)
        except Exception:  # noqa: BLE001
            override = None
        if override:
            rel = str(override).strip()
            p = Path(rel)
            if not p.is_absolute():
                p = ROOT / p
            cand = p / THUMBNAIL_TEMPLATE_NAME
            if cand.exists():
                return cand
    path = PROMPTS_DIR / THUMBNAIL_TEMPLATE_NAME
    if path.exists():
        return path
    raise ThumbnailPromptError(f"Thumbnail template missing: {path}")


def build_thumbnail_prompt(
    script: ScriptResult,
    *,
    seed_title: str | None = None,
    mode: ThumbnailPromptMode = "auto",
    settings: Settings | None = None,
    template_path: Path | None = None,
    channel: str | None = None,
) -> ThumbnailPromptResult:
    """Build a filled thumbnail prompt from script data (rules first, OpenRouter fallback)."""
    s = settings or get_settings()
    resolved = resolve_thumbnail_template_path(
        channel=channel, template_path=template_path
    )
    template = _load_template(resolved)
    corpus = _script_corpus(script, seed_title=seed_title)
    ch = (channel or os.getenv("SCRIPT_CHANNEL") or "").strip() or None

    title_for_guard = (seed_title or script.title or script.topic or "").strip()

    if mode == "openrouter":
        prompt = _openrouter_thumbnail_prompt(
            script,
            template=template,
            seed_title=seed_title,
            settings=s,
            channel=ch,
        )
        prompt = _guard_openrouter_gold_text(
            prompt, title=title_for_guard, hook=script.hook or ""
        )
        prompt = _append_thumb_prompt_addendum(prompt, channel=ch)
        return ThumbnailPromptResult(prompt=prompt, mode="openrouter")

    slots = _derive_slots_rule_based(script, corpus, channel=ch)
    slots = _apply_gold_title_guard(slots, title=title_for_guard, hook=script.hook or "")

    if mode == "rules" or (mode == "auto" and slots.is_sufficient()):
        prompt = _fill_template(template, slots)
        prompt = _append_thumb_prompt_addendum(prompt, channel=ch)
        return ThumbnailPromptResult(prompt=prompt, mode="rules", slots=slots)

    if mode == "auto":
        try:
            prompt = _openrouter_thumbnail_prompt(
                script,
                template=template,
                seed_title=seed_title,
                settings=s,
                channel=ch,
            )
            prompt = _guard_openrouter_gold_text(
                prompt, title=title_for_guard, hook=script.hook or ""
            )
            prompt = _append_thumb_prompt_addendum(prompt, channel=ch)
            return ThumbnailPromptResult(prompt=prompt, mode="openrouter", slots=slots)
        except OpenRouterError:
            # Last resort: return best-effort rules output
            prompt = _fill_template(template, slots)
            prompt = _append_thumb_prompt_addendum(prompt, channel=ch)
            return ThumbnailPromptResult(prompt=prompt, mode="rules", slots=slots)

    prompt = _fill_template(template, slots)
    prompt = _append_thumb_prompt_addendum(prompt, channel=ch)
    return ThumbnailPromptResult(prompt=prompt, mode="rules", slots=slots)


def _append_thumb_prompt_addendum(prompt: str, *, channel: str | None) -> str:
    """Append runtime SMM thumb_prompt_addendum from channel_editing_overrides."""
    ch = (channel or "").strip()
    if not ch:
        return prompt
    try:
        from src.services.editing_overrides import get_merged_channel_editing

        addendum = (get_merged_channel_editing(ch).get("thumb_prompt_addendum") or "").strip()
    except Exception:  # noqa: BLE001
        return prompt
    if not addendum:
        return prompt
    # Template may already contain the same SMM redesign block — avoid duplication.
    if "SMM competitor-vision redesign" in prompt and addendum.split("\n", 1)[0] in prompt:
        return prompt
    return prompt.rstrip() + "\n\n" + addendum + "\n"


def write_thumbnail_prompt(
    script: ScriptResult,
    out_path: Path | str,
    *,
    seed_title: str | None = None,
    mode: ThumbnailPromptMode = "auto",
    settings: Settings | None = None,
    channel: str | None = None,
    template_path: Path | None = None,
) -> ThumbnailPromptResult:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result = build_thumbnail_prompt(
        script,
        seed_title=seed_title,
        mode=mode,
        settings=settings,
        channel=channel,
        template_path=template_path,
    )
    out_path.write_text(result.prompt.strip() + "\n", encoding="utf-8")
    return result


def _load_template(template_path: Path | None) -> str:
    path = resolve_thumbnail_template_path(template_path=template_path)
    return path.read_text(encoding="utf-8").strip()


def _script_corpus(script: ScriptResult, *, seed_title: str | None) -> str:
    parts = [
        script.topic,
        script.title,
        seed_title or "",
        script.hook,
        script.outline.title,
        script.outline.hook,
        script.outline.closer,
    ]
    for ch in script.outline.chapters:
        parts.append(ch.title)
        parts.append(ch.goal)
        parts.extend(ch.key_points)
    if script.scenes:
        parts.append(script.scenes[0].text)
        parts.append(script.scenes[0].visual_prompt)
    return " ".join(p for p in parts if p).lower()


def _derive_slots_rule_based(
    script: ScriptResult,
    corpus: str,
    *,
    channel: str | None = None,
) -> ThumbnailSlots:
    bible_path = Path(get_settings().character_bible_path)
    if not bible_path.is_absolute():
        bible_path = ROOT / bible_path

    _, bible, _ = load_character_bible(bible_path)
    match = match_characters(corpus, bible=bible)

    figure, figure_desc, figure_conf, char_id = _pick_figure(script, corpus, match.characters)
    expression, expr_conf = _pick_expression(corpus)
    background, bg_conf = _pick_background(corpus)
    mystery, mystery_conf = _pick_mystery_object(corpus, script)
    # Historian Concept 1: keep hero object document-shaped when rules pick a weapon/crown.
    if (channel or "").strip().lower() == "napping_historian":
        doc_tokens = ("parchment", "letter", "warrant", "seal", "journal", "contract", "document", "missive")
        if not any(t in mystery.lower() for t in doc_tokens):
            mystery = (
                "aged blood-stained parchment with a cracked royal wax seal "
                "(dried archival stain, ad-safe)"
            )
            mystery_conf = max(mystery_conf, 0.82)
    gold_text, gold_conf = _pick_gold_text(script, corpus, char_id)
    position, text_side = _pick_position(script.topic or script.title)

    slot_conf = {
        "figure": figure_conf,
        "expression": expr_conf,
        "background": bg_conf,
        "mystery_object": mystery_conf,
        "gold_text": gold_conf,
    }
    confidence = sum(slot_conf.values()) / len(slot_conf)

    return ThumbnailSlots(
        historical_figure=figure,
        figure_descriptor=figure_desc,
        expression=expression,
        position=position,
        text_side=text_side,
        background=background,
        mystery_object=mystery,
        gold_text=gold_text,
        confidence=confidence,
        matched_character_id=char_id,
        slot_confidence=slot_conf,
    )


def _pick_figure(
    script: ScriptResult,
    corpus: str,
    characters: list[CharacterLock],
) -> tuple[str, str, float, str | None]:
    if characters:
        title_topic = f"{script.title} {script.topic}".lower()
        scored: list[tuple[int, int, CharacterLock]] = []
        for ch in characters:
            mention = 0
            earliest = 9999
            for alias in ch.aliases:
                pos = title_topic.find(alias)
                if pos >= 0:
                    mention += len(alias)
                    earliest = min(earliest, pos)
            scored.append((mention * 100 - earliest, ch.priority, ch))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        primary = scored[0][2]
        desc = _FIGURE_DESCRIPTORS.get(
            primary.id,
            _short_descriptor(primary.lock),
        )
        conf = 0.92 if scored[0][0] > 0 else 0.80
        return primary.display_name, desc, conf, primary.id

    # Title/topic patterns: "What If Anne Boleyn Outlived..."
    for text in (script.title, script.topic):
        m = re.search(
            r"what if\s+(.+?)(?:\s+(?:had|never|outliv|surviv|did|was|were)\b|$)",
            text,
            re.IGNORECASE,
        )
        if m:
            name = _title_case_name(m.group(1).strip(" ?."))
            if len(name.split()) <= 5:
                return name, _descriptor_for_name(name, corpus), 0.78, None

    # Proper-name scan (two+ capitalized words)
    for text in (script.title, script.topic, script.hook):
        m = re.search(
            r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b",
            text or "",
        )
        if m:
            name = m.group(1).strip()
            if name.lower() not in {"what if", "the hook"}:
                return name, _descriptor_for_name(name, corpus), 0.68, None

    return _GENERIC_FIGURE, "historically accurate attire", 0.25, None


def _short_descriptor(lock: str) -> str:
    """First clause of character lock for thumbnail descriptor."""
    clause = lock.split(",")[0].strip()
    if clause.lower().startswith("same recurring "):
        clause = clause[len("same recurring ") :]
    return clause[:120]


def _descriptor_for_name(name: str, corpus: str) -> str:
    lower = name.lower()
    if "boleyn" in lower or "anne" in lower:
        return _FIGURE_DESCRIPTORS["anne_boleyn"]
    if "henry" in lower:
        return _FIGURE_DESCRIPTORS["henry_viii"]
    if "cromwell" in lower:
        return _FIGURE_DESCRIPTORS["cromwell"]
    if any(k in corpus for k in ("tudor", "queen", "king")):
        return "period-accurate royal attire with rich fabrics"
    return "historically accurate clothing and presence"


def _pick_expression(corpus: str) -> tuple[str, float]:
    for keywords, expr in _EXPRESSION_RULES:
        if any(k in corpus for k in keywords):
            return expr, 0.85
    return _GENERIC_EXPRESSION, 0.35


def _pick_background(corpus: str) -> tuple[str, float]:
    for keywords, bg in _ERA_BACKGROUNDS:
        if any(k in corpus for k in keywords):
            return bg, 0.82
    return _GENERIC_BACKGROUND, 0.30


def _pick_mystery_object(corpus: str, script: ScriptResult) -> tuple[str, float]:
    for keywords, obj in _MYSTERY_RULES:
        if any(k in corpus for k in keywords):
            return obj, 0.84
    hook = (script.hook or "").lower()
    if hook:
        return "a revealing historical artifact tied to the hook", 0.45
    return _GENERIC_MYSTERY, 0.28


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
        # Keep never/not — dropping them inverts meaning (e.g. Never Fell -> Fell).
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


def _pick_position(seed: str) -> tuple[str, str]:
    # Stable pseudo-random: most thumbnails put figure LEFT, text RIGHT
    h = sum(ord(c) for c in (seed or "x"))
    if h % 5 == 0:
        return "RIGHT", "LEFT"
    return "LEFT", "RIGHT"


def _title_case_name(raw: str) -> str:
    raw = re.sub(r"\s+", " ", raw.strip())
    return " ".join(w.capitalize() for w in raw.split())


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


def _fill_template(template: str, slots: ThumbnailSlots) -> str:
    mapping = {
        "HISTORICAL FIGURE": slots.historical_figure,
        "FIGURE_DESCRIPTOR": slots.figure_descriptor,
        "EXPRESSION": slots.expression,
        "FIGURE_SIDE": slots.position,
        "TEXT_SIDE": slots.text_side,
        "BACKGROUND": slots.background,
        "MYSTERY OBJECT": slots.mystery_object,
        "GOLD_TEXT_LINE": slots.gold_text,
    }

    def repl(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        if key in mapping:
            return mapping[key]
        # Partial keys like "LEFT / RIGHT" already handled; try direct
        for k, v in mapping.items():
            if k == key:
                return v
        return match.group(0)

    return _PLACEHOLDER.sub(repl, template)


def _openrouter_thumbnail_prompt(
    script: ScriptResult,
    *,
    template: str,
    seed_title: str | None,
    settings: Settings,
    channel: str | None = None,
) -> str:
    client = OpenRouterClient(settings)
    chapters = "\n".join(
        f"- {ch.title}: {ch.goal}" for ch in script.outline.chapters[:12]
    )
    ch = (channel or "").strip().lower()
    historian = ch == "napping_historian"
    genre = (
        "forbidden-secrets / documentary-mystery historical documentary"
        if historian
        else "historical what-if documentary"
    )
    audience = (
        "night-owl documentary listeners who want eerie sealed-evidence mystery"
        if historian
        else "history nerds and what-if lovers"
    )
    system_role = (
        "You are a YouTube thumbnail art director for The Napping Historian "
        "(documentary mystery — Concept: The Secret Document). "
        if historian
        else "You are a YouTube thumbnail art director for historical alternate-history documentaries. "
    )
    user = f"""Fill this YouTube thumbnail prompt template for a {genre}.

Return ONLY the complete filled prompt text — no JSON, no markdown fences, no commentary.
Replace every bracket placeholder with vivid, historically grounded, click-worthy choices.
Include 2–5 words of gold thumbnail text (uppercase) in the Text Placement section.
The gold text MUST be derived from THIS video's title (or hook) — never reuse a phrase from another video.
Do NOT use generic sticky phrases like "SHE SURVIVED" unless those exact words appear in the current title.
{"Keep the sealed parchment / secret document as the hero object; shadowed watcher only." if historian else ""}

Seed title (high CTR reference): {seed_title or script.title}

Script context:
Topic: {script.topic}
Title: {script.title}
Hook: {script.hook}
Chapters:
{chapters}

TEMPLATE TO FILL:
{template}
"""
    system = (
        f"{system_role}"
        f"Audience: {audience}. Maximize curiosity and emotional tension. "
        "Output the fully filled template only."
    )
    text = client.chat_text(
        system=system,
        user=user,
        temperature=0.75,
        reasoning_enabled=True,
    )
    text = text.strip()
    if len(text) < 200:
        raise OpenRouterError(f"OpenRouter thumbnail prompt too short: {text[:200]!r}")
    return text
