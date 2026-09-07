"""CSV manifests for stock platform batch importers.

Follows the repo's existing CSV convention (``leadgen/tracker.py``): a module-level
column constant per schema plus ``csv.DictWriter(extrasaction="ignore")``.

Adobe Stock gets its own schema because it is the one platform that *requires* an
explicit generative-AI disclosure column — omitting it on AI work is a terms
violation, not a formatting preference.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

from src.microstock import config

logger = logging.getLogger(__name__)

ADOBE_COLUMNS = ["Filename", "Title", "Keywords", "Category", "Releases", "GeneratedByAI"]
GENERIC_COLUMNS = ["Filename", "Title", "Description", "Keywords", "Category", "AI Generated"]


def _row_for(platform: str, meta: dict[str, Any], filename: str) -> dict[str, Any]:
    keywords = ", ".join(meta.get("keywords") or [])
    title = meta.get("title", "")
    if platform == "adobe_stock":
        return {
            "Filename": filename,
            "Title": title,
            "Keywords": keywords,
            "Category": meta.get("category", ""),
            "Releases": "",
            "GeneratedByAI": "Yes",
        }
    row = config.platform_by_name(platform) or {}
    return {
        "Filename": filename,
        "Title": title,
        "Description": title,
        "Keywords": keywords,
        "Category": meta.get("category", ""),
        "AI Generated": "Yes" if row.get("requires_ai_disclosure", True) else "",
    }


def write_manifest(
    platform: str, assets: list[dict[str, Any]], manifest_path: str | Path
) -> Path:
    """Write a platform-shaped CSV manifest. ``assets`` are metadata dicts."""
    path = Path(manifest_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ADOBE_COLUMNS if platform == "adobe_stock" else GENERIC_COLUMNS

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for meta in assets:
            filename = meta.get("filename") or f"{meta.get('asset_id', '')}.svg"
            writer.writerow(_row_for(platform, meta, filename))

    logger.info("wrote %s manifest with %d row(s) -> %s", platform, len(assets), path)
    return path
