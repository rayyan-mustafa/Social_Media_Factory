"""End-of-day digest — one email aggregating finance + SMM + Live (+ RAM).

Schedule: once per Asia/Karachi calendar day at/after 23:00 (CEO hourly beat
gates). Watchdog/CEO orchestrates send so Rayyan gets one proper EOD report.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.agents.ledger import OpsLedger
from src.agents.store import OpsStore

logger = logging.getLogger(__name__)

try:
    from src.services.settings import ROOT
except Exception:  # noqa: BLE001
    ROOT = Path(__file__).resolve().parents[2]

OPS = ROOT / "output" / "ops"
EOD_MD = OPS / "eod_digest.md"
EOD_JSON = OPS / "eod_digest.json"
EOD_STAMP = OPS / "eod_digest_last_day.txt"
KARACHI = ZoneInfo("Asia/Karachi")
CHANNELS = ("napstorian", "napping_historian")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def karachi_now(now: datetime | None = None) -> datetime:
    dt = now or _utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KARACHI)


def karachi_day(now: datetime | None = None) -> str:
    return karachi_now(now).strftime("%Y-%m-%d")


def eod_window_open(now: datetime | None = None) -> bool:
    """True at/after 23:00 Asia/Karachi."""
    return karachi_now(now).hour >= 23


def already_sent_today(now: datetime | None = None) -> bool:
    if not EOD_STAMP.is_file():
        return False
    try:
        return EOD_STAMP.read_text(encoding="utf-8").strip() == karachi_day(now)
    except OSError:
        return False


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _day_bounds_utc(day: str) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(f"{day}T00:00:00").replace(tzinfo=KARACHI)
    end = datetime.fromisoformat(f"{day}T23:59:59.999999").replace(tzinfo=KARACHI)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return rows


def _in_day(ts: str | None, start: datetime, end: datetime) -> bool:
    dt = _parse_iso(ts)
    if not dt:
        return False
    return start <= dt <= end


def collect_smm_changes_day(day: str | None = None) -> dict[str, Any]:
    """All SMM mutations for Karachi day — schedule, levers, thumbs, packaging, experiments."""
    day = day or karachi_day()
    start, end = _day_bounds_utc(day)
    by_channel: dict[str, dict[str, Any]] = {
        ch: {
            "schedule_hours": [],
            "editing_levers": [],
            "thumbnail_designs": [],
            "soft_packaging": [],
            "works": [],
            "fails": [],
            "experiments": [],
            "prompt_patches": [],
        }
        for ch in CHANNELS
    }

    def _bucket(ch: str | None) -> dict[str, Any] | None:
        key = str(ch or "").strip().lower()
        if key in by_channel:
            return by_channel[key]
        return None

    # Schedule / preferred hours from publish_schedule.json + CEO actions
    sched_path = ROOT / "config" / "publish_schedule.json"
    if sched_path.is_file():
        try:
            sched = json.loads(sched_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            sched = {}
        for ch in CHANNELS:
            entry = ((sched.get("channels") or {}).get(ch)) or {}
            updated = entry.get("preferred_hours_updated_at")
            if _in_day(str(updated or ""), start, end):
                by_channel[ch]["schedule_hours"].append(
                    {
                        "preferred_hours": entry.get("preferred_hours"),
                        "source": entry.get("preferred_hours_source"),
                        "updated_at": updated,
                    }
                )

    for row in _read_jsonl(OPS / "ceo_actions.jsonl"):
        if not _in_day(str(row.get("at") or ""), start, end):
            continue
        rtype = str(row.get("type") or "")
        ch = row.get("channel")
        bucket = _bucket(ch) if ch else None
        if rtype in {"competitor_schedule", "schedule_hours"} and bucket is not None:
            bucket["schedule_hours"].append(
                {
                    "preferred_hours": row.get("hours") or row.get("preferred_hours"),
                    "source": rtype,
                    "at": row.get("at"),
                }
            )
        elif rtype == "prompt_patch":
            # prompt patches may omit channel — infer from path
            path = str(row.get("path") or "")
            inferred = "napping_historian" if "napping_historian" in path else (
                "napstorian" if path else None
            )
            b = _bucket(inferred or ch)
            if b is not None:
                b["prompt_patches"].append(
                    {
                        "path": path,
                        "marker": row.get("marker"),
                        "ok": row.get("ok"),
                        "skipped": row.get("skipped"),
                        "at": row.get("at"),
                    }
                )
        elif rtype == "thumb_redesign" and bucket is not None:
            bucket["thumbnail_designs"].append(
                {
                    "map_vs_face": row.get("map_vs_face"),
                    "mood": row.get("mood"),
                    "n_refs": row.get("n_refs"),
                    "n_analyzed": row.get("n_analyzed"),
                    "at": row.get("at"),
                    "source": "ceo_actions",
                }
            )

    # Editing levers from premium_editor_log
    for row in _read_jsonl(OPS / "premium_editor_log.jsonl"):
        if not _in_day(str(row.get("ts") or ""), start, end):
            continue
        bucket = _bucket(row.get("channel"))
        if bucket is None:
            continue
        lever = row.get("lever")
        if lever == "thumbnail_redesign":
            bucket["thumbnail_designs"].append(
                {
                    "before_preset": row.get("before_preset"),
                    "after_preset": row.get("after_preset"),
                    "value": row.get("value"),
                    "competitor_video_ids": row.get("competitor_video_ids"),
                    "prompt_addendum_summary": row.get("prompt_addendum_summary"),
                    "at": row.get("ts"),
                    "source": "premium_editor_log",
                }
            )
        elif lever:
            bucket["editing_levers"].append(
                {
                    "lever": lever,
                    "value": row.get("value"),
                    "reds": row.get("reds"),
                    "dominant_form": row.get("dominant_form"),
                    "at": row.get("ts"),
                }
            )

    # Dedicated thumb redesign log (richest before→after)
    for row in _read_jsonl(OPS / "smm_thumb_redesign_log.jsonl"):
        if not _in_day(str(row.get("ts") or ""), start, end):
            continue
        bucket = _bucket(row.get("channel"))
        if bucket is None:
            continue
        before = row.get("before") or {}
        after = row.get("after") or {}
        refs = row.get("competitor_refs") or []
        bucket["thumbnail_designs"].append(
            {
                "before_style": {
                    "preset": before.get("preset"),
                    "map_vs_face": before.get("map_vs_face"),
                    "mood": before.get("mood"),
                    "text_color": before.get("text_color"),
                },
                "after_style": {
                    "preset": after.get("preset"),
                    "map_vs_face": after.get("map_vs_face"),
                    "mood": after.get("mood"),
                    "text_color": after.get("text_color"),
                    "word_count_max": after.get("word_count_max"),
                    "contrast_boost": after.get("contrast_boost"),
                },
                "prompt_addendum": (row.get("prompt_addendum") or "")[:500],
                "competitor_refs": [
                    {
                        "video_id": r.get("video_id"),
                        "title": (r.get("title") or "")[:60],
                        "thumb_url": r.get("thumb_url"),
                    }
                    for r in refs[:5]
                    if isinstance(r, dict)
                ],
                "at": row.get("ts"),
                "source": "smm_thumb_redesign_log",
            }
        )

    # Soft packaging from ops ledger.json
    try:
        ledger_path = OPS / "ledger.json"
        # Prefer raw path under OPS; OpsStore may use same file under output/ops
        rows: list = []
        if ledger_path.is_file():
            try:
                raw = json.loads(ledger_path.read_text(encoding="utf-8"))
                rows = raw if isinstance(raw, list) else []
            except json.JSONDecodeError:
                # Truncated/concatenated ledger — scan line-ish objects best-effort via store
                rows = []
        if not rows:
            try:
                store = OpsStore()
                ledger_rows = store._read("ledger.json")  # noqa: SLF001
                if isinstance(ledger_rows, list):
                    rows = ledger_rows
            except Exception:  # noqa: BLE001
                rows = []
        for ent in rows:
            if not isinstance(ent, dict):
                continue
            if not _in_day(str(ent.get("created_at") or ""), start, end):
                continue
            if str(ent.get("agent") or "") != "smm":
                continue
            problem = str(ent.get("problem") or "").lower()
            if "soft packaging" not in problem:
                continue
            extra = ent.get("extra") or {}
            ch = extra.get("channel") or "unknown"
            bucket = _bucket(ch)
            target = bucket if bucket is not None else by_channel.setdefault(
                ch,
                {
                    "schedule_hours": [],
                    "editing_levers": [],
                    "thumbnail_designs": [],
                    "soft_packaging": [],
                    "works": [],
                    "fails": [],
                    "experiments": [],
                    "prompt_patches": [],
                },
            )
            target["soft_packaging"].append(
                {
                    "title": extra.get("title") or ent.get("problem"),
                    "video_id": extra.get("video_id"),
                    "action": ent.get("action"),
                    "job_id": ent.get("job_id"),
                    "at": ent.get("created_at"),
                }
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("eod soft packaging collect failed: %s", exc)

    # Works / fails / experiments
    for path_name, key in (
        ("smm_works.jsonl", "works"),
        ("smm_fails.jsonl", "fails"),
        ("smm_experiments.jsonl", "experiments"),
    ):
        for row in _read_jsonl(OPS / path_name):
            ts = str(row.get("ts") or row.get("created_at") or row.get("applied_at") or "")
            if not _in_day(ts, start, end):
                continue
            bucket = _bucket(row.get("channel"))
            if bucket is None:
                # unscoped → both channels note under napstorian as shared? skip
                continue
            bucket[key].append(
                {
                    "lever": row.get("lever"),
                    "outcome": row.get("outcome") or row.get("status"),
                    "note": (row.get("note") or row.get("reason") or "")[:120],
                    "at": ts,
                }
            )

    return {
        "day": day,
        "channels": by_channel,
    }


def collect_finance_day(day: str | None = None) -> dict[str, Any]:
    """Per-video / total spend for Karachi day from spend.json + runpod cost log."""
    day = day or karachi_day()
    start, end = _day_bounds_utc(day)
    store = OpsStore()
    spend = store._read("spend.json")  # noqa: SLF001
    items = []
    total = 0.0
    by_category: dict[str, float] = {}
    channels_seen: set[str] = set()
    for it in spend.get("items") or []:
        at = _parse_iso(str(it.get("at") or ""))
        if not at or at < start or at > end:
            continue
        amt = float(it.get("amount_usd") or 0)
        total += amt
        cat = str(it.get("category") or "other")
        by_category[cat] = by_category.get(cat, 0.0) + amt
        row = {
            "id": it.get("id"),
            "category": cat,
            "amount_usd": round(amt, 4),
            "note": it.get("note"),
            "job_id": it.get("job_id"),
            "channel": it.get("channel"),
            "at": it.get("at"),
        }
        if row.get("channel"):
            channels_seen.add(str(row["channel"]))
        items.append(row)

    # RunPod JSONL audit (may have nulls — still list sessions)
    pod_sessions: list[dict[str, Any]] = []
    pod_path = OPS / "runpod_pod_costs.jsonl"
    if pod_path.is_file():
        try:
            for line in pod_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    ent = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_iso(str(ent.get("ts") or ent.get("ended_at") or ""))
                if not ts or ts < start or ts > end:
                    continue
                pod_sessions.append(
                    {
                        "pod_id": ent.get("pod_id"),
                        "gpu": ent.get("gpu"),
                        "estimated_usd": ent.get("estimated_usd"),
                        "minutes": ent.get("minutes"),
                        "ts": ent.get("ts"),
                    }
                )
        except OSError:
            pass

    # Finance agent live ticket (valuation context)
    finance_ticket: dict[str, Any] | None = None
    try:
        from src.agents.finance_agent import FinanceAgent

        finance_ticket = FinanceAgent().collect_live_params()
    except Exception as exc:  # noqa: BLE001
        finance_ticket = {"error": str(exc)[:200]}

    return {
        "day": day,
        "total_spend_usd": round(total, 4),
        "by_category": {k: round(v, 4) for k, v in sorted(by_category.items())},
        "per_item": items,
        "channels_involved": sorted(channels_seen) or list(CHANNELS),
        "runpod_sessions": pod_sessions,
        "finance_ticket": finance_ticket,
        "month_spend_total": round(float(store.month_spend_total()), 4),
    }


def collect_smm_day(day: str | None = None) -> dict[str, Any]:
    """Published video counts/status + projection vs actual from scorecard/goals."""
    day = day or karachi_day()
    start, end = _day_bounds_utc(day)
    store = OpsStore()
    published: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    for job in store.list_jobs():
        updated = _parse_iso(job.updated_at)
        created = _parse_iso(getattr(job, "created_at", None))
        ts = updated or created
        if not ts or ts < start or ts > end:
            # Also count public/private if publish meta day matches
            meta = job.meta or {}
            pub_at = _parse_iso(str(meta.get("published_at") or meta.get("public_at") or ""))
            if not pub_at or pub_at < start or pub_at > end:
                continue
            ts = pub_at
        ch = str((job.meta or {}).get("channel") or "unknown")
        st = str(job.status or "unknown")
        status_counts[st] = status_counts.get(st, 0) + 1
        if st in {"public", "private", "scheduled", "hold"}:
            published.append(
                {
                    "job_id": job.id,
                    "channel": ch,
                    "status": st,
                    "title": (job.title or "")[:80],
                    "video_id": job.video_id,
                    "at": ts.isoformat(),
                }
            )

    # Scorecard / projection
    projection: dict[str, Any] = {}
    try:
        from src.agents.ceo_smm import build_ceo_digest, load_or_seed_goals

        goals = load_or_seed_goals()
        digest = build_ceo_digest(actions=[], goals=goals)
        projection = {
            "channel_health": digest.get("channel_health"),
            "critical_channels": digest.get("critical_channels"),
            "goals": {
                k: goals.get(k)
                for k in ("ctr_pct_min", "avd_pct_min", "monthly_views_target")
                if k in (goals or {})
            }
            or goals,
        }
    except Exception as exc:  # noqa: BLE001
        projection = {"error": str(exc)[:200]}

    scorecard_path = OPS / "smm_yt_scorecard.md"
    return {
        "day": day,
        "n_touched": len(published),
        "status_counts": status_counts,
        "videos": published[:40],
        "projection": projection,
        "scorecard_path": str(scorecard_path) if scorecard_path.is_file() else None,
    }


def collect_live_status() -> dict[str, Any]:
    """Both channels Live snapshot + featured heads (no secrets)."""
    channels: dict[str, Any] = {}
    try:
        from src.streaming.vod_loop import CHANNELS as VL_CH, performance_issues, run_status

        status = run_status(ensure_playlist=False)
        for ch in VL_CH:
            row = (status.get("channels") or {}).get(ch) or {}
            perf = performance_issues(ch)
            channels[ch] = {
                "alive": row.get("alive"),
                "supervisor_alive": row.get("supervisor_alive"),
                "rtmp_ready": row.get("rtmp_ready"),
                "has_rtmp_key": row.get("has_rtmp_key"),
                "featured_media": (row.get("extra") or {}).get("featured_media"),
                "bitrate": ((row.get("extra") or {}).get("health") or {}).get(
                    "last_bitrate"
                ),
                "issues": perf.get("issues"),
                "ffmpeg_count": perf.get("ffmpeg_count"),
                "supervise_count": perf.get("supervise_count"),
            }
        # Distinct keys boolean (never print keys)
        keys_distinct = bool(
            channels.get("napstorian", {}).get("has_rtmp_key")
            and channels.get("napping_historian", {}).get("has_rtmp_key")
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300]}

    featured = {}
    feat_path = OPS / "smm_live_featured.json"
    if feat_path.is_file():
        try:
            featured = json.loads(feat_path.read_text(encoding="utf-8"))
        except Exception:
            featured = {}

    return {
        "channels": channels,
        "keys_configured_both": keys_distinct,
        "keys_policy": "two_keys_two_channels_never_merge",
        "featured": (featured.get("channels") if isinstance(featured, dict) else {}),
        "ts": _utc_now().isoformat(),
    }


def collect_ram_status() -> dict[str, Any]:
    try:
        from src.runpod.ram_guard import LAST_PATH, is_ram_unhealthy, load_policy, read_meminfo

        mem = read_meminfo()
        unhealthy, reason = is_ram_unhealthy(mem, load_policy())
        last = {}
        if LAST_PATH.is_file():
            try:
                last = json.loads(LAST_PATH.read_text(encoding="utf-8"))
            except Exception:
                last = {}
        return {
            "available_gb": mem.get("available_gb"),
            "available_percent": mem.get("available_percent"),
            "total_gb": mem.get("total_gb"),
            "unhealthy": unhealthy,
            "reason": reason,
            "last_actions": (last.get("actions") if isinstance(last, dict) else None),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}


def build_eod_payload(*, day: str | None = None) -> dict[str, Any]:
    day = day or karachi_day()
    payload = {
        "ok": True,
        "module": "eod_digest",
        "day_karachi": day,
        "ts": _utc_now().isoformat(),
        "finance": collect_finance_day(day),
        "smm": collect_smm_day(day),
        "smm_changes": collect_smm_changes_day(day),
        "live": collect_live_status(),
        "ram": collect_ram_status(),
    }
    return payload


def _format_smm_changes_md(smm_changes: dict[str, Any]) -> list[str]:
    """Readable per-channel SMM mutation report for the 11pm email."""
    lines = [
        "",
        "## SMM changes today (both channels)",
        "",
        "_Schedule hours · editing levers · thumbnail redesigns · soft packaging · "
        "experiments · prompt patches_",
        "",
    ]
    channels = smm_changes.get("channels") or {}
    for ch in CHANNELS:
        block = channels.get(ch) or {}
        lines.append(f"### {ch}")
        lines.append("")

        hours = block.get("schedule_hours") or []
        if hours:
            lines.append("**Schedule / hour changes**")
            for h in hours[-5:]:
                lines.append(
                    f"- preferred_hours=`{h.get('preferred_hours')}` "
                    f"source=`{h.get('source') or '—'}` at=`{h.get('updated_at') or h.get('at')}`"
                )
            lines.append("")
        else:
            lines.append("- Schedule/hours: _(no change)_")
            lines.append("")

        levers = block.get("editing_levers") or []
        if levers:
            lines.append("**Editing levers applied** (hook / SFX / overlays / length / motion)")
            for lv in levers[-8:]:
                lines.append(
                    f"- `{lv.get('lever')}` → `{json.dumps(lv.get('value'), default=str)[:120]}` "
                    f"reds=`{lv.get('reds') or []}`"
                )
            lines.append("")
        else:
            lines.append("- Editing levers: _(none)_")
            lines.append("")

        thumbs = block.get("thumbnail_designs") or []
        # Prefer richest redesign log rows
        rich = [t for t in thumbs if t.get("source") == "smm_thumb_redesign_log"]
        show = rich or thumbs
        if show:
            lines.append("**Thumbnail design changes**")
            for t in show[-4:]:
                before = t.get("before_style") or {
                    "preset": t.get("before_preset"),
                }
                after = t.get("after_style") or t.get("value") or {
                    "preset": t.get("after_preset"),
                    "map_vs_face": t.get("map_vs_face"),
                    "mood": t.get("mood"),
                }
                lines.append(
                    f"- Style before→after: "
                    f"`{before.get('preset')}/{before.get('map_vs_face')}/{before.get('mood')}` → "
                    f"`{after.get('preset')}/{after.get('map_vs_face')}/{after.get('mood')}`"
                )
                add = t.get("prompt_addendum") or t.get("prompt_addendum_summary")
                if add:
                    summary = str(add).split("\n")[0][:160]
                    lines.append(f"  - Prompt addendum: {summary}")
                refs = t.get("competitor_refs") or []
                if not refs and t.get("competitor_video_ids"):
                    refs = [{"video_id": vid} for vid in (t.get("competitor_video_ids") or [])]
                if refs:
                    lines.append("  - Competitor refs:")
                    for r in refs[:4]:
                        vid = r.get("video_id")
                        title = (r.get("title") or "")[:50]
                        url = r.get("thumb_url") or (
                            f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else ""
                        )
                        lines.append(f"    - `{vid}` {title} — {url}")
            lines.append("")
        else:
            lines.append("- Thumbnail designs: _(no redesign today)_")
            lines.append("")

        soft = block.get("soft_packaging") or []
        if soft:
            lines.append("**Soft packaging (live videos)**")
            for s in soft[-6:]:
                lines.append(
                    f"- `{s.get('video_id')}` — {(s.get('title') or '')[:70]} "
                    f"({s.get('action') or 'updated'})"
                )
            lines.append("")
        else:
            lines.append("- Soft packaging: _(none)_")
            lines.append("")

        works = block.get("works") or []
        fails = block.get("fails") or []
        exps = block.get("experiments") or []
        lines.append("**Works / fails / experiments**")
        if works or fails or exps:
            for w in works[-5:]:
                lines.append(f"- WORK `{w.get('lever')}` — {w.get('note') or ''}")
            for f in fails[-5:]:
                lines.append(f"- FAIL `{f.get('lever')}` — {f.get('note') or ''}")
            for e in exps[-5:]:
                lines.append(
                    f"- EXP `{e.get('lever')}` status=`{e.get('outcome')}` — {e.get('note') or ''}"
                )
        else:
            lines.append("- _(none recorded)_")
        lines.append("")

        patches = block.get("prompt_patches") or []
        if patches:
            lines.append("**Prompt patches**")
            for p in patches[-6:]:
                lines.append(
                    f"- `{Path(str(p.get('path') or '')).name}` "
                    f"marker=`{p.get('marker')}` skipped=`{p.get('skipped')}`"
                )
            lines.append("")
        else:
            lines.append("- Prompt patches: _(none)_")
            lines.append("")

    return lines


def format_eod_markdown(payload: dict[str, Any]) -> str:
    fin = payload.get("finance") or {}
    smm = payload.get("smm") or {}
    smm_changes = payload.get("smm_changes") or {}
    live = payload.get("live") or {}
    ram = payload.get("ram") or {}
    lines = [
        f"# EOD digest — {payload.get('day_karachi')} (Asia/Karachi)",
        "",
        f"_Generated {_utc_now().isoformat()}_",
        "",
        "## Cost / finance",
        f"- **Total spend today:** `${fin.get('total_spend_usd', 0)}`",
        f"- **Month spend (ops store):** `${fin.get('month_spend_total', 0)}`",
        f"- **Channels involved:** `{', '.join(fin.get('channels_involved') or [])}`",
        f"- **By category:** `{json.dumps(fin.get('by_category') or {})}`",
        f"- **RunPod sessions today:** `{len(fin.get('runpod_sessions') or [])}`",
        "",
        "### Per-item spend (today)",
    ]
    items = fin.get("per_item") or []
    if not items:
        lines.append("- _(none recorded)_")
    else:
        for it in items[:30]:
            lines.append(
                f"- `${it.get('amount_usd')}` `{it.get('category')}` "
                f"job=`{it.get('job_id') or '—'}` — {it.get('note') or ''}"
            )

    lines += [
        "",
        "## SMM / publish",
        f"- **Videos touched today:** `{smm.get('n_touched')}`",
        f"- **Status counts:** `{json.dumps(smm.get('status_counts') or {})}`",
        f"- **Scorecard:** `{smm.get('scorecard_path') or 'n/a'}`",
    ]
    proj = smm.get("projection") or {}
    if proj.get("channel_health"):
        lines.append(f"- **Channel health:** `{json.dumps(proj.get('channel_health'))}`")
    if proj.get("critical_channels"):
        lines.append(f"- **Critical:** `{proj.get('critical_channels')}`")
    for v in (smm.get("videos") or [])[:15]:
        lines.append(
            f"- `{v.get('channel')}` **{v.get('status')}** — {v.get('title')}"
        )

    lines += _format_smm_changes_md(smm_changes)

    lines += ["", "## Live (both channels)"]
    for ch, row in (live.get("channels") or {}).items():
        lines.append(
            f"- **{ch}**: alive=`{row.get('alive')}` bitrate=`{row.get('bitrate')}` "
            f"issues=`{row.get('issues')}` ffmpeg_n=`{row.get('ffmpeg_count')}` "
            f"supervise_n=`{row.get('supervise_count')}`"
        )
        feat = ((live.get("featured") or {}).get(ch) or {})
        if feat.get("featured_path"):
            lines.append(
                f"  - featured: `{Path(str(feat['featured_path'])).name}` "
                f"views=`{feat.get('featured_views')}`"
            )
    lines.append(
        f"- **Keys policy:** `{live.get('keys_policy')}` "
        f"both_configured=`{live.get('keys_configured_both')}`"
    )

    lines += [
        "",
        "## RAM",
        f"- available: `{ram.get('available_gb')}GB` ({ram.get('available_percent')}%) "
        f"/ total `{ram.get('total_gb')}GB`",
        f"- unhealthy: `{ram.get('unhealthy')}` reason=`{ram.get('reason')}`",
        f"- last_actions: `{ram.get('last_actions')}`",
        "",
        "_One EOD email — Live/RAM criticals still use coalesce alerts separately._",
        "",
    ]
    return "\n".join(lines)


def maybe_send_eod_digest(
    *,
    force: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build + optionally email EOD once per Karachi day after 23:00."""
    now = now or _utc_now()
    day = karachi_day(now)
    out: dict[str, Any] = {
        "ok": True,
        "module": "eod_digest",
        "day": day,
        "sent_email": False,
    }
    if not force and not dry_run:
        if not eod_window_open(now):
            out["skipped"] = True
            out["reason"] = "before_23_karachi"
            return out
        if already_sent_today(now):
            out["skipped"] = True
            out["reason"] = "already_sent_today"
            return out

    payload = build_eod_payload(day=day)
    body = format_eod_markdown(payload)
    OPS.mkdir(parents=True, exist_ok=True)
    EOD_MD.write_text(body, encoding="utf-8")
    EOD_JSON.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    out["path_md"] = str(EOD_MD)
    out["path_json"] = str(EOD_JSON)
    out["payload_summary"] = {
        "total_spend_usd": (payload.get("finance") or {}).get("total_spend_usd"),
        "n_videos": (payload.get("smm") or {}).get("n_touched"),
        "live_alive": {
            ch: (row or {}).get("alive")
            for ch, row in ((payload.get("live") or {}).get("channels") or {}).items()
        },
        "ram_available_gb": (payload.get("ram") or {}).get("available_gb"),
    }

    subject = (
        f"[YT EOD] {day} — spend ${out['payload_summary']['total_spend_usd']} · "
        f"videos {out['payload_summary']['n_videos']}"
    )
    out["subject"] = subject

    if dry_run:
        out["dry_run"] = True
        out["note"] = "wrote markdown/json only"
        return out

    ledger = OpsLedger()
    if ledger.smtp_configured():
        sent, detail = ledger._send_smtp(subject, body)  # noqa: SLF001
        out["sent_email"] = sent
        out["send_detail"] = detail
    else:
        out["send_detail"] = "SMTP not configured — wrote eod_digest.md only"

    # Advance stamp even on SMTP fail to avoid hourly spam.
    try:
        EOD_STAMP.write_text(day + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("eod stamp failed: %s", exc)
    try:
        ledger.write(
            agent="eod_digest",
            problem="EOD digest",
            action=f"sent={out.get('sent_email')} day={day}",
            severity="info",
            extra=out.get("payload_summary"),
        )
    except Exception:  # noqa: BLE001
        pass
    return out
