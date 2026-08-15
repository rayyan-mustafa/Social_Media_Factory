"""CLI for autonomous ops agents.

Examples:
  .venv/bin/python -m src.cli.run_agents policy-refresh
  .venv/bin/python -m src.cli.run_agents trends-harvest
  .venv/bin/python -m src.cli.run_agents backfill-competitor-source
  .venv/bin/python -m src.cli.run_agents competitors-refresh
  .venv/bin/python -m src.cli.run_agents competitors-metrics
  .venv/bin/python -m src.cli.run_agents pick-titles
  .venv/bin/python -m src.cli.run_agents pick-titles --dry-run
  .venv/bin/python -m src.cli.run_agents sleep-beat
  .venv/bin/python -m src.cli.run_agents watchdog-scan
  .venv/bin/python -m src.cli.run_agents repair-watchdog
  .venv/bin/python -m src.cli.run_agents repair-watchdog --dry-run
  .venv/bin/python -m src.cli.run_agents smm-eval --title "What If..." --video-id abc
  .venv/bin/python -m src.cli.run_agents smm-scan
  .venv/bin/python -m src.cli.run_agents smm-scorecard
  .venv/bin/python -m src.cli.ceo_smm_beat
  .venv/bin/python -m src.cli.run_agents ceo-smm
  .venv/bin/python -m src.cli.run_agents smm-image-packs
  .venv/bin/python -m src.cli.run_agents cost-card
  .venv/bin/python -m src.cli.run_agents price-ticket --force
  .venv/bin/python -m src.cli.run_agents status
  .venv/bin/python -m src.cli.run_agents email-digest
  .venv/bin/python -m src.cli.run_agents email-digest --force
  .venv/bin/python -m src.cli.run_agents email-digest --dry-run
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.cost_guardian import CostGuardian  # noqa: E402
from src.agents.retention_auditor import RetentionAuditor  # noqa: E402
from src.agents.store import OpsStore  # noqa: E402
from src.agents.title_queue import TitleQueue  # noqa: E402
from src.agents.worker import (  # noqa: E402
    approve_public,
    pick_and_enqueue,
    run_backfill_competitor_source,
    run_competitors_metrics,
    run_competitors_refresh,
    run_policy_refresh,
    run_repair_watchdog,
    run_smm_eval,
    run_smm_learn_winners,
    run_smm_scan,
    run_trends_harvest,
    run_watchdog_scan,
)

app = typer.Typer(add_completion=False, help="Autonomous ops agents")
console = Console()


@app.command("policy-refresh")
def policy_refresh(force: bool = typer.Option(False, "--force")) -> None:
    from src.agents.policy_agent import PolicyAgent

    snap = PolicyAgent().refresh(force=force)
    console.print_json(data=snap.model_dump())


@app.command("trends-harvest")
def trends_harvest(
    dry_run: bool = typer.Option(False, "--dry-run"),
    channel: str | None = typer.Option(
        None,
        "--channel",
        "-c",
        help="napstorian | napping_historian — competitors file + sheet tab "
        "(default: napstorian / competitors.json)",
    ),
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="Max original titles to invent this run"
    ),
) -> None:
    """Invent ORIGINAL titles inspired by viral-ish competitor videos only.

    Channel-scoped sources:
      napstorian → config/competitors.json → sheet tab napstorian
      napping_historian → config/competitors_napping_historian.json → tab napping_historian

    Video bar knobs live in that channel's competitors JSON → power_filter:
      good_video_views (default 100k), good_video_vs_channel_avg (default 1.0),
      min_video_likes (optional, default 500). A video must clear
      views >= max(floor, channel_avg * ratio) when avg is known.
    """
    from src.agents.competitors_agent import competitors_path
    from src.agents.trends_agent import TrendsAgent, _load_competitors, _video_filter_cfg
    from src.services.youtube_channel_auth import normalize_youtube_channel

    ch = normalize_youtube_channel(channel) if channel else None
    src = competitors_path(ch)
    cfg = _load_competitors(ch)
    bar = _video_filter_cfg(cfg)
    comps = cfg.get("competitors") or []
    eligible = sum(1 for c in comps if c.get("eligible_for_titles") is True)
    console.print(
        f"[dim]channel[/dim] {ch or 'napstorian'}  "
        f"[dim]competitors[/dim] {src.name} "
        f"({len(comps)} loaded, {eligible} eligible)  "
        f"[dim]sheet tab[/dim] {ch or 'napstorian'}"
    )
    console.print(
        f"[dim]video bar:[/dim] views>=max({bar['good_video_views']}, "
        f"avg*{bar['good_video_vs_channel_avg']}) "
        f"likes>={bar.get('min_video_likes')}"
    )
    rows = run_trends_harvest(dry_run=dry_run, channel=ch, limit=limit)
    console.print(f"[green]titles[/green] {len(rows)}")
    for r in rows:
        flag = "OK" if r.get("policy_ok") else "BLOCK"
        src_url = r.get("competitor_source") or ""
        views = r.get("source_video_views") or ""
        console.print(f"  [{flag}] {r.get('trend_score')} {r.get('title')}")
        console.print(f"       source: {src_url}  views={views}")


@app.command("backfill-competitor-source")
def backfill_competitor_source(
    dry_run: bool = typer.Option(False, "--dry-run"),
    use_api: bool = typer.Option(
        False,
        "--use-api/--no-use-api",
        help="Optionally refresh inspiration via YouTube API (quota). "
        "Default uses competitors.json + last_inspiration cache only.",
    ),
) -> None:
    """Replace llm_original / placeholders with competitor video or channel URLs."""
    summary = run_backfill_competitor_source(dry_run=dry_run, use_api=use_api)
    console.print_json(data=summary)


@app.command("competitors-refresh")
def competitors_refresh(
    force: bool = typer.Option(
        False, "--force", help="Ignore interval / enabled-gate cadence"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Call APIs but do not write competitors JSON"
    ),
    channel: str | None = typer.Option(
        None,
        "--channel",
        "-c",
        help="napstorian | napping_historian (default: all existing competitor files)",
    ),
) -> None:
    """Discover/append niche competitors + search_queries (never Sheet titles)."""
    summary = run_competitors_refresh(force=force, dry_run=dry_run, channel=channel)
    console.print_json(data=summary)


@app.command("competitors-metrics")
def competitors_metrics(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Score channels but do not write JSON/Sheet"
    ),
    channel: str | None = typer.Option(
        None,
        "--channel",
        "-c",
        help="napstorian | napping_historian (default: all existing competitor files)",
    ),
) -> None:
    """Score competitors via YouTube Data API; sync Competitors sheet; set eligible_for_titles."""
    summary = run_competitors_metrics(dry_run=dry_run, channel=channel)
    console.print_json(data=summary)


@app.command("pick-titles")
def pick_titles(
    limit: int = typer.Option(1, "--limit", "-n"),
    run_pipeline: bool = typer.Option(
        False,
        "--run-pipeline/--no-run-pipeline",
        help="Spawn detached production farm (Script→…→private YouTube)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Preview next alternate-channel pick + profiles; does not create "
            "jobs or mutate sheet_pick_state"
        ),
    ),
) -> None:
    if dry_run:
        from src.agents.sheet_channels import (
            channel_default_profile,
            load_pick_state,
        )
        from src.agents.title_queue import TitleQueue

        q = TitleQueue()
        state = load_pick_state()
        preview = q.pick_approved(limit=limit, mutate_state=False)
        per_channel = {
            ch: {
                "ready": q.count_pending_ready(channel=ch),
                "default_profile": channel_default_profile(ch),
            }
            for ch in q.channels
        }
        console.print_json(
            data={
                "dry_run": True,
                "pick_state": {
                    "last_channel": state.get("last_channel"),
                    "policy": state.get("policy"),
                    "cold_start": state.get("cold_start"),
                    "last_farm_at_by_channel": state.get("last_farm_at_by_channel"),
                },
                "per_channel": per_channel,
                "next_picks": [
                    {
                        "channel": r.channel,
                        "row_index": r.row_index,
                        "title": r.title,
                        "trend_score": r.trend_score,
                        "profile": q.resolve_profile(r),
                        "job_id": r.job_id or "",
                    }
                    for r in preview
                ],
            }
        )
        return
    results = pick_and_enqueue(limit=limit, enqueue_pipeline=run_pipeline)
    console.print_json(data=results)


@app.command("sleep-beat")
def sleep_beat(
    harvest: bool | None = typer.Option(
        None,
        "--harvest/--no-harvest",
        help="Override agents_settings sleep_beat.harvest_if_queue_low",
    ),
    run_pipeline: bool | None = typer.Option(
        None,
        "--run-pipeline/--no-run-pipeline",
        help=(
            "Spawn real production farm for picked titles "
            "(default: sleep_beat config; overnight ON)"
        ),
    ),
    min_queued: int | None = typer.Option(
        None, "--min-queued", help="Override sleep_beat.min_queued_titles"
    ),
) -> None:
    """Alias for unattended sleep-factory beat (policy/watchdog/farm/harvest?/pick/schedule)."""
    from src.agents.sleep_factory import run_beat

    console.print_json(
        data=run_beat(
            harvest_if_queue_low=harvest,
            run_pipeline=run_pipeline,
            min_queued_titles=min_queued,
        )
    )


@app.command("watchdog-scan")
def watchdog_scan() -> None:
    actions = run_watchdog_scan()
    console.print_json(data=actions)


@app.command("repair-watchdog")
def repair_watchdog(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Classify + requeue playbooks but do not spawn farm processes",
    ),
) -> None:
    """Analyze failed farm jobs, repair, and resume the pipeline."""
    actions = run_repair_watchdog(spawn=not dry_run)
    console.print_json(data=actions)


@app.command("smm-eval")
def smm_eval(
    title: str = typer.Option(..., "--title"),
    video_id: str | None = typer.Option(None, "--video-id"),
    avd: float = typer.Option(35.0, "--avd"),
    ret60: float = typer.Option(55.0, "--ret60"),
    ctr: float = typer.Option(3.0, "--ctr"),
) -> None:
    data = run_smm_eval(
        video_id,
        title,
        metrics={
            "avd_pct": avd,
            "first_60s_retention_pct": ret60,
            "ctr_pct": ctr,
            "source": "cli",
        },
    )
    console.print_json(data=data)


@app.command("smm-scan")
def smm_scan() -> None:
    """Run SMM now: learn winners, watch public farm videos, apply allowed actions."""
    data = run_smm_scan()
    console.print_json(data=data)


@app.command("smm-scorecard")
def smm_scorecard(
    force: bool = typer.Option(
        True,
        "--force/--no-force",
        help="Write even if already wrote today",
    ),
) -> None:
    """Write daily YT quality scorecard (both channels)."""
    from src.agents.smm_agent import SocialMediaManager

    console.print_json(
        data=SocialMediaManager().maybe_write_daily_yt_scorecard(force=force)
    )


@app.command("ceo-smm")
def ceo_smm_cmd(
    force_scorecard: bool = typer.Option(False, "--force-scorecard"),
    force_email: bool = typer.Option(False, "--force-email"),
    force_competitors: bool = typer.Option(False, "--force-competitors"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """CEO-SMM full-auto beat (reach CTR, SOP heal, prompts, competitors, email)."""
    from src.agents.ceo_smm import run_ceo_beat

    console.print_json(
        data=run_ceo_beat(
            force_scorecard=force_scorecard,
            force_email=force_email,
            force_competitors=force_competitors,
            dry_run=dry_run,
        )
    )


@app.command("smm-image-packs")
def smm_image_packs(
    force: bool = typer.Option(False, "--force/--no-force"),
) -> None:
    """Emit ready-now Pinterest/IG/FB stills packs for farm jobs."""
    from src.agents.smm_agent import SocialMediaManager

    console.print_json(
        data=SocialMediaManager().maybe_emit_image_reuse_packs(force=force)
    )


@app.command("smm-unfreeze-status")
def smm_unfreeze_status() -> None:
    """Phase-2 unfreeze gate readiness (does not flip freeze flag)."""
    from src.agents.smm_agent import SocialMediaManager

    console.print_json(data=SocialMediaManager().unfreeze_gate_status())



@app.command("smm-learn-winners")
def smm_learn_winners(
    force: bool = typer.Option(True, "--force/--no-force"),
) -> None:
    """Pull top public videos from your channel into SMM benchmarks (positive patterns)."""
    data = run_smm_learn_winners(force=force)
    console.print_json(data=data)


@app.command("approve-title")
def approve_title(
    row: int = typer.Option(..., "--row", help="CSV/Sheet row index (2=first data)"),
    approved: bool = typer.Option(True, "--approved/--unapprove"),
    channel: str = typer.Option(
        "",
        "--channel",
        help="Sheet tab / channel (napstorian|napping_historian). Required if both have same row#.",
    ),
) -> None:
    q = TitleQueue()
    r = q.update_row(row, approved=approved, channel=channel or None)
    if not r:
        console.print("[red]row not found[/red]")
        raise typer.Exit(1)
    console.print_json(data=r.as_dict())


@app.command("ensure-sheet-channels")
def ensure_sheet_channels_cmd(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print planned channels/headers without API writes"
    ),
) -> None:
    """Rename Sheet1→napstorian, create napping_historian, drop format column, sync headers."""
    from src.agents.sheet_channels import (
        configured_sheet_channels,
        ensure_sheet_channels,
    )
    from src.agents.title_queue import DEFAULT_COLUMNS

    if dry_run:
        console.print_json(
            data={
                "channels": configured_sheet_channels(),
                "headers": DEFAULT_COLUMNS,
                "note": "format column removed — profile via channel default/retention",
            }
        )
        return
    console.print_json(data=ensure_sheet_channels())


@app.command("empire-status")
def empire_status_cmd(
    channel: str = typer.Option(
        "",
        "--channel",
        help="Optional staged channel to evaluate for activation (default: next in activation_order)",
    ),
) -> None:
    """Core harden bars + expansion gate + network revenue unlock status."""
    from src.agents.channel_empire import (
        evaluate_core_harden,
        evaluate_expansion_gate,
        load_empire,
        write_empire_ops_digest,
    )
    from src.agents.network_revenue import write_network_revenue_playbook

    harden = evaluate_core_harden()
    emp = load_empire()
    target = (channel or "").strip() or (
        (emp.get("activation_order") or ["art_mysteries"])[0]
    )
    gate = evaluate_expansion_gate(target)
    digest = write_empire_ops_digest()
    playbook = write_network_revenue_playbook()
    console.print_json(
        data={
            "harden": {
                "ready_for_wave1": harden.get("ready_for_wave1"),
                "archival_ok": harden.get("archival_ok"),
                "blockers": harden.get("blockers"),
                "videos_per_month_locked": harden.get("videos_per_month_locked"),
            },
            "expansion_gate": {
                "channel": gate.get("channel"),
                "allowed": gate.get("allowed"),
                "reasons": gate.get("reasons"),
                "skin": gate.get("skin"),
            },
            "digest": str(digest),
            "network_playbook": str(playbook),
        }
    )

@app.command("sheet-hygiene")
def sheet_hygiene_cmd(
    channel: str = typer.Option(
        "",
        "--channel",
        help="napstorian | napping_historian (default: both)",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report removals without rewriting sheet/CSV"
    ),
) -> None:
    """Strip historian What-If + policy-blocked queued junk (safe deletes only)."""
    from src.agents.sheet_hygiene import maybe_hygiene_idea_sheets

    ch = (channel or "").strip() or None
    channels = [ch] if ch else None
    console.print_json(
        data=maybe_hygiene_idea_sheets(channels=channels, dry_run=dry_run)
    )


@app.command("approve-public")
def approve_public_cmd(
    job_id: str = typer.Option(..., "--job-id"),
    arm: bool = typer.Option(True, "--arm/--no-arm", help="Arm YouTube publishAt"),
) -> None:
    """Require for all 15 videos/month before schedule is armed."""
    console.print_json(data=approve_public(job_id, arm_schedule=arm))


@app.command("schedule-status")
def schedule_status() -> None:
    from src.agents.schedule_agent import ScheduleAgent

    console.print_json(data=ScheduleAgent().status())


@app.command("set-publish-hour")
def set_publish_hour(
    hour: int = typer.Option(..., "--hour", min=0, max=23),
    channel: str | None = typer.Option(
        None,
        "--channel",
        "-c",
        help="napstorian | napping_historian (omit = legacy global approve)",
    ),
) -> None:
    """Apply publish hour (Asia/Karachi local). Always accepted — future slots only."""
    from src.agents.schedule_agent import ScheduleAgent

    console.print_json(
        data=ScheduleAgent().apply_approved_hour_override(hour, channel=channel)
    )


@app.command("cost-card")
def cost_card() -> None:
    from src.agents.finance_agent import FinanceAgent

    g = CostGuardian()
    pack = g.estimate_monthly_pack()
    pack_vol = g.estimate_monthly_pack(include_network_volume=True)
    br_r = g.estimate_video_breakdown(profile="retention")
    ticket = FinanceAgent(cost=g).compute_ticket()
    console.print_json(
        data={
            "rates": g.rate_card(),
            "formula_retention": br_r.get("formula"),
            "breakdown_retention": br_r,
            "breakdown_longform": g.estimate_video_breakdown(profile="longform"),
            "breakdown_epic": g.estimate_video_breakdown(profile="epic"),
            "estimate_retention_usd": g.estimate_video_usd(profile="retention"),
            "estimate_longform_usd": g.estimate_video_usd(profile="longform"),
            "estimate_epic_usd": g.estimate_video_usd(profile="epic"),
            "monthly_pack_2ch_x15_retention": pack,
            "monthly_pack_2ch_x15_retention_with_volume": pack_vol,
            "budget_check": g.check_can_start_job()[1],
            "price_ticket": ticket.get("headline"),
            "price_ticket_score": (ticket.get("score") or {}).get("overall_score"),
            "price_ticket_verdict": ticket.get("verdict"),
            "farm_armed": False,
            "defaults": {
                "RETENTION_PROFILE": "retention",
                "TTS_BACKEND": "kokoro",
                "seedream_thumbs_required": True,
                "runpod_visuals_mode": "pod",
                "runpod_seconds_per_still": 30.5,
                "runpod_pod_a5000_usd_per_hour": 0.16,
            },
        }
    )


@app.command("price-ticket")
def price_ticket(
    force: bool = typer.Option(
        False,
        "--force",
        help="Recompute even if within recheck_hours window",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Compute only; do not write price_ticket.json / CTO_STATUS",
    ),
    no_cto: bool = typer.Option(
        False,
        "--no-cto",
        help="Skip CTO_STATUS.md snippet update",
    ),
) -> None:
    """Open-market valuation ticket (floor / easy-ask / stretch).

    FinanceAgent pulls live CostGuardian + ops params and writes
    output/ops/price_ticket.json (+ jsonl). Watchdog rechecks daily.
    """
    from src.agents.finance_agent import FinanceAgent

    out = FinanceAgent().maybe_recompute(
        force=force or dry_run,
        dry_run=dry_run,
        update_cto=not no_cto,
    )
    console.print_json(data=out)


@app.command("email-digest")
def email_digest(
    force: bool = typer.Option(
        False,
        "--force",
        help="Send/write digest now even if farms are inflight or nothing new",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Write output/ops/last_digest.md only; do not email or update last_digest.json",
    ),
    max_hours: float = typer.Option(
        6.0,
        "--max-hours",
        help="Auto-send while farming if this many hours since last digest",
    ),
) -> None:
    """One combined overnight farm digest (not per-video / per-ledger warn)."""
    from src.agents.ledger import OpsLedger

    console.print_json(
        data=OpsLedger().maybe_send_digest(
            force=force, dry_run=dry_run, max_hours=max_hours
        )
    )


@app.command("status")
def status() -> None:
    store = OpsStore()
    q = TitleQueue()
    cost = CostGuardian(store)
    from src.agents.schedule_agent import ScheduleAgent

    sched = ScheduleAgent(store, queue=q).status()
    table = Table(title="Ops status")
    table.add_column("Key")
    table.add_column("Value")
    table.add_row("jobs", str(len(store.list_jobs())))
    table.add_row("events", str(len(store.list_events(limit=10000))))
    table.add_row("title_queue_rows", str(len(q.list_rows())))
    table.add_row("approved_ready", str(q.count_pending_ready()))
    for ch in q.channels:
        table.add_row(f"ready_{ch}", str(q.count_pending_ready(channel=ch)))
    table.add_row("buffer_private_scheduled", str(q.count_buffer()))
    table.add_row("month_spend_usd", f"{store.month_spend_total():.2f}")
    ok, msg = cost.check_can_start_job()
    table.add_row("budget", msg)
    table.add_row("est_retention_usd", str(cost.estimate_video_usd(profile="retention")))
    table.add_row("est_longform_usd", str(cost.estimate_video_usd(profile="longform")))
    table.add_row("est_epic_usd", str(cost.estimate_video_usd(profile="epic")))
    pack = cost.estimate_monthly_pack()
    table.add_row(
        "monthly_pack_2x15",
        f"${pack['total_usd']:.2f} (~{int(pack['total_pkr_approx'])} PKR)",
    )
    try:
        from src.agents.finance_agent import FinanceAgent

        ticket = FinanceAgent(store=store, cost=cost).compute_ticket()
        h = ticket.get("headline") or {}
        table.add_row(
            "price_ticket",
            (
                f"floor=${h.get('floor_usd'):,.0f} · "
                f"ask=${h.get('easy_ask_usd'):,.0f} · "
                f"stretch=${h.get('stretch_usd'):,.0f}"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        table.add_row("price_ticket", f"error: {exc}")
    snap = store.latest_policy_snapshot()
    table.add_row(
        "policy",
        f"v{snap.version} ok={snap.compile_ok}" if snap else "none",
    )
    table.add_row("publish_tz", str(sched.get("timezone")))
    table.add_row("publish_hour", str(sched.get("publish_hour_local")))
    table.add_row("next_slot", str(sched.get("next_slot_local")))
    table.add_row("pkt_vs_ny", str(sched.get("pkt_vs_ny")))
    console.print(table)
    console.print(f"ops dir: {store.root}")


@app.command("audit-script")
def audit_script(script_path: Path = typer.Argument(...)) -> None:
    from src.domain.models import ScriptResult

    script = ScriptResult.model_validate(
        json.loads(script_path.read_text(encoding="utf-8"))
    )
    result = RetentionAuditor().audit_script(script)
    console.print_json(data=result)


if __name__ == "__main__":
    app()
