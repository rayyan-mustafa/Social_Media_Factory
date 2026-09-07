"""Vision tagging — title, category and keywords for stock submission.

roadmap.txt reaches for the ``google-genai`` SDK and passes a raw file handle in
``contents``; that is not how vision APIs accept images, and the SDK is not used
anywhere in this repo. This module instead reuses what the farm already proved
out: base64 data URLs via ``vision_judge._b64_data_url``, ``OpenRouterClient``
for the call, and a process-wide rate gate so bulk tagging cannot 429 the pool.

The roadmap's anti-spam rules (5-10 word titles, 25-35 keywords, no stuffing) are
enforced *in code* after the model answers, not merely requested in the prompt.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.microstock import config, paths

logger = logging.getLogger(__name__)

VALID_CATEGORIES = (
    "Technology", "Business", "Icons", "Nature", "People",
    "Abstract", "Food", "Healthcare", "Education", "Industrial",
)

# Words that carry no search value and signal keyword stuffing to review filters.
_FILLER = {
    "vector", "graphic", "design", "asset", "illustration", "image", "eps",
    "eps10", "file", "art", "artwork", "clipart", "stock", "royalty", "free",
    "download", "template", "element", "background", "set", "collection", "pack",
}


class TagError(RuntimeError):
    """Tagging failed or produced metadata that cannot be made compliant."""


@dataclass
class AssetMetadata:
    asset_id: str = ""
    title: str = ""
    category: str = "Abstract"
    keywords: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "title": self.title,
            "category": self.category,
            "keywords": self.keywords,
            "warnings": self.warnings,
            "source": self.source,
        }


def _clean_title(raw: str, *, min_words: int, max_words: int) -> tuple[str, list[str]]:
    """Normalise a title and reject keyword-stuffed ones."""
    warnings: list[str] = []
    title = re.sub(r"\s+", " ", str(raw or "")).strip(" .,-–—")
    title = re.sub(r"\s*[-–—|]\s*(vector|eps\s?10?|illustration)\s*$", "", title, flags=re.I)
    words = title.split()

    # Stuffing check: a run of >=3 consecutive filler words is what filters flag.
    run = 0
    for word in words:
        run = run + 1 if word.lower().strip(",") in _FILLER else 0
        if run >= 3:
            warnings.append("title looked keyword-stuffed; filler words removed")
            words = [w for w in words if w.lower().strip(",") not in _FILLER]
            break

    if len(words) > max_words:
        words = words[:max_words]
        warnings.append(f"title truncated to {max_words} words")
    title = " ".join(words).strip()
    if title:
        title = title[0].upper() + title[1:]
    if len(words) < min_words:
        warnings.append(f"title is only {len(words)} words (min {min_words})")
    return title, warnings


def _clean_keywords(raw: Any, *, minimum: int, maximum: int) -> tuple[list[str], list[str]]:
    """Deduplicate, normalise and clamp keywords to the platform-safe range."""
    warnings: list[str] = []
    items: list[str] = []
    if isinstance(raw, str):
        raw = re.split(r"[,;\n]", raw)
    for entry in raw or []:
        word = re.sub(r"\s+", " ", str(entry)).strip().strip(",.").lower()
        if word and len(word) > 1 and word not in items:
            items.append(word)
    if len(items) > maximum:
        items = items[:maximum]
        warnings.append(f"keywords trimmed to {maximum}")
    if len(items) < minimum:
        warnings.append(f"only {len(items)} keywords (min {minimum}) — search rank will suffer")
    return items, warnings


def _normalise_category(raw: Any) -> str:
    text = str(raw or "").strip()
    for category in VALID_CATEGORIES:
        if category.lower() == text.lower():
            return category
    # Match in both directions: "tech" -> Technology, and
    # "Technology and Computing" -> Technology.
    for category in VALID_CATEGORIES:
        low = category.lower()
        if len(text) >= 3 and (low in text.lower() or text.lower() in low):
            return category
    return "Abstract"


def normalise(payload: dict[str, Any], *, asset_id: str = "", source: str = "") -> AssetMetadata:
    """Turn a raw model response into compliant metadata. Pure and testable."""
    cfg = config.section("tagger")
    title, title_warnings = _clean_title(
        payload.get("title", ""),
        min_words=int(cfg.get("title_min_words") or 5),
        max_words=int(cfg.get("title_max_words") or 10),
    )
    keywords, keyword_warnings = _clean_keywords(
        payload.get("keywords"),
        minimum=int(cfg.get("keywords_min") or 25),
        maximum=int(cfg.get("keywords_max") or 35),
    )
    return AssetMetadata(
        asset_id=asset_id,
        title=title,
        category=_normalise_category(payload.get("category")),
        keywords=keywords,
        warnings=title_warnings + keyword_warnings,
        source=source,
    )


def _build_prompt() -> str:
    from src.services.settings import load_prompt, render_prompt

    cfg = config.section("tagger")
    template = load_prompt("asset_tag", prompts_dir=paths.PROMPTS_DIR)
    return render_prompt(template, {
        "TITLE_MIN": int(cfg.get("title_min_words") or 5),
        "TITLE_MAX": int(cfg.get("title_max_words") or 10),
        "KW_MIN": int(cfg.get("keywords_min") or 25),
        "KW_MAX": int(cfg.get("keywords_max") or 35),
    })


def tag_image(
    image_path: str | Path, *, asset_id: str = "", backend: str | None = None,
) -> AssetMetadata:
    """Tag a raster with commercial metadata using a vision model."""
    path = Path(image_path)
    if not path.is_file():
        raise TagError(f"image not found: {path}")

    cfg = config.section("tagger")
    name = (backend or cfg.get("backend") or "openrouter").strip()

    if name == "mock":
        stem = re.sub(r"[^a-z ]", " ", path.stem.lower()).split()
        base = stem or ["abstract", "vector", "shape"]
        return normalise(
            {
                "title": " ".join((base * 3)[:6]),
                "category": "Abstract",
                "keywords": [f"{w}{i}" for i in range(1, 30) for w in base][:28],
            },
            asset_id=asset_id or path.stem, source="mock",
        )

    # Reuse the farm's proven image encoding: downscale, JPEG, size-capped data URL.
    from src.services.vision_judge import _b64_data_url

    data_url = _b64_data_url(path)
    if not data_url:
        raise TagError(f"could not encode {path.name} for vision tagging")

    if name != "openrouter":
        raise TagError(f"unknown tagger backend {name!r}")

    from src.services.openrouter import OpenRouterClient, OpenRouterError
    from src.services.settings import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    if not (settings.openrouter_api_key or "").strip():
        raise TagError("OPENROUTER_API_KEY is not set — cannot tag assets")

    client = OpenRouterClient(settings, default_model=str(cfg.get("model") or "openrouter/free"))
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": _build_prompt()},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
    }]
    try:
        payload = client.chat_json(messages)
    except OpenRouterError as exc:
        raise TagError(f"vision tagging failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise TagError("vision tagger returned a non-object response")

    metadata = normalise(payload, asset_id=asset_id or path.stem, source=name)
    if not metadata.title:
        raise TagError("vision tagger returned no usable title")
    logger.info("tagged %s: %r (%d keywords)", path.name, metadata.title, len(metadata.keywords))
    return metadata
