"""Ops farm priority queue — prefer listed job_ids before sheet picks.

File: ``output/ops/farm_priority_jobs.json``

Example::

    {
      "updated_at": "...",
      "reason": "stale scheduled new-format recompose",
      "jobs": [
        {"job_id": "job_…", "priority": 0, "phase": "visuals", "note": "Tudor"},
        {"job_id": "job_…", "priority": 1, "phase": "visuals", "note": "Mongols"}
      ]
    }

Lower ``priority`` number = earlier. Watchdog ``_start_one_farm_job`` drains
this list (spawn when GREEN / gpu slot free) before HOLD resume fall-through
to sheet ``pick_and_enqueue``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.store import OPS_DIR, OpsStore
from src.services.settings import ROOT

logger = logging.getLogger(__name__)

PRIORITY_PATH = OPS_DIR / "farm_priority_jobs.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def priority_path() -> Path:
    return PRIORITY_PATH


def read_farm_priority() -> dict[str, Any]:
    if not PRIORITY_PATH.is_file():
        return {"jobs": [], "updated_at": None, "reason": ""}
    try:
        data = json.loads(PRIORITY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("farm_priority read failed: %s", exc)
        return {"jobs": [], "updated_at": None, "reason": f"read_error:{exc}"}
    if not isinstance(data, dict):
        return {"jobs": [], "updated_at": None, "reason": "invalid"}
    jobs = data.get("jobs") or []
    if not isinstance(jobs, list):
        jobs = []
    return {
        "updated_at": data.get("updated_at"),
        "reason": data.get("reason") or "",
        "jobs": [j for j in jobs if isinstance(j, dict) and (j.get("job_id") or "").strip()],
    }


def write_farm_priority(
    jobs: list[dict[str, Any]],
    *,
    reason: str = "",
) -> dict[str, Any]:
    OPS_DIR.mkdir(parents=True, exist_ok=True)
    # Stable sort by priority then original order
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for i, row in enumerate(jobs):
        if not isinstance(row, dict):
            continue
        jid = str(row.get("job_id") or "").strip()
        if not jid:
            continue
        try:
            pri = int(row.get("priority", i))
        except (TypeError, ValueError):
            pri = i
        ranked.append((pri, i, {**row, "job_id": jid, "priority": pri}))
    ranked.sort(key=lambda t: (t[0], t[1]))
    payload = {
        "updated_at": _utc_now(),
        "reason": reason,
        "jobs": [t[2] for t in ranked],
    }
    PRIORITY_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def list_priority_job_ids(*, pending_only: bool = True) -> list[str]:
    """Ordered job_ids from the priority file."""
    data = read_farm_priority()
    out: list[str] = []
    for row in data.get("jobs") or []:
        jid = str(row.get("job_id") or "").strip()
        if not jid:
            continue
        if pending_only and row.get("done"):
            continue
        out.append(jid)
    return out


def mark_priority_done(job_id: str, *, note: str = "") -> dict[str, Any]:
    data = read_farm_priority()
    jobs = list(data.get("jobs") or [])
    changed = False
    for row in jobs:
        if str(row.get("job_id") or "").strip() == job_id:
            row["done"] = True
            row["done_at"] = _utc_now()
            if note:
                row["done_note"] = note
            changed = True
    if changed:
        return write_farm_priority(jobs, reason=str(data.get("reason") or ""))
    return data


def remove_priority_job(job_id: str) -> dict[str, Any]:
    data = read_farm_priority()
    jobs = [
        j
        for j in (data.get("jobs") or [])
        if str(j.get("job_id") or "").strip() != job_id
    ]
    return write_farm_priority(jobs, reason=str(data.get("reason") or ""))


def enqueue_priority_jobs(
    entries: list[dict[str, Any]],
    *,
    reason: str,
    store: OpsStore | None = None,
    replace: bool = True,
) -> dict[str, Any]:
    """Write priority file and stamp each job for farm pickup.

    Each entry: ``job_id`` required; optional ``priority``, ``phase``, ``note``,
    ``preserve_scheduled_video_id`` (default True when job already has video_id
    and status looks scheduled — set False for fresh upload + new video_id),
    ``schedule_at_local`` / ``publish_at_utc`` (locks that slot for schedule arm),
    ``public_approved`` (default True when locking a schedule slot).
    """
    store = store or OpsStore()
    existing = [] if replace else list(read_farm_priority().get("jobs") or [])
    by_id = {
        str(j.get("job_id") or "").strip(): dict(j)
        for j in existing
        if str(j.get("job_id") or "").strip()
    }

    stamped: list[dict[str, Any]] = []
    for i, raw in enumerate(entries):
        jid = str(raw.get("job_id") or "").strip()
        if not jid:
            continue
        job = store.get_job(jid)
        if not job:
            stamped.append({"job_id": jid, "ok": False, "error": "job_not_found"})
            continue

        from src.agents.smm_sop import stamp_new_format_sop_checklist

        meta = dict(job.meta or {})
        channel = (meta.get("channel") or meta.get("sheet_tab") or "napstorian").strip()
        phase = str(raw.get("phase") or meta.get("farm_phase") or "visuals").strip()
        preserve = raw.get("preserve_scheduled_video_id")
        if preserve is None:
            preserve = bool(job.video_id) and (job.status or "").lower() in {
                "scheduled",
                "private",
                "public",
            }
        preserve = bool(preserve)

        schedule_local = raw.get("schedule_at_local") or raw.get("scheduled_at_local")
        publish_at_utc = raw.get("publish_at_utc")
        if schedule_local and not publish_at_utc:
            try:
                from datetime import datetime, timezone
                from zoneinfo import ZoneInfo

                dt = datetime.fromisoformat(str(schedule_local).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ZoneInfo("Asia/Karachi"))
                publish_at_utc = dt.astimezone(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                schedule_local = dt.astimezone(ZoneInfo("Asia/Karachi")).isoformat()
            except ValueError:
                schedule_local = str(schedule_local)
        lock_slot = bool(schedule_local or publish_at_utc or raw.get("schedule_slot_locked"))

        public_approved = raw.get("public_approved")
        if public_approved is None:
            public_approved = True if (lock_slot and not preserve) else meta.get(
                "public_approved"
            )

        legacy_vid = job.video_id if (not preserve and job.video_id) else None
        if legacy_vid:
            meta["legacy_video_id"] = legacy_vid
            meta["legacy_watch_url"] = job.watch_url

        meta = stamp_new_format_sop_checklist(
            {
                **meta,
                "channel": channel,
                "farm_phase": phase,
                "resume": True,
                "farm_priority": True,
                "farm_priority_rank": int(raw.get("priority", i)),
                "preserve_scheduled_video_id": preserve,
                "skip_publish_keep_schedule": preserve,
                "needs_yt_media_replace": False if not preserve else bool(preserve),
                "priority_reason": reason,
                "priority_enqueued_at": _utc_now(),
                "skip_auto_farm": False,
                "schedule_slot_locked": lock_slot,
            },
            channel=channel,
            stage="farm_priority_enqueue",
        )
        if schedule_local:
            meta["scheduled_at_local"] = str(schedule_local)
        if publish_at_utc:
            meta["publish_at_utc"] = str(publish_at_utc)
            meta["force_publish_at_utc"] = str(publish_at_utc)
            meta["reserved_publish_at_utc"] = str(publish_at_utc)
        if public_approved is not None:
            meta["public_approved"] = bool(public_approved)
        # Fresh uploads: drop old YouTube identity so pipeline inserts a new video.
        if not preserve:
            meta.pop("needs_yt_media_replace", None)
            meta["note"] = (
                meta.get("note")
                or "farm_priority fresh upload — publish + schedule locked slot"
            )

        store.update_job(
            jid,
            status="queued",
            stage="queued",
            error=None,
            meta=meta,
            **(
                {"video_id": None, "watch_url": None}
                if not preserve
                else {}
            ),
        )
        row = {
            "job_id": jid,
            "priority": int(raw.get("priority", i)),
            "phase": phase,
            "note": raw.get("note") or (job.title or "")[:80],
            "video_id": None if not preserve else job.video_id,
            "legacy_video_id": legacy_vid or raw.get("legacy_video_id"),
            "preserve_scheduled_video_id": preserve,
            "publish": not preserve,
            "schedule_at_local": schedule_local,
            "publish_at_utc": publish_at_utc,
            "done": False,
        }
        by_id[jid] = row
        stamped.append({"job_id": jid, "ok": True, **row, "status": "queued"})

    # Keep caller order for new entries; merge with any retained
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in entries:
        jid = str(raw.get("job_id") or "").strip()
        if jid and jid in by_id and jid not in seen:
            ordered.append(by_id[jid])
            seen.add(jid)
    if not replace:
        for jid, row in by_id.items():
            if jid not in seen:
                ordered.append(row)

    payload = write_farm_priority(ordered, reason=reason)
    return {"priority_file": str(PRIORITY_PATH), "payload": payload, "stamped": stamped}


def next_priority_job(store: OpsStore | None = None) -> dict[str, Any] | None:
    """First unfinished priority job that is spawnable (queued / hold / farming-dead)."""
    from src.agents.farm import pid_alive

    store = store or OpsStore()
    data = read_farm_priority()
    for row in sorted(
        data.get("jobs") or [],
        key=lambda r: (int(r.get("priority", 999)), str(r.get("job_id") or "")),
    ):
        if row.get("done"):
            continue
        jid = str(row.get("job_id") or "").strip()
        job = store.get_job(jid)
        if not job:
            continue
        meta = job.meta or {}
        if meta.get("skip_auto_farm"):
            continue
        if pid_alive(meta.get("farm_pid")):
            # Already running — treat as claimed; do not spawn sibling.
            return {
                "job_id": jid,
                "phase": row.get("phase") or meta.get("farm_phase") or "visuals",
                "already_running": True,
                "row": row,
                "job": job,
            }
        st = (job.status or "").lower()
        if st in {"public", "done"} and not meta.get("farm_priority"):
            continue
        # Allow queued / hold / scheduled (if somehow not flipped) / farming without pid
        if st in {
            "queued",
            "hold",
            "failed",
            "scheduled",
            "farming",
            "ready_for_stills",
            "private",
        } or meta.get("farm_priority"):
            return {
                "job_id": jid,
                "phase": row.get("phase") or meta.get("farm_phase") or "visuals",
                "already_running": False,
                "row": row,
                "job": job,
            }
    return None


def queue_positions(store: OpsStore | None = None) -> list[dict[str, Any]]:
    """Human-readable status for each priority entry."""
    from src.agents.farm import pid_alive

    store = store or OpsStore()
    data = read_farm_priority()
    out: list[dict[str, Any]] = []
    for i, row in enumerate(
        sorted(
            data.get("jobs") or [],
            key=lambda r: (int(r.get("priority", 999)), str(r.get("job_id") or "")),
        )
    ):
        jid = str(row.get("job_id") or "").strip()
        job = store.get_job(jid) if jid else None
        meta = (job.meta if job else {}) or {}
        running = bool(job and pid_alive(meta.get("farm_pid")))
        out.append(
            {
                "position": i + 1,
                "job_id": jid,
                "priority": row.get("priority"),
                "done": bool(row.get("done")),
                "phase": row.get("phase") or meta.get("farm_phase"),
                "status": job.status if job else "missing",
                "stage": job.stage if job else None,
                "video_id": (job.video_id if job else None) or row.get("video_id"),
                "farm_pid_alive": running,
                "title": (job.title if job else row.get("note") or "")[:80],
                "preserve_scheduled_video_id": bool(
                    meta.get("preserve_scheduled_video_id")
                    or row.get("preserve_scheduled_video_id")
                ),
            }
        )
    return out
