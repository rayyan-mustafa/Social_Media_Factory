"""Load YouTube packaging from a job's youtube_meta/ folder.

Expected layout:
  youtube_meta/
    title.txt
    description.txt
    tags.json          # {primary, secondary, audience} or flat list
    thumbnail.jpg      # optional 1280x720
    thumbnail_prompt.txt  # optional provenance
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path


class YoutubeMetaError(RuntimeError):
    pass


# YouTube rejects angle brackets and most control / symbol junk in keywords.
_TAG_INVALID_CHARS = re.compile(r"[<>\[\]{}|\\^`]")
_TAG_CTRL = re.compile(r"[\x00-\x1f\x7f]")
# Keep letters/numbers across scripts, spaces, and mild punctuation.
_TAG_KEEP = re.compile(r"[^\w\s\-_'&.!?#:+]", re.UNICODE)


def load_youtube_meta(meta_dir: Path | str) -> dict:
    """Return {title, description, tags, thumbnail_path, meta_dir}."""
    meta_dir = Path(meta_dir)
    if not meta_dir.is_dir():
        raise YoutubeMetaError(f"youtube_meta folder missing: {meta_dir}")

    title_path = meta_dir / "title.txt"
    desc_path = meta_dir / "description.txt"
    tags_path = meta_dir / "tags.json"

    title = title_path.read_text(encoding="utf-8").strip() if title_path.exists() else ""
    description = (
        desc_path.read_text(encoding="utf-8").strip() if desc_path.exists() else ""
    )
    tags = _load_tags(tags_path) if tags_path.exists() else []

    thumb = None
    for name in ("thumbnail.jpg", "thumbnail.jpeg", "thumbnail.png", "thumbnail.webp"):
        cand = meta_dir / name
        if cand.exists():
            thumb = cand
            break

    if not title:
        raise YoutubeMetaError(f"title.txt empty or missing in {meta_dir}")

    return {
        "title": title[:100],
        "description": description,
        "tags": tags,
        "thumbnail_path": str(thumb.resolve()) if thumb else None,
        "meta_dir": str(meta_dir.resolve()),
    }


def _load_tags(path: Path) -> list[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return normalize_youtube_tags(flatten_youtube_tags(raw))


def flatten_youtube_tags(raw: object) -> list[str]:
    """Accept list, {primary,secondary,audience}, or {tags:[...]} → flat ordered tags."""
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    if isinstance(raw, dict):
        flat: list[str] = []
        seen: set[str] = set()
        for key in ("primary", "secondary", "audience", "tags"):
            for t in raw.get(key) or []:
                s = str(t).strip()
                if not s:
                    continue
                key_l = s.lower()
                if key_l in seen:
                    continue
                seen.add(key_l)
                flat.append(s)
        return flat
    raise YoutubeMetaError(f"tags must be list or object, got {type(raw)}")


def sanitize_youtube_tag(tag: str, *, max_len: int = 100) -> str:
    """Strip characters that trigger YouTube ``invalidTags`` (keywords) errors.

    Commas split into separate tags at the flatten/normalize layer — a single
    tag must not contain ``,``. Angle brackets and control chars are removed.
    """
    t = unicodedata.normalize("NFKC", str(tag or ""))
    t = _TAG_CTRL.sub("", t)
    t = _TAG_INVALID_CHARS.sub(" ", t)
    # Commas inside a tag → spaces (caller may have meant multi-tag strings)
    t = t.replace(",", " ")
    t = _TAG_KEEP.sub(" ", t)
    t = " ".join(t.split()).strip(" .-_#")
    # Drop pure-punctuation / empty after sanitize
    if not t or not any(ch.isalnum() for ch in t):
        return ""
    return t[: max(1, int(max_len))].strip()


def sanitize_youtube_tags(tags: list[str]) -> list[str]:
    """Map ``sanitize_youtube_tag`` over a list (no budget capping)."""
    out: list[str] = []
    for raw in tags or []:
        # Split accidental comma-joined blobs before sanitize.
        parts = str(raw).split(",") if isinstance(raw, str) and "," in str(raw) else [raw]
        for part in parts:
            s = sanitize_youtube_tag(part)
            if s:
                out.append(s)
    return out


def normalize_youtube_tags(
    tags: list[str],
    *,
    max_tags: int = 25,
    max_chars: int = 500,
) -> list[str]:
    """Sanitize, dedupe, cap count at max_tags, total chars (with separators) ≤ max_chars."""
    out: list[str] = []
    seen: set[str] = set()
    used = 0
    for raw in sanitize_youtube_tags(list(tags or [])):
        t = raw[:100]
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        # YouTube counts commas between tags; use ", " (2 chars) between entries.
        add = len(t) + (2 if out else 0)
        if len(out) >= max_tags:
            break
        if used + add > max_chars:
            break
        seen.add(key)
        out.append(t)
        used += add
    return out


def rewrite_tags_json(path: Path | str) -> dict:
    """Re-sanitize tags.json in place; return {path, before, after, changed}."""
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        before = list(raw)
        after = normalize_youtube_tags(flatten_youtube_tags(raw))
        payload: object = after
    elif isinstance(raw, dict):
        before = flatten_youtube_tags(raw)
        after = normalize_youtube_tags(before)
        payload = {**raw, "tags": after, "primary": after[:5], "secondary": after[5:15]}
    else:
        raise YoutubeMetaError(f"tags must be list or object, got {type(raw)}")
    changed = [str(x) for x in before] != list(after)
    # Always write normalized form so upload path is clean.
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "path": str(p),
        "before": before,
        "after": after,
        "changed": changed,
        "count": len(after),
    }


def _fit_youtube_tag_budget(tags: list[str], *, max_chars: int = 500) -> list[str]:
    """Back-compat wrapper — prefer normalize_youtube_tags."""
    return normalize_youtube_tags(tags, max_chars=max_chars)


# Shared disclosure footer for napstorian + napping_historian (and any channel).
# Meaning: AI-assisted visuals/narration; script remains original / human-written.
DEFAULT_YOUTUBE_AI_DISCLOSURE = (
    "Visuals and narration in this video are AI-assisted. "
    "The script is original and human-written. "
    "Altered or synthetic media is disclosed per YouTube guidelines."
)

_DISCLOSURE_FOOTER_MARKERS = (
    "ai-generated",
    "ai-assisted",
    "synthetic media",
    "altered or synthetic",
    "human-written",
)


def strip_youtube_ai_disclosure_footer(description: str) -> str:
    """Remove trailing —— disclosure blocks so packaging can refresh the line."""
    text = (description or "").rstrip()
    marker = "\n——\n"
    while marker in text:
        head, tail = text.rsplit(marker, 1)
        tail_l = tail.lower()
        if any(k in tail_l for k in _DISCLOSURE_FOOTER_MARKERS):
            text = head.rstrip()
            continue
        break
    return text


def append_youtube_ai_disclosure(
    description: str,
    disclosure: str | None = None,
) -> str:
    """Ensure description ends with the standard AI disclosure footer.

    Used by youtube_meta packaging and PublishModule so both channels share
    the same footer format. Does not claim the script is AI-written.
    """
    disc = (disclosure or DEFAULT_YOUTUBE_AI_DISCLOSURE).strip()
    if not disc:
        return description or ""
    body = strip_youtube_ai_disclosure_footer(description or "")
    if disc.lower() in body.lower():
        return body.rstrip() + "\n"
    return f"{body.rstrip()}\n\n——\n{disc}\n"
