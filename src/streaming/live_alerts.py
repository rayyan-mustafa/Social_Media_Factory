"""Live unresolved-error Gmail alerts with per-channel cooldown (anti-spam).

Watchdog / healthcheck always auto-resolves first. Email Rayyan only when:
  - fix attempts exhausted (N tries) / still broken after backoff window, OR
  - code reported success but verify still shows stream not alive / issues

Coalesce: at most one email per channel per calendar day (Asia/Karachi) unless
issues fingerprint changes to a new critical class after cooldown hours.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.agents.ledger import OpsLedger

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "output" / "ops"
STATE_PATH = OPS / "live_alert_state.json"
KARACHI = ZoneInfo("Asia/Karachi")

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_COOLDOWN_HOURS = 6.0
CRITICAL_ISSUES = frozenset(
    {
        "reconnect_storm",
        "supervise_without_encode",
        "rtmp_io_error",
        "rtmp_open_error",
        "dead_encode",
        "dual_ingest",
        "duplicate_supervise",
        "bitrate_collapse",
        "stall",
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _karachi_day(now: datetime | None = None) -> str:
    dt = now or _utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KARACHI).strftime("%Y-%m-%d")


def _max_attempts() -> int:
    raw = (os.getenv("LIVE_ALERT_MAX_ATTEMPTS") or str(DEFAULT_MAX_ATTEMPTS)).strip()
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_ATTEMPTS


def _cooldown_hours() -> float:
    raw = (os.getenv("LIVE_ALERT_COOLDOWN_HOURS") or str(DEFAULT_COOLDOWN_HOURS)).strip()
    try:
        return max(0.5, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_HOURS


def load_state() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {"channels": {}, "module": "live_alerts"}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"channels": {}, "module": "live_alerts"}
    if not isinstance(data, dict):
        return {"channels": {}, "module": "live_alerts"}
    data.setdefault("channels", {})
    return data


def save_state(state: dict[str, Any]) -> None:
    OPS.mkdir(parents=True, exist_ok=True)
    state = dict(state)
    state["updated_at"] = _utc_now().isoformat()
    state["module"] = "live_alerts"
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _issues_fingerprint(issues: list[str]) -> str:
    crit = sorted({i for i in issues if i in CRITICAL_ISSUES})
    return "+".join(crit) or "none"


def email_allowed(
    channel: str,
    *,
    fingerprint: str,
    state: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Return (allowed, reason) for sending a Live alert email."""
    now = now or _utc_now()
    st = state or load_state()
    row = (st.get("channels") or {}).get(channel) or {}
    day = _karachi_day(now)
    last_day = str(row.get("last_email_day") or "")
    last_fp = str(row.get("last_email_fingerprint") or "")
    last_at = str(row.get("last_email_at") or "")
    if last_day == day and last_fp == fingerprint:
        return False, "already_emailed_today_same_issues"
    if last_at:
        try:
            prev = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
            age_h = (now - prev).total_seconds() / 3600.0
            if age_h < _cooldown_hours() and last_fp == fingerprint:
                return False, f"cooldown_{age_h:.1f}h"
            # Different critical class within same day: still coalesce unless
            # cooldown elapsed (avoid */10 spam on oscillating symptoms).
            if last_day == day and age_h < _cooldown_hours():
                return False, f"same_day_cooldown_{age_h:.1f}h"
        except ValueError:
            pass
    return True, "ok"


