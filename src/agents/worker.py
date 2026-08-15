"""ARQ / cron worker tasks — title picker + agent beats.

Runs without Redis via CLI; with REDIS_URL uses ARQ when installed.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from src.agents.cost_guardian import CostGuardian
from src.agents.farm import (
    count_encoding_jobs,
    count_gpu_jobs,
    count_inflight_jobs,
    count_prep_jobs,
    count_ready_for_stills,
    max_gpu_concurrent,
    max_prep_concurrent,
    prep_slot_available,
    prep_while_gpu_enabled,
    spawn_farm_job,
)
from src.agents.ledger import OpsLedger
from src.agents.policy_agent import PolicyAgent
from src.agents.smm_agent import SocialMediaManager
from src.agents.store import OpsStore
from src.agents.title_queue import TitleQueue
from src.agents.trends_agent import TrendsAgent
from src.agents.watchdog import WatchdogAgent
from src.services.settings import CONFIG_DIR, get_settings

logger = logging.getLogger(__name__)


def pick_and_enqueue(
    *,
    limit: int | None = None,
    enqueue_pipeline: bool = False,
    max_concurrent: int | None = None,
) -> list[dict[str, Any]]:
    """Pick approved+policy_ok titles, create ops jobs, optionally spawn real farm.

    When ``enqueue_pipeline`` is True, detaches ``run_farm_job`` (Script→TTS→
    RunPod→FFmpeg→private YouTube). Does not block the caller for hours.
    """
    store = OpsStore()
    ledger = OpsLedger(store)
    queue = TitleQueue()
    watchdog = WatchdogAgent(store, ledger)
    cost = CostGuardian(store, ledger)
    cfg = _agents_cfg()
    sched_cfg = _schedule_cfg()
    lim = limit if limit is not None else int(cfg.get("max_jobs_per_pick") or 1)
    # Hard clamp: callers cannot open a second pod (or per-channel doubles).
    max_c = min(
        int(max_concurrent if max_concurrent is not None else max_gpu_concurrent()),
        max_gpu_concurrent(),
    )

    buffer_target = int(sched_cfg.get("buffer_target") or 8)
    if queue.count_buffer() >= buffer_target:
        msg = f"buffer full ({queue.count_buffer()}>={buffer_target}) — skip new farm jobs"
        ledger.write(agent="worker", problem=msg, action="wait for publishes", severity="info")
        return [{"error": msg}]

    ok_budget, budget_msg = cost.check_can_start_job()
    if not ok_budget:
        return [{"error": budget_msg}]

    # Factory spawn path: GREEN-light only (never YELLOW/RED).
    if enqueue_pipeline:
        try:
            from src.runpod.capacity import check_farm_capacity_green
            from src.runpod.guards import (
                farm_may_use_runpod_pod_stills,
                image_backend_is_runpod_pod,
            )

            if image_backend_is_runpod_pod():
                smoke_ok, smoke_msg = farm_may_use_runpod_pod_stills()
                if not smoke_ok:
                    return [{"error": f"stills smoke gate closed: {smoke_msg}"}]
                ok_green, green_msg, bench = check_farm_capacity_green()
                if not ok_green:
                    cls = bench.classification if bench else "UNKNOWN"
                    msg = (
                        f"FARM BLOCKED green-light only "
                        f"(classification={cls}): {green_msg}"
                    )
                    ledger.write(
                        agent="worker",
                        problem=msg,
                        action="skip enqueue_pipeline",
                        severity="warn",
                        extra={"green_only": True, "classification": cls},
                    )
                    return [{"error": msg, "classification": cls, "green_only": True}]
        except Exception as gate_exc:  # noqa: BLE001
            return [{"error": f"farm capacity gate failed: {gate_exc}", "green_only": True}]

    # GPU lock for farm spawn; title-only pick (prep) ignores GPU inflight.
    if enqueue_pipeline:
        inflight = count_inflight_jobs(store)
        gpu_n = count_gpu_jobs(store)
        slots = max(0, max_c - max(inflight, gpu_n))
        if slots <= 0:
            msg = (
                f"gpu_lock full (gpu={gpu_n} / inflight={inflight} "
                f">= max_gpu={max_c}) — skip pick"
            )
            ledger.write(agent="worker", problem=msg, action="wait for farm slot", severity="info")
            return [{"error": msg, "inflight": inflight, "gpu": gpu_n, "encoding": count_encoding_jobs(store)}]
    else:
        slots = lim

    lim = min(lim, slots)
    inv = queue.inventory_approved()
    for r in inv["skipped_have_job_id"]:
        logger.info(
            "pick SKIP have_job_id channel=%s row=%s job_id=%s title=%r",
            r.channel,
            r.row_index,
            r.job_id,
            r.title,
        )
    for r in inv["ready"]:
        logger.info(
            "pick READY empty_job_id channel=%s row=%s trend=%.3f title=%r",
            r.channel,
            r.row_index,
            r.trend_score,
            r.title,
        )
    logger.info(
        "pick inventory ready=%d skipped_have_job_id=%d limit=%d",
        len(inv["ready"]),
        len(inv["skipped_have_job_id"]),
        lim,
    )

    picked = queue.pick_approved(limit=lim)
    results: list[dict[str, Any]] = []
    for row in picked:
        # Re-check GPU slot each iteration (another beat may have spawned)
        if enqueue_pipeline and count_gpu_jobs(store) >= max_c:
            results.append(
                {
                    "title": row.title,
                    "status": "deferred",
                    "error": "farm slot full mid-pick",
                }
            )
            break

        # Hard rule: never remake / re-enqueue a title that already has a job_id.
        jid = (row.job_id or "").strip()
        if jid:
            existing = store.get_job(jid)
            st = ((existing.status if existing else "") or "").lower()
            # Escape hatch only: done + notes contain refarm.
            if (
                existing is not None
                and st == "done"
                and "refarm" in ((row.notes or "").lower())
            ):
                logger.warning(
                    "pick REFARM allowed job_id=%s title=%r", jid, row.title
                )
            else:
                note = (
                    "idempotent skip — sheet job_id set; never remake "
                    "(clear job_id or notes=refarm only after done)"
                )
                if existing is None:
                    status = "skip_has_job_id"
                elif existing.video_id or st == "private":
                    status = "already_published"
                elif st in ("failed", "hold"):
                    status = "blocked_failed_hold"
                    note = (
                        "idempotent skip — prior job failed/hold; "
                        "repair same job or human-clear sheet job_id"
                    )
                elif st == "done":
                    status = "already_done"
                    note = "idempotent skip — add notes=refarm to re-enqueue"
                else:
                    status = "already_enqueued"
                    note = "idempotent skip — job already exists"
                logger.info(
                    "pick SKIP status=%s job_id=%s title=%r",
                    status,
                    jid,
                    row.title,
                )
                out: dict[str, Any] = {
                    "title": row.title,
                    "job_id": jid,
                    "status": status,
                    "note": note,
                }
                if existing is not None and existing.video_id:
                    out["video_id"] = existing.video_id
                if existing is not None and st in ("failed", "hold"):
                    out["error"] = (existing.error or "")[:200]
                results.append(out)
                continue

        pre_ok, errs = TrendsAgent(store, ledger).precheck_title(
            row.title, skip_row_index=row.row_index
        )
        if not pre_ok:
            queue.update_row(
                row.row_index,
                notes="; ".join(errs),
                status="hold",
                channel=getattr(row, "channel", None) or None,
            )
            ledger.write(
                agent="worker",
                problem=f"precheck failed: {row.title}",
                action="hold row",
                severity="warn",
                extra={"errors": errs},
            )
            results.append({"title": row.title, "status": "hold", "errors": errs})
            continue

        profile = queue.resolve_profile(row)
        channel = (getattr(row, "channel", None) or "").strip()
        # Hard enforce: historian always farms epic (~90 min) unless an explicit
        # SHEET_CHANNEL_PROFILE_NAPPING_HISTORIAN override changed the default.
        if channel.lower() == "napping_historian" and profile != "epic":
            from src.agents.sheet_channels import channel_default_profile

            forced = channel_default_profile(channel)
            if forced == "epic":
                logger.warning(
                    "historian profile was %r — forcing epic (~90 min)", profile
                )
                profile = "epic"
        # Stamp new-format SOP at enqueue for EVERY Brand channel (KNOWN_CHANNELS).
        # Historian = calm/SFX-off; napstorian = punchy/SFX+PD motion — both new-format.
        from src.agents.smm_sop import (
            is_new_format_stamped,
            stamp_new_format_sop_checklist,
        )
        from src.services.youtube_channel_auth import (
            KNOWN_CHANNELS,
            normalize_youtube_channel,
        )

        channel = normalize_youtube_channel(channel) if channel else channel
        if channel and channel not in KNOWN_CHANNELS:
            logger.warning(
                "enqueue channel %r not in KNOWN_CHANNELS %s — still applying new-format stamp",
                channel,
                KNOWN_CHANNELS,
            )
        job_meta = stamp_new_format_sop_checklist(
            {
                "source": "title_queue",
                "channel": channel,
                "sheet_tab": channel,
                "format": profile,
                "retention_profile": profile,
            },
            channel=channel,
            stage="enqueue",
        )
        if not is_new_format_stamped(job_meta):
            raise RuntimeError(
                f"new-format SOP stamp incomplete at enqueue for channel={channel!r}"
            )
        job = store.create_job(
            row.title,
            status="queued",
            stage="queued",
            sheet_row=row.row_index,
            meta=job_meta,
        )
        queue.update_row(
            row.row_index,
            status="running",
            job_id=job.id,
            notes="enqueued",
            channel=channel or None,
        )
        watchdog.on_stage(job.id, "queued", status="queued")

        pipeline_result = None
        schedule_result = None
        if enqueue_pipeline:
            pipeline_result = spawn_farm_job(job.id, store=store, watchdog=watchdog)
            cost.record(
                category="job_start",
                amount_usd=0.0,
                note=f"{row.title}|{profile}",
                job_id=job.id,
            )
            # Schedule arm happens later: farm child sets private + video_id;
            # sleep beat step 5 arms publishAt once public_approved.
        else:
            cost.record(
                category="job_start",
                amount_usd=0.0,
                note=f"{row.title}|{profile}",
                job_id=job.id,
            )

        results.append(
            {
                "title": row.title,
                "job_id": job.id,
                "row_index": row.row_index,
                "format": profile,
                "retention_profile": profile,
                "pipeline": pipeline_result,
                "schedule": schedule_result,
            }
        )
    return results


def prep_voice_jobs(
    *,
    limit: int = 1,
    max_ready: int | None = None,
    max_concurrent: int | None = None,
) -> list[dict[str, Any]]:
    """Phase A: pick approved titles and run script+Kokoro only (no RunPod).

    Safe on RED/YELLOW — skips capacity gate. Parks jobs as ``ready_for_stills``.
    Uses prep_lock (independent of gpu_lock). May run while one GPU job is on
    RunPod when ``RUNPOD_PREP_WHILE_GPU=1``. Never starts a second pod.
    """
    store = OpsStore()
    ledger = OpsLedger(store)
    # Shared prep_lock across both channels — never exceed MAX_PREP_CONCURRENT.
    max_p = min(
        int(max_concurrent if max_concurrent is not None else max_prep_concurrent()),
        max_prep_concurrent(),
    )
    cap = max_ready
    if cap is None:
        raw = (os.getenv("RUNPOD_PREP_VOICE_MAX") or "1").strip()
        try:
            cap = max(0, int(raw))
        except ValueError:
            cap = 1
    ready_n = count_ready_for_stills(store)
    if ready_n >= cap:
        return [
            {
                "status": "skipped",
                "note": f"prep buffer full ({ready_n}>={cap})",
                "ready_for_stills": ready_n,
            }
        ]

    ok_prep, prep_msg = prep_slot_available(store)
    if not ok_prep:
        return [
            {
                "status": "skipped",
                "note": prep_msg,
                "prep": count_prep_jobs(store),
                "gpu": count_gpu_jobs(store),
                "prep_while_gpu": prep_while_gpu_enabled(),
            }
        ]

    ok_budget, budget_msg = CostGuardian(store, ledger).check_can_start_job()
    if not ok_budget:
        return [{"error": budget_msg, "status": "blocked_budget"}]

    prep_n = count_prep_jobs(store)
    slots = min(limit, max(0, cap - ready_n), max(0, max_p - prep_n))
    if slots <= 0:
        return [{"status": "skipped", "note": "no prep slots"}]

    results = pick_and_enqueue(
        limit=slots,
        enqueue_pipeline=False,
        max_concurrent=max_gpu_concurrent(),
    )
    out: list[dict[str, Any]] = []
    for item in results:
        if item.get("error") and not item.get("job_id"):
            out.append(item)
            continue
        jid = item.get("job_id")
        if not jid:
            out.append(item)
            continue
        if item.get("status") in {
            "already_enqueued",
            "already_published",
            "already_done",
            "blocked_failed_hold",
            "skip_has_job_id",
            "hold",
            "deferred",
        }:
            out.append(item)
            continue
        # Re-check prep lock before each spawn (GPU job may still be running).
        ok_again, again_msg = prep_slot_available(store)
        if not ok_again:
            out.append(
                {
                    **item,
                    "status": "deferred",
                    "note": again_msg,
                }
            )
            break
        spawned = spawn_farm_job(
            str(jid),
            store=store,
            phase="prep",
            skip_capacity_gate=True,
        )
        item = {**item, "pipeline": spawned, "farm_phase": "prep"}
        out.append(item)
        if spawned.get("ok") and spawned.get("spawned"):
            CostGuardian(store, ledger).record(
                category="job_start",
                amount_usd=0.0,
                note=f"prep_voice|{item.get('title')}",
                job_id=str(jid),
            )
    return out


def approve_public(job_id: str, *, arm_schedule: bool = True) -> dict[str, Any]:
    """Mark job/row public_approved; optionally arm YouTube publishAt."""
    store = OpsStore()
    ledger = OpsLedger(store)
    queue = TitleQueue()
    job = store.get_job(job_id)
    if not job:
        return {"ok": False, "error": f"job not found: {job_id}"}
    row = queue.find_by_job_id(job_id)
    if row:
        queue.update_row(
            row.row_index,
            public_approved=True,
            channel=getattr(row, "channel", None) or None,
        )
    store.update_job(
        job_id,
        meta={**(job.meta or {}), "public_approved": True},
    )
    out: dict[str, Any] = {"ok": True, "job_id": job_id, "public_approved": True}
    if arm_schedule and job.video_id:
        from src.agents.schedule_agent import ScheduleAgent

        out["schedule"] = ScheduleAgent(store, ledger, queue).assign_slot_for_job(
            job_id=job_id,
            video_id=job.video_id,
            public_approved=True,
            apply_youtube=True,
        )
    ledger.write(
        agent="schedule",
        problem=f"human public_approved for {job_id}",
        action="armed schedule" if arm_schedule else "approved only",
        job_id=job_id,
        severity="info",
    )
    return out


def run_policy_refresh() -> dict[str, Any]:
    snap = PolicyAgent().refresh()
    return snap.model_dump()


def run_trends_harvest(
    *,
    dry_run: bool = False,
    channel: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Harvest original titles into the given sheet tab (default: napstorian).

    ``channel=napping_historian`` reads ``config/competitors_napping_historian.json``
    and appends to the ``napping_historian`` tab. Napstorian stays on
    ``config/competitors.json``.
    """
    return TrendsAgent().harvest_titles(
        dry_run=dry_run, channel=channel, limit=limit
    )


