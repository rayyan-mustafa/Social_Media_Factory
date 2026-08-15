"""Ops readiness + GREEN miss taxonomy for the RunPod watchdog.

CTO plan (2026-08-08):
  1) Taxonomize GREEN no-starts (GPU often fine — ops not ready)
  2) Queue buffer SLA + empty-sheet streak alerts
  3) Pre-window refill + non-GPU pre-stage (script/TTS) before prime GPU time

365d always-on: used by the existing */10 ``runpod_watchdog`` cron beat.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from src.runpod.cost_log import utcnow_iso
from src.services.settings import ROOT

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

MISSED_GREEN_PATH: Path = ROOT / "output" / "ops" / "green_missed_opportunities.jsonl"
OPS_READINESS_PATH: Path = ROOT / "output" / "ops" / "ops_readiness_last.json"
STREAK_ALERT_PATH: Path = ROOT / "output" / "ops" / "ops_readiness_streak_alert.json"

# Taxonomy codes for GREEN ticks that did not start GPU production
MISS_EMPTY_SHEET = "empty_sheet"
MISS_NO_VOICE_BUFFER = "no_voice_buffer"  # sheet empty AND no ready_for_stills
MISS_JOB_INFLIGHT = "job_inflight"
MISS_BUDGET = "budget_cap"
MISS_SMOKE = "smoke_gate"
MISS_COOLDOWN = "cooldown"
MISS_START_FAILED = "start_failed"
MISS_BUFFER_LOW = "buffer_low"  # some ready, but under SLA
MISS_OTHER = "other_skip"


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_truthy(name: str, default: str = "1") -> bool:
    raw = (os.getenv(name) or default).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def queue_buffer_target() -> int:
    """Min approved+queued (or voice-ready) titles we want banked 24/7."""
    return max(1, _env_int("QUEUE_BUFFER_TARGET", 3))


def prep_voice_buffer_target() -> int:
    """How many ready_for_stills jobs to bank ahead of GREEN (Phase A)."""
    # Prefer dedicated env; else align with queue buffer SLA.
    if (os.getenv("RUNPOD_PREP_VOICE_MAX") or "").strip():
        return max(0, _env_int("RUNPOD_PREP_VOICE_MAX", 3))
    return queue_buffer_target()


def pre_window_refill_minutes() -> int:
    """Minutes before a preferred window to force harvest + prep."""
    return max(0, _env_int("PRE_WINDOW_REFILL_MIN", 60))


def empty_sheet_alert_streak() -> int:
    """Alert after this many consecutive empty-sheet GREEN misses."""
    return max(1, _env_int("EMPTY_SHEET_MISS_ALERT_STREAK", 3))


def classify_green_no_start(
    *,
    classification: str | None,
    started: bool,
    pending_ready: int,
    voice_ready: int,
    busy: bool | None,
    reasons: list[str] | None,
    message: str = "",
    buffer_target: int | None = None,
) -> str | None:
    """Return taxonomy code if this was a GREEN tick with no GPU start, else None."""
    if started:
        return None
    if (classification or "").upper() != "GREEN":
        return None

    target = queue_buffer_target() if buffer_target is None else buffer_target
    blob = " ".join([message or ""] + [str(r) for r in (reasons or [])]).lower()
    ready_total = int(pending_ready or 0) + int(voice_ready or 0)

    if busy or "job already running" in blob or "gpu_lock" in blob:
        return MISS_JOB_INFLIGHT
    if "budget" in blob or "spend" in blob or "cap $" in blob:
        return MISS_BUDGET
    if "smoke" in blob:
        return MISS_SMOKE
    if "cooldown" in blob:
        return MISS_COOLDOWN
    if "start skipped" in blob or "spawn" in blob and "fail" in blob:
        return MISS_START_FAILED
    if pending_ready <= 0 and voice_ready <= 0:
        if "no pending" in blob or "no pending sheet" in blob or ready_total <= 0:
            return MISS_EMPTY_SHEET
    if ready_total > 0 and ready_total < target:
        return MISS_BUFFER_LOW
    if pending_ready <= 0 and voice_ready <= 0:
        return MISS_NO_VOICE_BUFFER
    return MISS_OTHER


def minutes_to_next_preferred_window(
    now: datetime | None = None,
) -> tuple[int | None, str | None, bool]:
    """Return (minutes_until_open, next_label, currently_in_window)."""
    from src.runpod.capacity import evaluate_schedule, schedule_windows_utc

    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    in_window, label, _advice = evaluate_schedule(now=now)
    if in_window:
        return 0, label or "preferred", True

    windows = schedule_windows_utc()
    if not windows:
        return None, None, False

    best_min: int | None = None
    best_label: str | None = None
    # Search today + tomorrow for next open
    for day_offset in (0, 1):
        day = (now + timedelta(days=day_offset)).date()
        for wlabel, start_t, end_t in windows:
            start_dt = datetime.combine(day, start_t, tzinfo=timezone.utc)
            if start_dt <= now:
                continue
            mins = int((start_dt - now).total_seconds() // 60)
            if best_min is None or mins < best_min:
                best_min = mins
                best_label = wlabel
    return best_min, best_label, False


def in_pre_window_refill(now: datetime | None = None) -> dict[str, Any]:
    """True when we should force harvest/prep before a preferred GPU window."""
    lead = pre_window_refill_minutes()
    mins, label, inside = minutes_to_next_preferred_window(now)
    if inside:
        return {
            "active": True,
            "reason": "inside_preferred_window",
            "minutes_to_window": 0,
            "window_label": label,
            "lead_minutes": lead,
        }
    if mins is None or lead <= 0:
        return {
            "active": False,
            "reason": "no_window_or_disabled",
            "minutes_to_window": mins,
            "window_label": label,
            "lead_minutes": lead,
        }
    active = mins <= lead
    return {
        "active": active,
        "reason": "approaching_window" if active else "outside_lead",
        "minutes_to_window": mins,
        "window_label": label,
        "lead_minutes": lead,
    }


def ops_readiness_snapshot(
    *,
    pending_ready: int,
    voice_ready: int,
    classification: str | None = None,
    in_window: bool = False,
    job_inflight: bool | None = None,
    pre_window: dict[str, Any] | None = None,
    miss_code: str | None = None,
    empty_sheet_miss_streak: int = 0,
) -> dict[str, Any]:
    target = queue_buffer_target()
    prep_target = prep_voice_buffer_target()
    ready_total = int(pending_ready) + int(voice_ready)
    deficit = max(0, target - ready_total)
    voice_deficit = max(0, prep_target - int(voice_ready))
    pre = pre_window or in_pre_window_refill()
    snap = {
        "at": utcnow_iso(),
        "queue_buffer_target": target,
        "prep_voice_target": prep_target,
        "pending_ready": int(pending_ready),
        "voice_ready": int(voice_ready),
        "ready_total": ready_total,
        "buffer_deficit": deficit,
        "voice_deficit": voice_deficit,
        "ops_ready": deficit <= 0,
        "gpu_line_ready": int(voice_ready) > 0,
        "classification": classification,
        "in_preferred_window": bool(in_window),
        "job_inflight": job_inflight,
        "pre_window": pre,
        "last_miss_code": miss_code,
        "empty_sheet_miss_streak": int(empty_sheet_miss_streak),
        "note": (
            "GPU line = Phase A script+TTS parked as ready_for_stills; "
            "sheet pending = approved+queued ideas not yet prepped"
        ),
    }
    try:
        OPS_READINESS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            OPS_READINESS_PATH.write_text(
                json.dumps(snap, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to write ops_readiness_last: %s", exc)
    return snap


def record_green_opportunity(
    *,
    miss_code: str | None,
    result_payload: dict[str, Any],
    window_label: str = "",
    busy: bool | None = None,
    busy_msg: str = "",
    ops: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Append taxonomy row for any GREEN no-start (and optional buffer_low note)."""
    if not miss_code:
        return None
    payload = {
        "event": "missed_green_opportunity",
        "miss_code": miss_code,
        "ts_utc": result_payload.get("at") or utcnow_iso(),
        "classification": result_payload.get("classification"),
        "decision": result_payload.get("decision"),
        "action": result_payload.get("action"),
        "reason": miss_code,
        "message": (result_payload.get("message") or "")[:300],
        "reasons": list(result_payload.get("reasons") or [])[:12],
        "pending_ready": int(result_payload.get("pending_ready") or 0),
        "voice_ready": int(result_payload.get("voice_ready") or 0),
        "in_preferred_window": bool(result_payload.get("in_window")),
        "window_label": window_label or None,
        "job_inflight": busy,
        "inflight_detail": busy_msg or None,
        "started": False,
        "dry_run": bool(result_payload.get("dry_run")),
        "ops_readiness": {
            "buffer_target": (ops or {}).get("queue_buffer_target"),
            "buffer_deficit": (ops or {}).get("buffer_deficit"),
            "voice_deficit": (ops or {}).get("voice_deficit"),
            "ops_ready": (ops or {}).get("ops_ready"),
            "pre_window": (ops or {}).get("pre_window"),
        }
        if ops
        else None,
        "green_streak": (result_payload.get("state") or {}).get("green_streak"),
        "empty_sheet_miss_streak": (ops or {}).get("empty_sheet_miss_streak"),
    }
    try:
        MISSED_GREEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with _LOCK:
            with MISSED_GREEN_PATH.open("a", encoding="utf-8") as fh:
                fh.write(line)
        logger.info(
            "missed_green_opportunity code=%s pending=%s voice_ready=%s "
            "in_window=%s inflight=%s deficit=%s",
            miss_code,
            payload["pending_ready"],
            payload["voice_ready"],
            payload["in_preferred_window"],
            payload["job_inflight"],
            (ops or {}).get("buffer_deficit"),
        )
        return payload
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to record missed_green_opportunity: %s", exc)
        return None


