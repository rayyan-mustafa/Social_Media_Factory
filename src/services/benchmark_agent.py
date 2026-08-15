"""Post-upload niche benchmarks + content-vs-distribution diagnosis.

Skeptical of shadowban: require impressions / browse-suggested signature.
Test-channel validation is recommend-only (Rayyan consent).
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.store import OPS_DIR

BENCHMARKS = OPS_DIR / "benchmarks.jsonl"
PERFORMANCE_LOG = OPS_DIR / "performance_log.jsonl"
TEST_CHANNEL_VALIDATION = OPS_DIR / "test_channel_validation.jsonl"
CONSENT_QUEUE = OPS_DIR / "smm_consent_queue.md"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _day_views(views_by_day: list[Any], day: int) -> float | None:
    """views_by_day may be list of ints (index=day-1) or dict-like days."""
    if not views_by_day:
        return None
    if isinstance(views_by_day, dict):
        for key in (day, str(day), f"day{day}"):
            if key in views_by_day:
                try:
                    return float(views_by_day[key])
                except (TypeError, ValueError):
                    return None
        return None
    idx = day - 1
    if 0 <= idx < len(views_by_day):
        try:
            return float(views_by_day[idx])
        except (TypeError, ValueError):
            return None
    return None


def set_benchmark(
    video_id: str,
    channel_id: str,
    historical_data: list[dict[str, Any]],
    *,
    niche_tag: str | None = None,
    current_subs: float | None = None,
) -> dict[str, Any]:
    """Median day1/3/7/14 curve from same niche_tag; scale by subs ratio."""
    niche = (niche_tag or "").strip().lower()
    peers = []
    for row in historical_data:
        tag = str(row.get("niche_tag") or "").strip().lower()
        if niche and tag and tag != niche:
            continue
        peers.append(row)
    if not peers:
        peers = list(historical_data[-10:])
    peers = peers[-10:]

    projected: dict[str, int] = {}
    for day in (1, 3, 7, 14):
        vals: list[float] = []
        for p in peers:
            v = _day_views(p.get("views_by_day") or [], day)
            if v is None:
                continue
            scale = 1.0
            if current_subs and p.get("subs_at_upload"):
                try:
                    scale = float(current_subs) / max(1.0, float(p["subs_at_upload"]))
                except (TypeError, ValueError):
                    scale = 1.0
            vals.append(v * scale)
        if vals:
            projected[f"day{day}"] = int(statistics.median(vals))
        else:
            projected[f"day{day}"] = 0

    out = {
        "ts": _now(),
        "video_id": video_id,
        "channel_id": channel_id,
        "niche_tag": niche or None,
        "projected": {
            "day1": projected["day1"],
            "day3": projected["day3"],
            "day7": projected["day7"],
            "day14": projected["day14"],
        },
        "n_peers": len(peers),
    }
    _append_jsonl(BENCHMARKS, out)
    return out


def check_performance(
    video_id: str,
    actual_views_today: int,
    day_number: int,
    *,
    projected_for_day: float | None = None,
    channel_id: str | None = None,
) -> dict[str, Any]:
    """Compare actual vs projected (±20%)."""
    projected = projected_for_day
    if projected is None:
        # Look up latest benchmark for video
        projected = 0.0
        if BENCHMARKS.is_file():
            for line in BENCHMARKS.read_text(encoding="utf-8").splitlines()[::-1]:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("video_id") != video_id:
                    continue
                key = f"day{int(day_number)}" if day_number in (1, 3, 7, 14) else None
                # interpolate roughly: use closest
                proj = (row.get("projected") or {})
                if key and key in proj:
                    projected = float(proj[key])
                else:
                    projected = float(proj.get("day7") or proj.get("day1") or 0)
                break

    projected = float(projected or 0)
    actual = float(actual_views_today)
    if projected <= 0:
        deviation = 0.0
        status = "inconclusive"
    else:
        deviation = (actual - projected) / projected * 100.0
        if abs(deviation) <= 20:
            status = "on_track"
        elif deviation < -20:
            status = "underperforming"
        else:
            status = "overperforming"

    out = {
        "ts": _now(),
        "video_id": video_id,
        "channel_id": channel_id,
        "day_number": day_number,
        "actual": actual,
        "projected": projected,
        "deviation_pct": round(deviation, 2),
        "status": status,
    }
    _append_jsonl(PERFORMANCE_LOG, out)
    return out


def _consecutive_under_days(video_id: str) -> int:
    if not PERFORMANCE_LOG.is_file():
        return 0
    rows = []
    for line in PERFORMANCE_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("video_id") == video_id:
            rows.append(row)
    rows = rows[-14:]
    n = 0
    for row in reversed(rows):
        if row.get("status") == "underperforming":
            n += 1
        else:
            break
    return n


def diagnose_underperformance(
    video_id: str,
    *,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Content checks first; distribution only with impressions/browse signature."""
    m = dict(metrics or {})
    evidence: list[str] = []

    ctr = m.get("ctr")
    ctr_avg = m.get("ctr_channel_avg")
    avd = m.get("avd_pct")
    avd_avg = m.get("avd_channel_avg")
    slot_ok = m.get("upload_slot_ok")
    impressions = m.get("impressions")
    impressions_avg = m.get("impressions_channel_avg")
    browse_pct = m.get("browse_suggested_pct")
    browse_avg = m.get("browse_suggested_channel_avg")

    content_bad = False
    if ctr is not None and ctr_avg is not None and float(ctr) < float(ctr_avg) * 0.8:
        content_bad = True
        evidence.append(f"CTR {ctr} << channel avg {ctr_avg}")
    if avd is not None and avd_avg is not None and float(avd) < float(avd_avg) * 0.8:
        content_bad = True
        evidence.append(f"AVD {avd} << channel avg {avd_avg}")
    if slot_ok is False:
        content_bad = True
        evidence.append("upload slot off historical best hours")

    if content_bad:
        verdict = "likely_content_issue"
    else:
        dist_sig = False
        if (
            impressions is not None
            and impressions_avg is not None
            and float(impressions_avg) > 0
            and float(impressions) < float(impressions_avg) * 0.5
        ):
            dist_sig = True
            evidence.append(
                f"impressions {impressions} << avg {impressions_avg} while content metrics OK"
            )
        if (
            browse_pct is not None
            and browse_avg is not None
            and float(browse_avg) > 0
            and float(browse_pct) < float(browse_avg) * 0.5
        ):
            dist_sig = True
            evidence.append(
                f"browse/suggested {browse_pct}% << avg {browse_avg}%"
            )
        if dist_sig:
            verdict = "likely_distribution_suppression"
        elif not m:
            verdict = "inconclusive_needs_more_data"
            evidence.append("insufficient metrics for diagnosis")
        else:
            verdict = "inconclusive_needs_more_data"
            evidence.append("content OK but no clear impressions/browse signature")

    out = {
        "ts": _now(),
        "video_id": video_id,
        "verdict": verdict,
        "evidence": evidence,
        "consecutive_under_days": _consecutive_under_days(video_id),
    }
    _append_jsonl(PERFORMANCE_LOG, {"event": "diagnosis", **out})
    return out