def run_backfill_competitor_source(
    *, dry_run: bool = False, use_api: bool = False
) -> dict[str, Any]:
    """Replace llm_original / placeholder competitor_source with YouTube URLs."""
    return TrendsAgent().backfill_competitor_sources(
        dry_run=dry_run, use_api=use_api
    )


def run_watchdog_scan() -> list[dict[str, Any]]:
    return WatchdogAgent().scan_stuck_jobs()


def run_repair_watchdog(*, spawn: bool = True) -> list[dict[str, Any]]:
    from src.agents.repair_watchdog import RepairWatchdog

    return RepairWatchdog().scan_and_repair(spawn=spawn)


def run_competitors_refresh(
    *,
    force: bool = False,
    dry_run: bool = False,
    channel: str | None = None,
) -> dict[str, Any]:
    from src.agents.competitors_agent import CompetitorsAgent, iter_competitors_paths

    if channel:
        return CompetitorsAgent(channel=channel).refresh(force=force, dry_run=dry_run)
    paths = iter_competitors_paths()
    if len(paths) <= 1:
        return CompetitorsAgent(path=paths[0] if paths else None).refresh(
            force=force, dry_run=dry_run
        )
    return {
        CompetitorsAgent(path=p).channel: CompetitorsAgent(path=p).refresh(
            force=force, dry_run=dry_run
        )
        for p in paths
    }