def record_attempt(
    channel: str,
    *,
    issues: list[str],
    repaired: bool,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    st = state or load_state()
    channels = dict(st.get("channels") or {})
    row = dict(channels.get(channel) or {})
    fp = _issues_fingerprint(issues)
    if not issues:
        row["attempts"] = 0
        row["last_fingerprint"] = "none"
        row["last_healthy_at"] = _utc_now().isoformat()
    else:
        prev_fp = str(row.get("last_fingerprint") or "")
        attempts = int(row.get("attempts") or 0)
        if fp != prev_fp:
            attempts = 1
        else:
            attempts += 1
        row["attempts"] = attempts
        row["last_fingerprint"] = fp
        row["last_issues"] = list(issues)
        row["last_repaired"] = bool(repaired)
        row["last_attempt_at"] = _utc_now().isoformat()
    channels[channel] = row
    st["channels"] = channels
    save_state(st)
    return row


def should_escalate(
    channel: str,
    *,
    issues: list[str],
    alive: bool,
    repair_claimed_ok: bool,
    state: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Decide whether unresolved Live issues warrant an email."""
    crit = [i for i in issues if i in CRITICAL_ISSUES]
    if not crit and alive:
        return False, "healthy"
    st = state or load_state()
    row = (st.get("channels") or {}).get(channel) or {}
    attempts = int(row.get("attempts") or 0)
    # Claimed success but verify still broken → escalate immediately (cooldown still applies).
    if repair_claimed_ok and (not alive or crit):
        return True, "verify_still_broken_after_fix"
    if attempts >= _max_attempts() and (not alive or crit):
        return True, f"attempts_exhausted_{attempts}"
    return False, f"still_auto_resolving_attempts_{attempts}"


def format_live_alert_body(
    *,
    channel: str,
    issues: list[str],
    reason: str,
    perf: dict[str, Any],
    attempts: int,
) -> str:
    lines = [
        f"# Live unresolved — {channel}",
        "",
        f"- escalate_reason: `{reason}`",
        f"- issues: `{', '.join(issues) or 'none'}`",
        f"- attempts: `{attempts}`",
        f"- alive: `{perf.get('alive')}` supervisor_alive=`{perf.get('supervisor_alive')}`",
        f"- ffmpeg_count: `{perf.get('ffmpeg_count')}` supervise_count=`{perf.get('supervise_count')}`",
        f"- storm: `{perf.get('storm_detail')}`",
        f"- stall: `{perf.get('stall_detail')}`",
        "",
        "Auto-resolve already tried (single-supervisor model: systemd `vod-loop@CHANNEL`).",
        "Keys stay **one per channel** (napstorian ≠ napping_historian) — do not merge.",
        "",
        "Ops: `sudo systemctl status vod-loop@"
        + channel
        + "` · `python -m src.cli.vod_loop status --json`",
    ]
    return "\n".join(lines) + "\n"


def maybe_email_unresolved_live(
    channel: str,
    *,
    repair_claimed_ok: bool = False,
    force: bool = False,
    dry_run: bool = False,
    ledger: OpsLedger | None = None,
) -> dict[str, Any]:
    """Verify Live health; email only if unresolved + cooldown allows."""
    from src.streaming.vod_loop import performance_issues

    perf = performance_issues(channel)
    issues = list(perf.get("issues") or [])
    alive = bool(perf.get("alive"))
    row = record_attempt(
        channel, issues=issues, repaired=repair_claimed_ok
    )
    out: dict[str, Any] = {
        "channel": channel,
        "issues": issues,
        "alive": alive,
        "attempts": int(row.get("attempts") or 0),
        "sent_email": False,
    }
    escalate, why = should_escalate(
        channel,
        issues=issues,
        alive=alive,
        repair_claimed_ok=repair_claimed_ok,
    )
    out["escalate"] = escalate
    out["escalate_reason"] = why
    if not escalate and not force:
        out["skipped"] = True
        out["reason"] = why
        return out
    fp = _issues_fingerprint(issues)
    allowed, allow_why = email_allowed(channel, fingerprint=fp)
    out["email_gate"] = allow_why
    if not allowed and not force:
        out["skipped"] = True
        out["reason"] = allow_why
        return out
    subject = f"[YT Live] {channel} unresolved: {fp or 'dead'}"
    body = format_live_alert_body(
        channel=channel,
        issues=issues,
        reason=why if not force else "force",
        perf=perf,
        attempts=int(row.get("attempts") or 0),
    )
    if dry_run:
        out["dry_run"] = True
        out["subject"] = subject
        out["body_preview"] = body[:400]
        return out
    ledger = ledger or OpsLedger()
    mail = ledger.alert_now(subject=subject, body=body)
    out["sent_email"] = bool(mail.get("sent_email"))
    out["mail"] = {k: mail.get(k) for k in ("ok", "sent_email", "send_detail", "path")}
    # Stamp coalesce even when SMTP unset (file written) so */10 doesn't spam files.
    st = load_state()
    channels = dict(st.get("channels") or {})
    crow = dict(channels.get(channel) or {})
    crow["last_email_at"] = _utc_now().isoformat()
    crow["last_email_day"] = _karachi_day()
    crow["last_email_fingerprint"] = fp
    channels[channel] = crow
    st["channels"] = channels
    save_state(st)
    return out


def after_healthcheck_alerts(
    healthcheck_payload: dict[str, Any],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Hook for healthcheck_all / stream_beat / runpod_watchdog."""
    from src.streaming.vod_loop import CHANNELS

    channels = healthcheck_payload.get("channels") or {}
    results: dict[str, Any] = {}
    for ch in CHANNELS:
        row = channels.get(ch) or {}
        action = str(row.get("action") or "")
        repair_ok = action.startswith("relive") or action in {
            "dual_ingest_cleared",
            "stall_signaled",
            "healthy",
        }
        # Only probe email path when not clearly healthy.
        if action == "healthy" and not (row.get("extra") or {}).get("performance", {}).get(
            "needs_repair"
        ):
            # Reset attempts on healthy ticks.
            record_attempt(ch, issues=[], repaired=True)
            results[ch] = {"skipped": True, "reason": "healthy"}
            continue
        claimed = repair_ok and action not in {"healthy"}
        results[ch] = maybe_email_unresolved_live(
            ch, repair_claimed_ok=claimed, dry_run=dry_run
        )
    return {"ok": True, "module": "live_alerts", "channels": results, "ts": _utc_now().isoformat()}
