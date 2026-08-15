"""Sleep-mode factory beat — keep farming/scheduling healthy while you sleep.

Overnight defaults (config/agents_settings.json → sleep_beat):
- enqueue_pipeline + run_pipeline ON → spawn real Script→TTS→RunPod→FFmpeg→private YT
- Does NOT invent+auto-approve titles (you still set approved=TRUE)
- Does NOT auto public_approve (schedule arms only after public_approved=TRUE)
- DOES: policy, watchdog, farm reconcile, schedule arm, harvest refill, pick+spawn

HARD RULE: farm spawn is **GREEN-light only** (capacity benchmark). YELLOW/RED
never start the factory. Prefer the RunPod capacity watchdog cron for
autonomous starts; keep blind sleep_factory farm DISARMED unless gated.

Preferred autonomy cron (watchdog-gated GREEN-only):
  */10 * * * * cd /home/ubuntu/new_yt_automation && .venv/bin/python -m src.cli.runpod_watchdog \\
    >> output/ops/runpod_watchdog.log 2>&1

Cron (coalesce backup for schedule+harvest; farm spawn stays disarmed in config):
  */15 * * * * cd /home/ubuntu/new_yt_automation && .venv/bin/python -m src.cli.sleep_factory beat \\
    >> output/ops/sleep_factory.log 2>&1

Escape hatch: ``python -m src.cli.sleep_factory beat --no-run-pipeline``
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.cost_guardian import CostGuardian
from src.agents.farm import max_concurrent_jobs, reconcile_farms
from src.agents.ledger import OpsLedger
from src.agents.policy_agent import PolicyAgent
from src.agents.schedule_agent import ScheduleAgent
from src.agents.store import OpsStore
from src.agents.title_queue import TitleQueue
from src.agents.watchdog import WatchdogAgent
from src.agents.worker import pick_and_enqueue
from src.services.settings import CONFIG_DIR, ROOT

logger = logging.getLogger(__name__)


def _maybe_ceo_smm_from_sleep() -> dict[str, Any]:
    """Run CEO beat at most ~hourly from */15 sleep so digests aren't tripled."""
    from src.agents.ceo_smm import CEO_BEAT_LAST, _read_json, run_ceo_beat

    last = _read_json(CEO_BEAT_LAST)
    last_beat = (last.get("last_beat") or {}).get("at") or last.get("last_email_at")
    if last_beat:
        try:
            prev = datetime.fromisoformat(str(last_beat).replace("Z", "+00:00"))
            age_m = (datetime.now(timezone.utc) - prev).total_seconds() / 60.0
            if age_m < 50:
                return {"skipped": True, "reason": "ceo_coalesce_50m", "age_m": round(age_m, 1)}
        except Exception:  # noqa: BLE001
            pass
    return run_ceo_beat()


