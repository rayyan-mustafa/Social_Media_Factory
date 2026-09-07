"""Per-platform release queue.

At 200 assets/day the engine outruns what the best-paying platforms will accept:
Adobe Stock caps contributors around 30-50 uploads/day and Freepik allows only
150-200 files total until Tier 1 approval. Dumping everything at the uncapped
low-royalty platforms because they happen to accept it would be the wrong trade.

So production and distribution are decoupled. Gate-V-passed assets enter a
pending queue, and each beat releases only each platform's remaining allowance,
walking platforms in royalty order so the backlog always drains toward the best
payout first.
"""

from __future__ import annotations

import logging
from typing import Any

from src.microstock import config
from src.microstock.ledger import AssetLedger

logger = logging.getLogger(__name__)


def enqueue(asset_ids: list[str], *, ledger: AssetLedger | None = None) -> dict[str, Any]:
    """Queue passed assets for every enabled platform."""
    ledger = ledger or AssetLedger()
    platforms = [p["name"] for p in config.load_platforms(enabled_only=True)]
    if not platforms:
        return {"queued": 0, "reason": "no_enabled_platforms"}

    queued = 0
    for asset_id in asset_ids:
        record = ledger.get(asset_id)
        if not record or record.get("stage") not in ("gate_passed", "tagged", "queued"):
            continue
        ledger.queue_for_platforms(asset_id, platforms)
        queued += 1
    return {"queued": queued, "platforms": platforms}


def allowance(platform_name: str, *, ledger: AssetLedger | None = None) -> int:
    """How many more files this platform will accept right now."""
    ledger = ledger or AssetLedger()
    platform = config.platform_by_name(platform_name) or {}
    if platform.get("permanently_excluded") or not platform.get("enabled"):
        return 0

    remaining = int(platform.get("daily_cap") or 0) - ledger.uploaded_today(platform_name)

    lifetime_cap = platform.get("lifetime_cap_until_approved")
    if lifetime_cap:
        lifetime_left = int(lifetime_cap) - ledger.uploaded_count(platform_name)
        remaining = min(remaining, lifetime_left)
        if lifetime_left <= 0:
            logger.info(
                "%s lifetime cap reached (%s files) — approval required before more",
                platform_name, lifetime_cap,
            )
    return max(0, remaining)


def plan(*, ledger: AssetLedger | None = None) -> list[dict[str, Any]]:
    """What each platform would receive this beat, in royalty order."""
    ledger = ledger or AssetLedger()
    rows: list[dict[str, Any]] = []
    for platform_name in config.release_order():
        platform = config.platform_by_name(platform_name) or {}
        if platform.get("permanently_excluded") or not platform.get("enabled"):
            continue
        room = allowance(platform_name, ledger=ledger)
        pending = ledger.pending_for_platform(platform_name)
        release = pending[:room] if room > 0 else []
        rows.append({
            "platform": platform_name,
            "tier": platform.get("tier"),
            "pending": len(pending),
            "allowance": room,
            "releasing": len(release),
            "backlog": max(0, len(pending) - len(release)),
            "asset_ids": [r["asset_id"] for r in release],
            "assets": release,
        })
    return rows


def release(
    *, ledger: AssetLedger | None = None, dry_run: bool | None = None,
) -> dict[str, Any]:
    """Release one beat's worth of assets to every enabled platform."""
    ledger = ledger or AssetLedger()
    from src.microstock.ftp_uploader import UploadError, upload_batch
    from src.microstock.outbox import stage_batch

    if dry_run is None:
        dry_run = bool(config.section("distribution").get("dry_run", True))

    results: list[dict[str, Any]] = []
    for row in plan(ledger=ledger):
        if not row["releasing"]:
            results.append({
                "platform": row["platform"], "released": 0,
                "reason": "no_allowance" if row["pending"] else "nothing_pending",
                "pending": row["pending"], "backlog": row["backlog"],
            })
            continue

        if row["tier"] == "auto":
            try:
                outcome = upload_batch(
                    row["platform"], row["assets"], ledger=ledger, dry_run=dry_run
                ).as_dict()
            except UploadError as exc:
                outcome = {"platform": row["platform"], "error": str(exc), "uploaded": 0}
            outcome["backlog"] = row["backlog"]
            results.append(outcome)
        else:
            outcome = stage_batch(row["platform"], row["assets"], dry_run=dry_run)
            # Staged is not delivered — a human still has to drop the batch, so
            # the ledger is only marked once they confirm with `microstock confirm`.
            outcome["backlog"] = row["backlog"]
            outcome["note"] = "staged for manual upload; run 'microstock confirm' after submitting"
            results.append(outcome)

    return {"dry_run": dry_run, "platforms": results}


def confirm_manual_upload(
    platform_name: str, asset_ids: list[str], *, ledger: AssetLedger | None = None
) -> dict[str, Any]:
    """Record that a human actually submitted a staged Tier B batch.

    Kept separate from staging on purpose: marking an asset delivered when it was
    only *staged* would silently drop it from the queue and it would never be
    uploaded anywhere.
    """
    ledger = ledger or AssetLedger()
    confirmed: list[str] = []
    for asset_id in asset_ids:
        if ledger.get(asset_id) and not ledger.already_uploaded(asset_id, platform_name):
            ledger.mark_uploaded(asset_id, platform_name)
            confirmed.append(asset_id)
    return {"platform": platform_name, "confirmed": len(confirmed), "asset_ids": confirmed}