def update_empty_sheet_streak(prev: int, miss_code: str | None) -> int:
    if miss_code == MISS_EMPTY_SHEET:
        return int(prev or 0) + 1
    return 0


def maybe_alert_empty_sheet_streak(
    *,
    streak: int,
    ops: dict[str, Any],
) -> dict[str, Any] | None:
    """Fire critical alert when empty-sheet GREEN misses streak too long."""
    need = empty_sheet_alert_streak()
    if streak < need:
        return None
    # De-dupe: only alert when streak hits exact threshold or every +need
    if streak != need and streak % need != 0:
        return None

    subject = (
        f"[ops] Empty-sheet GREEN miss streak={streak} "
        f"(buffer target={ops.get('queue_buffer_target')})"
    )
    body = (
        "GPU was likely fine (GREEN), but ops readiness failed: "
        "no approved+queued sheet ideas and no voice-ready pre-stage jobs.\n\n"
        f"streak={streak}\n"
        f"pending_ready={ops.get('pending_ready')}\n"
        f"voice_ready={ops.get('voice_ready')}\n"
        f"buffer_deficit={ops.get('buffer_deficit')}\n"
        f"pre_window={ops.get('pre_window')}\n"
        f"at={ops.get('at')}\n\n"
        "Action: approve more sheet titles and/or let Phase A prep fill "
        "ready_for_stills before the next preferred GPU window.\n"
        "365d factory: refill is handled by */10 watchdog pre-window + harvest."
    )
    detail: dict[str, Any] = {
        "at": utcnow_iso(),
        "streak": streak,
        "subject": subject,
        "alerted": False,
    }
    try:
        from src.agents.ledger import OpsLedger
        from src.agents.store import OpsStore

        out = OpsLedger(OpsStore()).alert_now(subject=subject, body=body)
        detail["alerted"] = True
        detail["send_detail"] = out
    except Exception as exc:  # noqa: BLE001
        detail["error"] = str(exc)[:300]
        logger.warning("empty-sheet streak alert failed: %s", exc)
        try:
            STREAK_ALERT_PATH.parent.mkdir(parents=True, exist_ok=True)
            STREAK_ALERT_PATH.write_text(
                json.dumps({"subject": subject, "body": body, **detail}, indent=2)
                + "\n",
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            STREAK_ALERT_PATH.write_text(
                json.dumps(detail, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass
    logger.warning("empty_sheet_miss_streak_alert streak=%s", streak)
    return detail
