"""YouTube Analytics API helpers — CTR / AVD / impressions for SMM scorecard.

Requires OAuth scope ``yt-analytics.readonly`` (added to AUTH_SCOPES).
Refresh cannot upgrade scopes — re-run ``youtube_auth`` per channel after AUTH_SCOPES
changes. Tokens that only have upload/force-ssl will 403; callers should soft-fail
with a clear re-auth note.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

ANALYTICS_READONLY_SCOPE = (
    "https://www.googleapis.com/auth/yt-analytics.readonly"
)

# Core metrics (always available when Analytics API is enabled).
VIDEO_METRICS_CORE = (
    "views,"
    "estimatedMinutesWatched,"
    "averageViewDuration,"
    "averageViewPercentage,"
    "subscribersGained,"
    "subscribersLost"
)

# Full set including packaging CTR — some Brand Accounts return
# 400 Unknown identifier for impressions / impressionClickThroughRate.
VIDEO_METRICS = (
    "views,"
    "estimatedMinutesWatched,"
    "averageViewDuration,"
    "averageViewPercentage,"
    "impressions,"
    "impressionClickThroughRate,"
    "subscribersGained,"
    "subscribersLost"
)

CTR_UNAVAILABLE_NOTE = (
    "Analytics packaging CTR metrics removed "
    "(impressions/impressionClickThroughRate → 400 Unknown identifier); "
    "core views/AVD used; thumbnail CTR requires Reporting reach reports"
)

REAUTH_HINT = (
    "Re-auth with Analytics scope:\n"
    "  .venv/bin/python -m src.cli.youtube_auth --channel {channel} --print-url\n"
    "  .venv/bin/python -m src.cli.youtube_auth --channel {channel} "
    '--complete "PASTE_REDIRECT_URL"\n'
    "Enable YouTube Analytics API in the same GCP project as the OAuth client."
)


def token_has_analytics_scope(scopes: list[str] | None) -> bool:
    have = {str(s).strip() for s in (scopes or []) if str(s).strip()}
    return ANALYTICS_READONLY_SCOPE in have


def parse_analytics_report(
    report: dict[str, Any] | None,
    *,
    video_id: str | None = None,
    duration_s: float | None = None,
) -> dict[str, Any]:
    """Map Analytics ``reports.query`` payload → SMM metric fields.

    CTR: ``impressionClickThroughRate`` (API returns a percentage, e.g. 4.5).
    AVD%: prefer ``averageViewPercentage``; else derive from
    ``averageViewDuration`` / ``duration_s``.
    """
    out: dict[str, Any] = {
        "ctr_pct": None,
        "avd_pct": None,
        "views": None,
        "impressions": None,
        "average_view_duration_s": None,
        "estimated_minutes_watched": None,
        "subscribers_gained": None,
        "subscribers_lost": None,
        "source": "youtube_analytics",
    }
    if not report:
        return out

    headers = report.get("columnHeaders") or []
    names = [str(h.get("name") or "") for h in headers]
    rows = report.get("rows") or []
    if not names or not rows:
        out["note"] = "analytics_empty_rows"
        return out

    # Prefer exact video match when dimension=video is present.
    chosen: list[Any] | None = None
    if "video" in names and video_id:
        vi = names.index("video")
        for row in rows:
            if len(row) > vi and str(row[vi]) == str(video_id):
                chosen = list(row)
                break
    if chosen is None:
        chosen = list(rows[0])

    def _cell(name: str) -> Any:
        if name not in names:
            return None
        i = names.index(name)
        if i >= len(chosen):
            return None
        return chosen[i]

    def _num(name: str) -> float | None:
        raw = _cell(name)
        if raw is None or raw == "":
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    views = _num("views")
    if views is not None:
        out["views"] = int(views)

    impressions = _num("impressions")
    if impressions is not None:
        out["impressions"] = int(impressions)

    avd_s = _num("averageViewDuration")
    if avd_s is not None:
        out["average_view_duration_s"] = float(avd_s)

    minutes = _num("estimatedMinutesWatched")
    if minutes is not None:
        out["estimated_minutes_watched"] = float(minutes)

    gained = _num("subscribersGained")
    if gained is not None:
        out["subscribers_gained"] = int(gained)
    lost = _num("subscribersLost")
    if lost is not None:
        out["subscribers_lost"] = int(lost)

    ctr = _num("impressionClickThroughRate")
    if ctr is not None:
        # Guard: rare clients return 0–1 ratio; typical API is already %.
        if 0.0 <= ctr <= 1.0 and (impressions or 0) >= 50:
            out["ctr_pct"] = round(ctr * 100.0, 3)
            out["ctr_unit_note"] = "scaled_from_ratio"
        else:
            out["ctr_pct"] = round(ctr, 3)

    avd_pct = _num("averageViewPercentage")
    if avd_pct is not None:
        out["avd_pct"] = round(float(avd_pct), 3)
    elif avd_s is not None and duration_s and float(duration_s) > 0:
        out["avd_pct"] = round(100.0 * float(avd_s) / float(duration_s), 3)
        out["avd_derived"] = True

    return out


def analytics_date_window(
    *,
    end: date | None = None,
    lookback_days: int = 90,
    start: date | None = None,
) -> tuple[str, str]:
    """Return (startDate, endDate) ISO strings for reports.query."""
    end_d = end or datetime.now(timezone.utc).date()
    if start is not None:
        start_d = start
    else:
        start_d = end_d - timedelta(days=max(1, int(lookback_days)))
    if start_d > end_d:
        start_d = end_d
    return start_d.isoformat(), end_d.isoformat()


def build_youtube_analytics_client(credentials: Any):
    """Build youtubeAnalytics v2 discovery client."""
    from googleapiclient.discovery import build

    return build(
        "youtubeAnalytics",
        "v2",
        credentials=credentials,
        cache_discovery=False,
    )


def _query_video_report(
    analytics: Any,
    *,
    channel_id: str,
    start_date: str,
    end_date: str,
    metrics: str,
    video_id: str,
) -> dict[str, Any]:
    return (
        analytics.reports()
        .query(
            ids=f"channel=={channel_id}",
            startDate=start_date,
            endDate=end_date,
            metrics=metrics,
            dimensions="video",
            filters=f"video=={video_id}",
            maxResults=5,
        )
        .execute()
    )


def _elapsed_ratio_at_60s(duration_s: float | None) -> float | None:
    """Map wall-clock 60s onto Analytics ``elapsedVideoTimeRatio`` (0–1)."""
    try:
        dur = float(duration_s) if duration_s is not None else 0.0
    except (TypeError, ValueError):
        return None
    if dur <= 0:
        return None
    return min(1.0, 60.0 / dur)


def parse_audience_watch_ratio_at_elapsed(
    report: dict[str, Any] | None,
    *,
    target_elapsed_ratio: float,
) -> dict[str, Any]:
    """Pick audienceWatchRatio nearest to ``target_elapsed_ratio``.

    Returns ``first_60s_retention_pct`` as percentage (0–100) when a row exists.
    """
    out: dict[str, Any] = {
        "first_60s_retention_pct": None,
        "audience_watch_ratio": None,
        "elapsed_video_time_ratio": None,
        "source": "youtube_analytics_audience_retention",
    }
    if not report:
        out["note"] = "audience_retention_empty_report"
        return out
    headers = report.get("columnHeaders") or []
    names = [str(h.get("name") or "") for h in headers]
    rows = report.get("rows") or []
    if "elapsedVideoTimeRatio" not in names or "audienceWatchRatio" not in names:
        out["note"] = f"audience_retention_missing_cols headers={names}"
        return out
    if not rows:
        out["note"] = "audience_retention_empty_rows"
        return out
    ei = names.index("elapsedVideoTimeRatio")
    ai = names.index("audienceWatchRatio")
    best: tuple[float, float, float] | None = None  # dist, elapsed, ratio
    for row in rows:
        if len(row) <= max(ei, ai):
            continue
        try:
            elapsed = float(row[ei])
            ratio = float(row[ai])
        except (TypeError, ValueError):
            continue
        dist = abs(elapsed - float(target_elapsed_ratio))
        if best is None or dist < best[0]:
            best = (dist, elapsed, ratio)
    if best is None:
        out["note"] = "audience_retention_unparseable"
        return out
    _dist, elapsed, ratio = best
    # audienceWatchRatio is typically 0–1; Studio shows %.
    pct = ratio * 100.0 if ratio <= 1.5 else ratio
    out["elapsed_video_time_ratio"] = elapsed
    out["audience_watch_ratio"] = ratio
    out["first_60s_retention_pct"] = round(pct, 2)
    out["target_elapsed_ratio"] = float(target_elapsed_ratio)
    return out


def fetch_first_60s_retention(
    *,
    analytics: Any,
    channel_id: str,
    video_id: str,
    start_date: str,
    end_date: str,
    duration_s: float | None,
) -> dict[str, Any]:
    """Query Analytics audience retention near the first 60 seconds.

    Supported query shape (requires video filter):
    metrics=audienceWatchRatio, dimensions=elapsedVideoTimeRatio.
    Soft-fails with a note when rows are empty (common for new/low-traffic VODs).
    """
    target = _elapsed_ratio_at_60s(duration_s)
    base: dict[str, Any] = {
        "first_60s_retention_pct": None,
        "source": "youtube_analytics_audience_retention",
        "target_elapsed_ratio": target,
    }
    if target is None:
        base["note"] = "first_60s_needs_duration_s"
        return base
    try:
        report = (
            analytics.reports()
            .query(
                ids=f"channel=={channel_id}",
                startDate=start_date,
                endDate=end_date,
                metrics="audienceWatchRatio",
                dimensions="elapsedVideoTimeRatio",
                filters=f"video=={video_id}",
                maxResults=200,
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "audience retention query failed video=%s: %s", video_id, exc
        )
        base["note"] = f"audience_retention_query_failed: {exc}"
        return base
    parsed = parse_audience_watch_ratio_at_elapsed(
        report, target_elapsed_ratio=target
    )
    base.update(parsed)
    return base


def fetch_video_analytics(
    *,
    credentials: Any,
    channel_id: str,
    video_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
    lookback_days: int = 90,
    duration_s: float | None = None,
) -> dict[str, Any]:
    """Query Analytics for one video. Raises on HTTP/API errors (caller handles).

    Tries full ``VIDEO_METRICS`` (incl. legacy impressions/CTR) first. On 400
    Unknown identifier, retries with ``VIDEO_METRICS_CORE`` so views/AVD still
    populate, then tries Reporting API reach reports for thumbnail CTR.
    """
    cid = (channel_id or "").strip()
    vid = (video_id or "").strip()
    if not cid or not vid:
        return {
            "ok": False,
            "error": "missing_channel_or_video_id",
            "ctr_pct": None,
            "avd_pct": None,
            "source": "youtube_analytics",
        }

    if start_date and end_date:
        start_s, end_s = start_date, end_date
    else:
        start_s, end_s = analytics_date_window(lookback_days=lookback_days)

    analytics = build_youtube_analytics_client(credentials)
    used_core_fallback = False
    try:
        report = _query_video_report(
            analytics,
            channel_id=cid,
            start_date=start_s,
            end_date=end_s,
            metrics=VIDEO_METRICS,
            video_id=vid,
        )
    except Exception as exc:
        if not is_unknown_metric_identifier_error(exc):
            raise
        logger.info(
            "analytics impressions/CTR rejected for video=%s (%s); "
            "falling back to core metrics",
            vid,
            exc,
        )
        report = _query_video_report(
            analytics,
            channel_id=cid,
            start_date=start_s,
            end_date=end_s,
            metrics=VIDEO_METRICS_CORE,
            video_id=vid,
        )
        used_core_fallback = True

    parsed = parse_analytics_report(
        report, video_id=vid, duration_s=duration_s
    )
    if used_core_fallback:
        parsed["ctr_pct"] = None
        parsed["impressions"] = None
        parsed["ctr_unavailable"] = True
        parsed["note"] = CTR_UNAVAILABLE_NOTE
        try:
            from src.services.youtube_reporting import fetch_video_reach_metrics

            reach = fetch_video_reach_metrics(credentials=credentials, video_id=vid)
            parsed["reporting"] = {
                "job_id": reach.get("job_id"),
                "job_action": reach.get("job_action"),
                "reports_available": reach.get("reports_available"),
                "note": reach.get("note"),
            }
            if reach.get("impressions") is not None:
                parsed["impressions"] = reach["impressions"]
            if reach.get("ctr_pct") is not None:
                parsed["ctr_pct"] = reach["ctr_pct"]
                parsed["ctr_unavailable"] = False
                parsed["ctr_source"] = "youtube_reporting_reach"
                parsed["note"] = (
                    "Analytics CTR metrics gone; "
                    "impressions/CTR from Reporting channel_reach_basic_a1"
                )
                parsed["source"] = "youtube_analytics+reporting_reach"
            elif reach.get("note"):
                parsed["note"] = f"{CTR_UNAVAILABLE_NOTE}; {reach['note']}"
                parsed["ctr_pending_reporting"] = True
                parsed["reports_available"] = reach.get("reports_available")
        except Exception as reach_exc:  # noqa: BLE001
            logger.info(
                "reporting reach CTR unavailable for video=%s: %s", vid, reach_exc
            )
            parsed["reporting_error"] = str(reach_exc)[:400]
            parsed["note"] = (
                f"{CTR_UNAVAILABLE_NOTE}; Reporting reach probe failed: {reach_exc}"
            )
            parsed["ctr_pending_reporting"] = True

    # First-60s: audienceWatchRatio at elapsed≈60/duration (advisory hook bar).
    if duration_s and parsed.get("first_60s_retention_pct") is None:
        try:
            ret60 = fetch_first_60s_retention(
                analytics=analytics,
                channel_id=cid,
                video_id=vid,
                start_date=start_s,
                end_date=end_s,
                duration_s=duration_s,
            )
            parsed["first_60s_probe"] = {
                "note": ret60.get("note"),
                "target_elapsed_ratio": ret60.get("target_elapsed_ratio"),
                "elapsed_video_time_ratio": ret60.get("elapsed_video_time_ratio"),
            }
            if ret60.get("first_60s_retention_pct") is not None:
                parsed["first_60s_retention_pct"] = ret60["first_60s_retention_pct"]
                parsed["first_60s_source"] = ret60.get("source")
            else:
                parsed["first_60s_unavailable"] = True
                if ret60.get("note"):
                    parsed["first_60s_note"] = ret60["note"]
        except Exception as ret_exc:  # noqa: BLE001
            logger.info("first-60s retention probe failed video=%s: %s", vid, ret_exc)
            parsed["first_60s_unavailable"] = True
            parsed["first_60s_note"] = f"first_60s_probe_failed: {ret_exc}"

    parsed["ok"] = True
    parsed["channel_id"] = cid
    parsed["video_id"] = vid
    parsed["start_date"] = start_s
    parsed["end_date"] = end_s
    return parsed


def is_unknown_metric_identifier_error(exc: BaseException) -> bool:
    """True when Analytics rejects a metric name (e.g. impressions)."""
    text = str(exc).lower()
    return (
        "unknown identifier" in text
        or "unknown metric" in text
        or "unknownmetric" in text.replace(" ", "")
    )


def is_analytics_scope_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "insufficient" in text
        or "insufficientpermissions" in text
        or ("403" in text and "scope" in text)
        or "accessNotConfigured" in str(exc)
        or "access not configured" in text
        or "youtube analytics api" in text
        or "has not been used" in text
    )


def scope_error_ops_note(channel: str, exc: BaseException) -> str:
    return (
        f"YouTube Analytics 403/scope for channel={channel}: {exc}. "
        + REAUTH_HINT.format(channel=channel)
    )
