"""Tier B staging for platforms with no contributor upload API.

Adobe Stock and Freepik accept contributor uploads through their web UI only.
roadmap.txt puts them in an automated pipeline; they cannot be. So the engine
stages a ready-to-drag batch — SVGs, a platform-shaped manifest, and written
instructions — and a human does the final drop.

This is the deliberate seam in the hybrid autonomy model: everything up to here
is headless, and the irreducibly manual step is made as small as possible.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.microstock import config, paths
from src.microstock.manifest import write_manifest

logger = logging.getLogger(__name__)

_INSTRUCTIONS = """# Upload batch: {display}

Batch `{batch_id}` — {count} asset(s), staged {when}.

## Steps

1. Open the {display} contributor portal.
2. Upload every `.svg` file in this folder.
3. Import `manifest.csv` to apply titles, keywords and categories.
{ai_step}
5. Submit.

## Important

- These assets are AI-generated. {ai_note}
- Do not re-upload a batch that is already submitted — the engine has recorded
  these as delivered and will not stage them again.
{extra}
"""


def _ai_lines(platform: dict[str, Any]) -> tuple[str, str]:
    if platform.get("requires_ai_disclosure", True):
        return (
            '4. **Tick the "Generative AI" / AI-generated checkbox.** This is mandatory.',
            "Declaring them is required by the platform's terms — never skip it.",
        )
    return ("4. (No AI disclosure required for this platform.)", "")


def stage_batch(
    platform_name: str,
    assets: list[dict[str, Any]],
    *,
    batch_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Copy assets + manifest + instructions into an outbox folder for manual upload."""
    platform = config.platform_by_name(platform_name)
    if not platform:
        raise ValueError(f"unknown platform: {platform_name}")
    if platform.get("permanently_excluded"):
        raise ValueError(f"{platform_name} is permanently excluded and must never receive assets")
    if not assets:
        return {"staged": 0, "reason": "nothing_to_stage", "platform": platform_name}

    batch_id = batch_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = paths.outbox_for(platform_name, batch_id)

    if dry_run:
        return {
            "staged": 0, "reason": "dry_run", "platform": platform_name,
            "would_stage": len(assets), "target": str(target),
        }

    target.mkdir(parents=True, exist_ok=True)
    staged: list[dict[str, Any]] = []
    for meta in assets:
        source = Path(meta.get("clean_svg") or "")
        if not source.is_file():
            logger.warning("outbox: missing SVG for %s", meta.get("asset_id"))
            continue
        filename = f"{meta.get('asset_id')}.svg"
        shutil.copy2(source, target / filename)
        staged.append({**meta, "filename": filename})

    if not staged:
        return {"staged": 0, "reason": "no_readable_assets", "platform": platform_name}

    write_manifest(platform_name, staged, target / "manifest.csv")

    ai_step, ai_note = _ai_lines(platform)
    extra = ""
    lifetime_cap = platform.get("lifetime_cap_until_approved")
    if lifetime_cap:
        extra = (
            f"- {platform.get('display')} caps Tier 1 contributors at {lifetime_cap} files "
            "total until approved. The drip queue enforces this, but check your\n"
            "  dashboard tier before uploading more.\n"
        )
    (target / "UPLOAD_INSTRUCTIONS.md").write_text(
        _INSTRUCTIONS.format(
            display=platform.get("display", platform_name), batch_id=batch_id,
            count=len(staged), when=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            ai_step=ai_step, ai_note=ai_note, extra=extra,
        ),
        encoding="utf-8",
    )

    logger.info("staged %d asset(s) for %s in %s", len(staged), platform_name, target)
    return {
        "staged": len(staged), "platform": platform_name, "batch_id": batch_id,
        "target": str(target), "asset_ids": [a["asset_id"] for a in staged],
    }