def flag_for_test_channel_validation(video_id: str) -> dict[str, Any]:
    """Recommend-only: do not auto-create channels."""
    rec = {
        "ts": _now(),
        "video_id": video_id,
        "recommendation": (
            "Upload a NEW same-tier piece (not the identical file) to one disposable "
            "test channel. Compare impressions/browse% vs Brand. Rayyan consent required."
        ),
        "auto_action": False,
        "status": "awaiting_rayyan_consent",
    }
    _append_jsonl(TEST_CHANNEL_VALIDATION, rec)
    CONSENT_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    block = (
        f"\n## Test channel validation — {video_id}\n"
        f"- Status: awaiting consent\n"
        f"- {rec['recommendation']}\n"
        f"- Accept: write `output/ops/smm_consent_accept_{video_id}.flag`\n"
    )
    with CONSENT_QUEUE.open("a", encoding="utf-8") as fh:
        fh.write(block)
    return rec


def daily_report(channel_id: str) -> str:
    lines = [
        f"# Benchmark daily report — {channel_id}",
        f"_Generated {_now()}_",
        "",
    ]
    by_video: dict[str, list[dict[str, Any]]] = {}
    if PERFORMANCE_LOG.is_file():
        for line in PERFORMANCE_LOG.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if channel_id and row.get("channel_id") not in (None, channel_id):
                continue
            vid = row.get("video_id")
            if not vid or row.get("event") == "diagnosis":
                continue
            by_video.setdefault(str(vid), []).append(row)

    if not by_video:
        lines.append("No videos in tracking window.")
        return "\n".join(lines) + "\n"

    for vid, rows in by_video.items():
        last = rows[-1]
        lines.append(
            f"- `{vid}` day={last.get('day_number')} status=**{last.get('status')}** "
            f"dev={last.get('deviation_pct')}% actual={last.get('actual')} "
            f"proj={last.get('projected')}"
        )
        if _consecutive_under_days(vid) >= 2:
            diag = diagnose_underperformance(vid)
            lines.append(f"  - diagnosis: `{diag.get('verdict')}` — {diag.get('evidence')}")
            if diag.get("verdict") == "likely_distribution_suppression":
                flag_for_test_channel_validation(vid)
                lines.append("  - test-channel recommendation queued (consent)")
    return "\n".join(lines) + "\n"


def maybe_diagnose_if_under(video_id: str, metrics: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if _consecutive_under_days(video_id) < 2:
        return None
    return diagnose_underperformance(video_id, metrics=metrics)
