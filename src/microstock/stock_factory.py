"""The microstock beat — one unattended cycle.

Mirrors ``src/agents/sleep_factory.py::run_beat``: gates first, then numbered
steps, then a ``*_last.json`` snapshot. Deliberately a *separate* cron entry from
the video farm's beats so a microstock failure can never stall video production.

Gates, in order — any closed gate ends the beat cleanly rather than degrading:
  0a  master switch          config.enabled
  0b  budget ceiling         CostGuardian (its own $25, not the farm's)
  0c  disk headroom
  0d  CPU politeness         skip while the video farm is encoding

Steps:
  1  brief harvest refill
  2  generate -> trace -> clean -> Gate V
  3  tag passed assets
  4  enqueue into the per-platform drip queue
  5  release one beat's allowance (FTP + outbox staging)
  6  refresh sales benchmarks -> biases step 1 next time
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from typing import Any

from src.microstock import brief_harvest, config, drip, paths
from src.microstock.ledger import AssetLedger

logger = logging.getLogger(__name__)

MIN_FREE_DISK_GB = 5.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_last(payload: dict[str, Any]) -> None:
    paths.OPS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = paths.BEAT_LAST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(paths.BEAT_LAST_PATH)


def _disk_gate() -> tuple[bool, str]:
    try:
        usage = shutil.disk_usage(paths.ROOT)
    except OSError as exc:
        return True, f"disk check unavailable ({exc})"
    free_gb = usage.free / (1024**3)
    if free_gb < MIN_FREE_DISK_GB:
        return False, f"only {free_gb:.1f}GB free (need {MIN_FREE_DISK_GB}GB)"
    return True, f"{free_gb:.1f}GB free"


def _farm_busy_gate() -> tuple[bool, str]:
    """Skip the beat while the video farm is mid-encode.

    vtracer is CPU-heavy and this engine is strictly the lower-priority tenant;
    the video farm must never be slowed down by it.
    """
    politeness = config.section("politeness")
    if not politeness.get("skip_if_farm_busy", True):
        return True, "politeness disabled"
    busy_statuses = set(politeness.get("farm_busy_statuses") or [])
    try:
        from src.agents.store import OpsStore

        for job in OpsStore().list_jobs():
            status = str(getattr(job, "status", "") or "")
            if status in busy_statuses:
                return False, f"video farm busy (job in '{status}')"
    except Exception as exc:  # noqa: BLE001 - never let the farm's state break this beat
        logger.debug("farm busy check skipped: %s", exc)
        return True, f"farm state unreadable ({exc})"
    return True, "farm idle"


def _budget_gate(estimated_usd: float) -> tuple[bool, str]:
    try:
        from src.agents.cost_guardian import CostGuardian
        from src.agents.ledger import OpsLedger
        from src.agents.store import OpsStore

        store = OpsStore(root=paths.OPS_DIR)
        guardian = CostGuardian(store=store, ledger=OpsLedger(store=store))
        return guardian.check_can_start_job(estimated_usd=estimated_usd)
    except Exception as exc:  # noqa: BLE001
        logger.debug("budget gate unavailable: %s", exc)
        return True, f"budget check unavailable ({exc})"


def run_beat(
    *,
    max_assets: int | None = None,
    backend: str | None = None,
    dry_run: bool = False,
    skip_gates: bool = False,
    use_llm: bool = True,
) -> dict[str, Any]:
    """One unattended cycle. Safe to run from cron at any interval."""
    paths.ensure_dirs()
    started = _now()
    steps: dict[str, Any] = {}
    settings = config.load_settings()

    # ---- gates -------------------------------------------------------
    gates: dict[str, Any] = {}
    if not skip_gates:
        if not config.is_enabled():
            result = {
                "at": started, "ran": False,
                "reason": "master switch off (config/microstock/settings.json -> enabled)",
            }
            _write_last(result)
            return result

        disk_ok, disk_msg = _disk_gate()
        gates["disk"] = disk_msg
        if not disk_ok:
            result = {"at": started, "ran": False, "reason": f"disk gate: {disk_msg}", "gates": gates}
            _write_last(result)
            return result

        farm_ok, farm_msg = _farm_busy_gate()
        gates["politeness"] = farm_msg
        if not farm_ok:
            result = {"at": started, "ran": False, "reason": farm_msg, "gates": gates}
            _write_last(result)
            return result

    per_beat = int(
        max_assets
        if max_assets is not None
        else settings.get("max_assets_per_beat") or 12
    )

    generator_cfg = config.section("generator")
    backend_name = backend or str(generator_cfg.get("backend") or "gemini")
    unit_cost = float((generator_cfg.get(backend_name) or {}).get("usd_per_image") or 0.0)
    if not skip_gates:
        budget_ok, budget_msg = _budget_gate(unit_cost * per_beat)
        gates["budget"] = budget_msg
        if not budget_ok:
            result = {"at": started, "ran": False, "reason": f"budget gate: {budget_msg}", "gates": gates}
            _write_last(result)
            return result

    ledger = AssetLedger()

    # ---- 1. harvest ---------------------------------------------------
    steps["harvest"] = brief_harvest.maybe_refill(use_llm=use_llm, dry_run=dry_run)

    # ---- 2. produce ---------------------------------------------------
    if dry_run:
        # A dry run must still be informative: harvest did not actually add
        # briefs, so count what it *would* have added towards what we'd produce.
        available = brief_harvest.stock_count() + int(steps["harvest"].get("would_add") or 0)
        steps["produce"] = {
            "processed": 0, "reason": "dry_run",
            "would_process": min(per_beat, available),
            "briefs_available": available,
        }
        briefs = []
    else:
        briefs = brief_harvest.consume(per_beat)
    if not dry_run and not briefs:
        steps["produce"] = {"processed": 0, "reason": "no_briefs"}
    elif not dry_run:
        from src.microstock.pipeline import process_batch, summarise

        results = process_batch(briefs, ledger=ledger, backend=backend)
        steps["produce"] = summarise(results)
        passed_ids = [r.asset_id for r in results if r.passed]

        # Briefs that never ran (quota parked) go back on the stock.
        unused = briefs[len(results):]
        if unused:
            brief_harvest.add(unused)
            steps["produce"]["parked_briefs"] = len(unused)

        # ---- 3. tag ---------------------------------------------------
        steps["tag"] = _tag_assets(passed_ids, ledger)

        # ---- 4. enqueue ----------------------------------------------
        steps["enqueue"] = drip.enqueue(passed_ids, ledger=ledger)

    # ---- 5. release ---------------------------------------------------
    steps["release"] = drip.release(ledger=ledger, dry_run=dry_run or None)

    # ---- 6. sales feedback --------------------------------------------
    from src.microstock import stock_smm

    steps["sales"] = stock_smm.maybe_refresh_sales(ledger=ledger, dry_run=dry_run)

    result = {
        "at": started, "finished_at": _now(), "ran": True, "dry_run": dry_run,
        "backend": backend_name, "gates": gates, "steps": steps,
        "stock_remaining": brief_harvest.stock_count(),
        "ledger_counts": ledger.counts_by_stage(),
    }
    _write_last(result)
    logger.info("microstock beat complete: %s", steps.get("produce"))
    return result


def _tag_assets(asset_ids: list[str], ledger: AssetLedger) -> dict[str, Any]:
    """Tag every passed asset, tolerating a tagger outage without losing work."""
    if not asset_ids:
        return {"tagged": 0, "reason": "nothing_passed"}

    from src.microstock.ai_tagger import TagError, tag_image

    tagged, failed = 0, []
    for asset_id in asset_ids:
        record = ledger.get(asset_id) or {}
        png = record.get("png")
        if not png:
            continue
        try:
            metadata = tag_image(png, asset_id=asset_id)
        except TagError as exc:
            # Not fatal: the asset stays at gate_passed and is retried next beat.
            failed.append({"asset_id": asset_id, "error": str(exc)[:120]})
            continue
        ledger.set_stage(asset_id, "tagged", metadata=metadata.as_dict())
        tagged += 1
    return {"tagged": tagged, "failed": failed}
