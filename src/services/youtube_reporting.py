"""YouTube Reporting API helpers — reach (thumbnail impressions / CTR).

Packaging CTR is **not** available via Analytics ``reports.query`` anymore
(``impressions`` / ``impressionClickThroughRate`` → 400 Unknown identifier).
Google documents thumbnail impressions/CTR only on **bulk Reach reports**:

- ``channel_reach_basic_a1``
- ``channel_reach_combined_a1``

Metrics (snake_case): ``video_thumbnail_impressions``,
``video_thumbnail_impressions_ctr``.

Same OAuth scope as Analytics: ``yt-analytics.readonly``.
Jobs are async — after ``jobs.create``, daily CSV files may take up to ~48h
before the first download appears.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

logger = logging.getLogger(__name__)

REACH_BASIC_REPORT_TYPE = "channel_reach_basic_a1"
REACH_JOB_NAME = "smm reach basic"

REPORTING_AWAITING_NOTE = (
    "Reporting API reach job exists but no daily report files yet "
    "(Google generates them asynchronously; often within ~24–48h of job create)"
)
REPORTING_NO_VIDEO_NOTE = (
    "Reporting reach CSV downloaded but video_id not present in recent rows"
)


def build_youtube_reporting_client(credentials: Any):
    from googleapiclient.discovery import build

    return build(
        "youtubereporting",
        "v1",
        credentials=credentials,
        cache_discovery=False,
    )


def ensure_reach_job(reporting: Any, *, name: str = REACH_JOB_NAME) -> dict[str, Any]:
    """Return existing channel_reach_basic_a1 job or create one."""
    jobs = (reporting.jobs().list().execute() or {}).get("jobs") or []
    for job in jobs:
        if job.get("reportTypeId") == REACH_BASIC_REPORT_TYPE:
            return {"action": "existing", "job": job}
    job = (
        reporting.jobs()
        .create(body={"reportTypeId": REACH_BASIC_REPORT_TYPE, "name": name})
        .execute()
    )
    return {"action": "created", "job": job}


def list_job_reports(reporting: Any, job_id: str) -> list[dict[str, Any]]:
    reps = (
        reporting.jobs().reports().list(jobId=job_id).execute() or {}
    ).get("reports") or []
    return sorted(
        reps,
        key=lambda r: str(r.get("createTime") or r.get("startTime") or ""),
        reverse=True,
    )


def download_report_csv(credentials: Any, download_url: str) -> str:
    """Download a Reporting API CSV via the report downloadUrl + bearer token."""
    from google.auth.transport.requests import Request
    import urllib.request

    creds = credentials
    if getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
        creds.refresh(Request())
    token = getattr(creds, "token", None)
    if not token:
        raise RuntimeError("missing OAuth access token for report download")
    req = urllib.request.Request(
        download_url,
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_reach_csv_for_video(
    csv_text: str,
    *,
    video_id: str,
) -> dict[str, Any]:
    """Aggregate thumbnail impressions/CTR for one video across CSV rows.

    CTR is impression-weighted when multiple day rows exist.
    """
    out: dict[str, Any] = {
        "impressions": None,
        "ctr_pct": None,
        "rows_matched": 0,
        "source": "youtube_reporting_reach",
    }
    if not (csv_text or "").strip():
        out["note"] = "empty_reach_csv"
        return out

    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        out["note"] = "reach_csv_no_headers"
        return out

    # Normalize header names.
    fields = {h: h for h in reader.fieldnames}
    lower = {h.lower().strip(): h for h in reader.fieldnames}

    def col(*names: str) -> str | None:
        for n in names:
            if n in fields:
                return n
            if n.lower() in lower:
                return lower[n.lower()]
        return None

    vid_col = col("video_id", "video")
    impr_col = col("video_thumbnail_impressions", "impressions")
    ctr_col = col("video_thumbnail_impressions_ctr", "impression_click_through_rate")
    if not vid_col or not impr_col:
        out["note"] = f"reach_csv_missing_cols headers={list(reader.fieldnames)}"
        return out

    want = str(video_id).strip()
    total_impr = 0.0
    weighted_ctr = 0.0
    matched = 0
    for row in reader:
        if str(row.get(vid_col) or "").strip() != want:
            continue
        matched += 1
        try:
            impr = float(row.get(impr_col) or 0)
        except (TypeError, ValueError):
            impr = 0.0
        total_impr += impr
        if ctr_col:
            try:
                ctr = float(row.get(ctr_col) or 0)
            except (TypeError, ValueError):
                ctr = 0.0
            # Reporting CTR is typically a ratio 0–1; Studio shows %.
            if ctr > 1.0:
                ctr_ratio = ctr / 100.0
            else:
                ctr_ratio = ctr
            weighted_ctr += ctr_ratio * impr

    out["rows_matched"] = matched
    if matched == 0:
        out["note"] = REPORTING_NO_VIDEO_NOTE
        return out
    out["impressions"] = int(total_impr)
    if total_impr > 0 and ctr_col:
        out["ctr_pct"] = round(100.0 * (weighted_ctr / total_impr), 3)
    elif matched and ctr_col:
        # Zero impressions — leave CTR null.
        out["ctr_pct"] = None
    return out


def fetch_video_reach_metrics(
    *,
    credentials: Any,
    video_id: str,
    max_reports: int = 14,
    job_name: str = REACH_JOB_NAME,
) -> dict[str, Any]:
    """Ensure reach job, download newest available CSVs, aggregate one video.

    Returns dict with ok/impressions/ctr_pct/note. Does not raise on empty
    reports — callers keep Analytics AVD and leave CTR null with a note.
    """
    vid = (video_id or "").strip()
    if not vid:
        return {"ok": False, "error": "missing_video_id", "source": "youtube_reporting_reach"}

    reporting = build_youtube_reporting_client(credentials)
    ensured = ensure_reach_job(reporting, name=job_name)
    job = ensured.get("job") or {}
    job_id = str(job.get("id") or "")
    base: dict[str, Any] = {
        "ok": True,
        "source": "youtube_reporting_reach",
        "report_type": REACH_BASIC_REPORT_TYPE,
        "job_id": job_id,
        "job_action": ensured.get("action"),
        "impressions": None,
        "ctr_pct": None,
        "video_id": vid,
    }
    if not job_id:
        base["ok"] = False
        base["error"] = "reach_job_missing_id"
        return base

    reports = list_job_reports(reporting, job_id)
    base["reports_available"] = len(reports)
    if not reports:
        base["note"] = REPORTING_AWAITING_NOTE
        base["ctr_unavailable"] = True
        return base

    # Merge newest N daily files (each file is one day).
    total_impr = 0
    weighted = 0.0
    files_used = 0
    last_note = None
    for rep in reports[: max(1, int(max_reports))]:
        url = rep.get("downloadUrl")
        if not url:
            continue
        try:
            csv_text = download_report_csv(credentials, str(url))
            parsed = parse_reach_csv_for_video(csv_text, video_id=vid)
        except Exception as exc:  # noqa: BLE001
            logger.info("reach report download/parse failed: %s", exc)
            last_note = f"download_error: {exc}"
            continue
        files_used += 1
        impr = parsed.get("impressions")
        ctr = parsed.get("ctr_pct")
        if impr is None:
            last_note = parsed.get("note") or last_note
            continue
        total_impr += int(impr)
        if ctr is not None and impr:
            weighted += (float(ctr) / 100.0) * float(impr)

    base["reports_used"] = files_used
    if total_impr <= 0:
        base["note"] = last_note or REPORTING_NO_VIDEO_NOTE
        base["ctr_unavailable"] = True
        return base

    base["impressions"] = int(total_impr)
    base["ctr_pct"] = round(100.0 * (weighted / float(total_impr)), 3)
    base["ctr_unavailable"] = False
    return base