def run_competitors_metrics(
    *, dry_run: bool = False, channel: str | None = None
) -> dict[str, Any]:
    from src.agents.competitors_agent import CompetitorsAgent, iter_competitors_paths

    if channel:
        return CompetitorsAgent(channel=channel).refresh_metrics(dry_run=dry_run)
    paths = iter_competitors_paths()
    if len(paths) <= 1:
        return CompetitorsAgent(path=paths[0] if paths else None).refresh_metrics(
            dry_run=dry_run
        )
    return {
        CompetitorsAgent(path=p).channel: CompetitorsAgent(path=p).refresh_metrics(
            dry_run=dry_run
        )
        for p in paths
    }


def run_smm_eval(video_id: str | None, title: str, metrics: dict | None = None) -> dict:
    insight = SocialMediaManager().evaluate_video(
        video_id=video_id, title=title, metrics=metrics
    )
    return insight.model_dump()


def run_smm_scan() -> list[dict[str, Any]]:
    """Watch public farm videos, learn channel winners, apply allowed actions."""
    return SocialMediaManager().scan_and_act()


def run_smm_learn_winners(*, force: bool = True) -> dict[str, Any]:
    """Refresh positive patterns from channel winner videos into benchmarks."""
    return SocialMediaManager().maybe_learn_channel_winners(force=force)


def _agents_cfg() -> dict[str, Any]:
    path = CONFIG_DIR / "agents_settings.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _schedule_cfg() -> dict[str, Any]:
    path = CONFIG_DIR / "publish_schedule.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# --- Optional ARQ worker (install arq + redis to enable) ---
try:
    from arq import cron
    from arq.connections import RedisSettings

    async def agent_beat(ctx: dict) -> None:
        from src.agents.sleep_factory import run_beat

        run_beat()

    def _redis_settings() -> "RedisSettings":
        url = getattr(get_settings(), "redis_url", None) or "redis://localhost:6379"
        return RedisSettings.from_dsn(url)

    class WorkerSettings:
        functions = [agent_beat]
        cron_jobs = [cron(agent_beat, minute={0, 15, 30, 45})]

        @staticmethod
        def redis_settings() -> "RedisSettings":  # type: ignore[override]
            return _redis_settings()

except ImportError:
    WorkerSettings = None  # type: ignore