def run_beat(
    *,
    harvest_if_queue_low: bool | None = None,
    enqueue_pipeline: bool | None = None,
    run_pipeline: bool | None = None,
    min_queued_titles: int | None = None,
    max_picks: int | None = None,
) -> dict[str, Any]:
    """One unattended cycle. Pipeline spawn defaults ON from sleep_beat config."""
    beat_cfg = (_agents_cfg().get("sleep_beat") or {})
    if harvest_if_queue_low is None:
        harvest_if_queue_low = bool(beat_cfg.get("harvest_if_queue_low", True))
    if min_queued_titles is None:
        min_queued_titles = int(beat_cfg.get("min_queued_titles") or 5)
    if max_picks is None:
        max_picks = int(
            beat_cfg.get("max_picks_per_beat")
            or (_agents_cfg().get("max_jobs_per_pick") or 1)
        )

    # Config defaults ON for overnight autonomy; CLI --no-run-pipeline overrides
    cfg_enqueue = bool(beat_cfg.get("enqueue_pipeline", True))
    cfg_run = bool(beat_cfg.get("run_pipeline", cfg_enqueue))
    if enqueue_pipeline is None:
        enqueue_pipeline = cfg_enqueue
    if run_pipeline is None:
        run_pipeline = cfg_run
    # Spawn farm when both knobs allow (CLI --no-run-pipeline clears run_pipeline)
    do_farm = bool(enqueue_pipeline and run_pipeline)
    # Hard gate: IMAGE_BACKEND=runpod_pod requires smoke_ok before any farm spawn.
    smoke_gate_ok = True
    smoke_gate_msg = "ok"
    try:
        from src.runpod.guards import farm_may_use_runpod_pod_stills

        smoke_gate_ok, smoke_gate_msg = farm_may_use_runpod_pod_stills()
    except Exception as gate_exc:  # noqa: BLE001
        smoke_gate_ok, smoke_gate_msg = False, str(gate_exc)
    if do_farm and not smoke_gate_ok:
        logger.error(
            "sleep_beat: refusing farm spawn — stills smoke gate closed: %s",
            smoke_gate_msg,
        )
        do_farm = False

    # Hard gate: farm is GREEN-light only (never YELLOW/RED / Secure-only).
    capacity_green = True
    capacity_msg = "skipped (farm off)"
    capacity_classification = None
    if do_farm:
        try:
            from src.runpod.capacity import check_farm_capacity_green

            capacity_green, capacity_msg, bench = check_farm_capacity_green()
            capacity_classification = (
                bench.classification if bench is not None else None
            )
        except Exception as cap_exc:  # noqa: BLE001
            capacity_green, capacity_msg = False, str(cap_exc)
        if not capacity_green:
            logger.error(
                "sleep_beat: refusing farm spawn — not GREEN: %s",
                capacity_msg,
            )
            do_farm = False
    max_c = max_concurrent_jobs()

    store = OpsStore()
    ledger = OpsLedger(store)
    queue = TitleQueue()
    out: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "steps": {},
        "config": {
            "harvest_if_queue_low": harvest_if_queue_low,
            "min_queued_titles": min_queued_titles,
            "enqueue_pipeline": enqueue_pipeline,
            "run_pipeline": run_pipeline,
            "do_farm": do_farm,
            "max_picks_per_beat": max_picks,
            "max_concurrent_jobs": max_c,
            "stills_smoke_gate_ok": smoke_gate_ok,
            "stills_smoke_gate": smoke_gate_msg,
            "farm_green_only": True,
            "capacity_green": capacity_green,
            "capacity_classification": capacity_classification,
        },
    }
    out["steps"]["stills_smoke_gate"] = {
        "ok": smoke_gate_ok,
        "message": smoke_gate_msg,
        "do_farm": do_farm,
    }
    out["steps"]["capacity_green_gate"] = {
        "ok": capacity_green,
        "message": capacity_msg,
        "classification": capacity_classification,
        "green_only": True,
        "do_farm": do_farm,
    }

    # Shared disk guard (primary beat is */10 runpod_watchdog)
    try:
        from src.runpod.disk_guard import maybe_run_disk_guard

        out["steps"]["disk_guard"] = maybe_run_disk_guard(dry_run=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sleep_factory disk_guard failed: %s", exc)
        out["steps"]["disk_guard"] = {"error": str(exc)[:300]}


    # 1) Policy freshness
    try:
        snap = PolicyAgent(store, ledger).refresh()
        out["steps"]["policy"] = {
            "version": snap.version,
            "compile_ok": snap.compile_ok,
        }
    except Exception as exc:  # noqa: BLE001
        out["steps"]["policy"] = {"error": str(exc)}
        ledger.write(
            agent="sleep_factory",
            problem=f"policy refresh failed: {exc}",
            action="continue beat",
            severity="warn",
        )

    # 2) Reconcile dead farm PIDs + spawn orphan queued BEFORE stuck scan
    #    so capacity/GREEN waits are not false-HOLD'd as "stuck in queued".
    try:
        out["steps"]["farm_reconcile"] = reconcile_farms(
            store=store,
            ledger=ledger,
            queue=queue,
            spawn_queued=do_farm,
        )
    except Exception as exc:  # noqa: BLE001
        out["steps"]["farm_reconcile"] = {"error": str(exc)}

    # 2a) Job health — dead farm, SFX stall, hung ffmpeg, upload gap (always heal)
    try:
        from src.agents.job_health_watchdog import scan_and_heal_jobs

        out["steps"]["job_health"] = scan_and_heal_jobs(
            store=store, ledger=ledger, spawn=True
        )
    except Exception as exc:  # noqa: BLE001
        out["steps"]["job_health"] = {"error": str(exc)}

    # 2b) Watchdog stuck jobs (queued → spawn/heartbeat; other stages → HOLD)
    try:
        out["steps"]["watchdog"] = WatchdogAgent(store, ledger).scan_stuck_jobs()
    except Exception as exc:  # noqa: BLE001
        out["steps"]["watchdog"] = {"error": str(exc)}

    # 2c) RepairWatchdog — classify failed jobs, repair, resume farm
    try:
        from src.agents.repair_watchdog import RepairWatchdog

        out["steps"]["repair_watchdog"] = RepairWatchdog(
            store, ledger, queue
        ).scan_and_repair(spawn=do_farm)
    except Exception as exc:  # noqa: BLE001
        out["steps"]["repair_watchdog"] = {"error": str(exc)}

    # 2c2) SMM — onboard private uploads, pin comments, eval + allowed actions
    try:
        from src.agents.smm_agent import SocialMediaManager

        out["steps"]["smm"] = SocialMediaManager(store, ledger).scan_and_act()
    except Exception as exc:  # noqa: BLE001
        out["steps"]["smm"] = {"error": str(exc)}

    # 2c3) CEO-SMM — full-auto growth (coalesced; hourly cron is primary)
    try:
        out["steps"]["ceo_smm"] = _maybe_ceo_smm_from_sleep()
    except Exception as exc:  # noqa: BLE001
        out["steps"]["ceo_smm"] = {"error": str(exc)}

    # 2d) Competitors / search-query refresh (does not block when channel_ids empty)
    if bool(beat_cfg.get("competitors_refresh_if_due", True)):
        try:
            from src.agents.competitors_agent import (
                CompetitorsAgent,
                iter_competitors_paths,
            )

            refresh_by_channel: dict[str, Any] = {}
            for cpath in iter_competitors_paths():
                agent = CompetitorsAgent(store, ledger, path=cpath)
                refresh_by_channel[agent.channel] = agent.refresh()
            out["steps"]["competitors_refresh"] = (
                refresh_by_channel
                if len(refresh_by_channel) > 1
                else next(iter(refresh_by_channel.values()), {"skipped": True})
            )
        except Exception as exc:  # noqa: BLE001
            out["steps"]["competitors_refresh"] = {"error": str(exc)}
    else:
        out["steps"]["competitors_refresh"] = {"skipped": True}

    # 2e) Power metrics — daily cadence (full refresh() also scores when it writes)
    try:
        from src.agents.competitors_agent import (
            CompetitorsAgent,
            iter_competitors_paths,
            metrics_is_due,
        )

        metrics_by_channel: dict[str, Any] = {}
        cref_all = out["steps"].get("competitors_refresh") or {}
        for cpath in iter_competitors_paths():
            agent = CompetitorsAgent(store, ledger, path=cpath)
            ch = agent.channel
            cref = (
                cref_all.get(ch)
                if isinstance(cref_all, dict) and ch in cref_all
                else cref_all
                if isinstance(cref_all, dict) and "metrics" in cref_all
                else {}
            )
            already = isinstance(cref.get("metrics"), dict) and not cref["metrics"].get(
                "error"
            )
            due, reason = metrics_is_due(agent.cfg)
            if already:
                metrics_by_channel[ch] = {
                    "from_refresh": True,
                    **(cref.get("metrics") or {}),
                }
            elif due:
                metrics_by_channel[ch] = agent.refresh_metrics()
            else:
                metrics_by_channel[ch] = {
                    "skipped": True,
                    "reason": reason,
                }
        out["steps"]["competitors_metrics"] = (
            metrics_by_channel
            if len(metrics_by_channel) > 1
            else next(iter(metrics_by_channel.values()), {"skipped": True})
        )
    except Exception as exc:  # noqa: BLE001
        out["steps"]["competitors_metrics"] = {"error": str(exc)}

    # 3) Arm publishAt for private+public_approved (before harvest so same-tick
    #    consume-replace credits can refill). Shared with */10 watchdog + coalesce.
    try:
        from src.agents.schedule_agent import maybe_arm_public_approved_schedule

        arm_payload = maybe_arm_public_approved_schedule(
            store=store, ledger=ledger, queue=queue, dry_run=False
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("sleep_factory schedule_arm failed: %s", exc)
        arm_payload = {"ok": False, "error": str(exc)[:300]}
    out["steps"]["schedule_arm"] = arm_payload
    out["steps"]["schedule_status"] = (arm_payload or {}).get(
        "schedule_status"
    ) or ScheduleAgent(store, ledger, queue).status()

    # 4) Harvest: fill idea stock to target AND replace consumed public_approved arms
    if harvest_if_queue_low:
        try:
            from src.agents.idea_stock import maybe_refill_idea_stock

            out["steps"]["harvest"] = maybe_refill_idea_stock(
                queue=queue, store=store, ledger=ledger
            )
        except Exception as exc:  # noqa: BLE001
            out["steps"]["harvest"] = {"error": str(exc)}
    else:
        out["steps"]["harvest"] = {
            "skipped": True,
            "queued_policy_ok": sum(
                1
                for r in queue.list_rows()
                if (r.status or "queued") == "queued" and r.policy_ok
            ),
            "min_queued_titles": min_queued_titles,
        }

    # 5) Pick human-approved titles; spawn detached production farm when do_farm
    try:
        out["steps"]["pick"] = pick_and_enqueue(
            limit=max_picks,
            enqueue_pipeline=do_farm,
            max_concurrent=max_c,
        )
    except Exception as exc:  # noqa: BLE001
        out["steps"]["pick"] = {"error": str(exc)}

    out["steps"]["budget"] = CostGuardian(store, ledger).check_can_start_job()[1]

    # 5b) Shorts harvest + cascade + publish (parent-bound; not a second farm)
    try:
        from src.agents.sheet_channels import configured_sheet_channels, ensure_shorts_tabs
        from src.agents.sheet_hygiene import cascade_shorts_from_longform
        from src.agents.shorts_harvest import harvest_shorts_titles
        from src.agents.shorts_publish import publish_ready_shorts
        from src.content.derivatives import shorts_clip_allowed

        ensure_shorts_tabs()
        hs: dict[str, Any] = {}
        for ch in configured_sheet_channels():
            hs[ch] = harvest_shorts_titles(channel=ch, store=store, queue=queue)
        out["steps"]["shorts_harvest"] = hs
        out["steps"]["shorts_cascade"] = cascade_shorts_from_longform(queue=queue)
        if shorts_clip_allowed():
            out["steps"]["shorts_publish"] = publish_ready_shorts(
                store=store, dry_run=False, allow_video_reuse=True, limit=1
            )
        else:
            out["steps"]["shorts_publish"] = {
                "skipped": True,
                "reason": "youtube_shorts_clip not allowlisted",
            }
    except Exception as exc:  # noqa: BLE001
        out["steps"]["shorts"] = {"error": str(exc)[:400]}
        logger.warning("shorts factory step failed: %s", exc)

    ledger.write(
        agent="sleep_factory",
        problem="beat complete",
        action="policy+watchdog+farm+competitors?/schedule+harvest?/pick",
        severity="info",
        extra={"summary": {k: _short(v) for k, v in out["steps"].items()}},
    )

    # 6) One batch digest email (not per video / per ledger warn). Safe no-op when
    # no new private/failed or farms still inflight (unless > digest_max_hours).
    digest_cfg = beat_cfg.get("email_digest")
    if digest_cfg is False:
        out["steps"]["email_digest"] = {"skipped": True, "reason": "disabled in config"}
    else:
        try:
            max_h = float(
                beat_cfg.get("digest_max_hours")
                if beat_cfg.get("digest_max_hours") is not None
                else 6
            )
            out["steps"]["email_digest"] = ledger.maybe_send_digest(max_hours=max_h)
        except Exception as exc:  # noqa: BLE001
            out["steps"]["email_digest"] = {"ok": False, "error": str(exc)}
            logger.warning("email digest step failed safely: %s", exc)

    log_path = ROOT / "output" / "ops" / "sleep_factory_last.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out


def install_crontab_hint() -> str:
    root = str(ROOT)
    return (
        f"*/15 * * * * cd {root} && {root}/.venv/bin/python -m src.cli.sleep_factory beat "
        f">> {root}/output/ops/sleep_factory.log 2>&1\n"
        f"0 3 * * 1 cd {root} && {root}/.venv/bin/python -m src.cli.run_agents policy-refresh "
        f">> {root}/output/ops/sleep_factory.log 2>&1\n"
        f"15 3 * * 1 cd {root} && {root}/.venv/bin/python -m src.cli.run_agents competitors-refresh --force "
        f">> {root}/output/ops/sleep_factory.log 2>&1\n"
        f"5 * * * * cd {root} && {root}/.venv/bin/python -m src.cli.ceo_smm_beat "
        f">> {root}/output/ops/ceo_smm.log 2>&1\n"
    )


def install_crontab() -> Path:
    """Append sleep-beat lines to user crontab without wiping unrelated entries."""
    hint_lines = [ln for ln in install_crontab_hint().splitlines() if ln.strip()]
    markers = (
        "src.cli.sleep_factory beat",
        "src.cli.run_agents policy-refresh",
        "src.cli.run_agents competitors-refresh",
        "src.cli.ceo_smm_beat",
    )

    import subprocess

    existing = ""
    try:
        proc = subprocess.run(
            ["crontab", "-l"],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            existing = proc.stdout or ""
        elif "no crontab" not in (proc.stderr or "").lower():
            raise RuntimeError(proc.stderr or "crontab -l failed")
    except FileNotFoundError as exc:
        raise RuntimeError("crontab binary not available") from exc

    kept = [
        ln
        for ln in existing.splitlines()
        if not any(m in ln for m in markers)
    ]
    # Drop trailing blanks before append
    while kept and not kept[-1].strip():
        kept.pop()
    new_lines = kept + ([""] if kept else []) + hint_lines + [""]
    payload = "\n".join(new_lines)
    proc = subprocess.run(
        ["crontab", "-"],
        input=payload,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or "crontab install failed")
    out_path = ROOT / "output" / "ops" / "crontab_sleep_factory.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(payload, encoding="utf-8")
    return out_path


def _short(v: Any) -> Any:
    if isinstance(v, list):
        return {"count": len(v)}
    if isinstance(v, dict) and "error" in v:
        return {"error": str(v["error"])[:120]}
    return v if not isinstance(v, dict) else {k: v[k] for k in list(v)[:6]}


def _agents_cfg() -> dict[str, Any]:
    path = CONFIG_DIR / "agents_settings.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))
