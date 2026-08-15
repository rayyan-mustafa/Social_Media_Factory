"""Packaging QA — title/thumb promise vs script (feeds Gate B)."""

from __future__ import annotations

import re
from typing import Any

from src.domain.models import ScriptResult


def check_packaging(
    *,
    title: str,
    script: ScriptResult | None = None,
    thumbnail_prompt: str = "",
) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    title_l = (title or "").lower()
    if len(title) > 100:
        warnings.append("title > 100 chars")
    if script:
        corpus = f"{script.hook} {script.topic} {script.title}".lower()
        words = {w for w in re.findall(r"[a-z]{4,}", title_l)}
        if words and not any(w in corpus for w in list(words)[:8]):
            errors.append("title does not share content words with script hook/topic")
    if thumbnail_prompt and "text" in thumbnail_prompt.lower() and "no text" not in thumbnail_prompt.lower():
        warnings.append("thumbnail prompt may request on-image text")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
    }
