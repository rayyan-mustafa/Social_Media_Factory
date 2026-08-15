"""Lead-gen watchdog — VPS-only. Never start RunPod/Kokoro. Separate from runpod_watchdog."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.leadgen.cost import load_rates, remaining_month_usd
from src.services.settings import ROOT

OPS = ROOT / "output" / "ops"
LAST_PATH = OPS / "leadgen_watchdog_last.json"
SPEND_LOG = OPS / "leadgen_spend.jsonl"
HUNT_LOG = OPS / "leadgen_hunts.jsonl"


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _count_today_hunts() -> tuple[int, int]:
    hunts = 0
    leads = 0
    day = _utc_day()
    if not HUNT_LOG.is_file():
        return 0, 0
    for line in HUNT_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("at") or "").startswith(day):
            hunts += 1
            leads += int(row.get("leads_n") or 0)
    return hunts, leads


def log_hunt(*, run_id: str, leads_n: int) -> None:
    HUNT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with HUNT_LOG.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "run_id": run_id,
                    "leads_n": int(leads_n),
                }
            )
            + "\n"
        )


def _runpod_env_hot() -> bool:
    return bool(
        (os.getenv("RUNPOD_API_KEY") or "").strip()
        and str(os.getenv("IMAGE_BACKEND") or "").lower() in {"runpod_pod", "runpod", "comfy"}
    )


def _last_spend_row() -> dict[str, Any] | None:
    if not SPEND_LOG.is_file():
        return None
    last: dict[str, Any] | None = None
    for line in SPEND_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            last = json.loads(line)
        except json.JSONDecodeError:
            continue
    return last


def _warn_last_job_over_benchmark(rates: dict[str, Any]) -> str | None:
    """Warn (not error) if last spend row elapsed_s blew hunt_25 benchmark. Never kills pods."""
    row = _last_spend_row()
    if not row:
        return None
    bench = float((rates.get("benchmarks") or {}).get("hunt_25_elapsed_s_max") or 240)
    try:
        elapsed = float(row.get("elapsed_s") or 0)
    except (TypeError, ValueError):
        return None
    if elapsed <= bench:
        return None
    run_id = row.get("run_id") or "?"
    return (
        f"last job {run_id} elapsed_s={elapsed:.1f} > hunt_25_elapsed_s_max={bench:.0f} "
        "(warning only; do not kill RunPod)"
    )


def preflight(*, limit: int = 25) -> dict[str, Any]:
    """FAIL CLOSED if this hunt would violate SOP bans/caps."""
    rates = load_rates()
    caps = rates.get("caps") or {}
    errors: list[str] = []
    warnings: list[str] = []

    hunts, leads = _count_today_hunts()
    if hunts >= int(caps.get("hunts_per_utc_day_max") or 3):
        errors.append(f"hunts today {hunts} ≥ cap {caps.get('hunts_per_utc_day_max')}")
    if leads + int(limit) > int(caps.get("leads_per_utc_day_max") or 100):
        errors.append(
            f"leads today {leads}+{limit} > cap {caps.get('leads_per_utc_day_max')}"
        )

    left = remaining_month_usd()
    if left <= 0:
        errors.append(f"monthly leadgen cap exhausted (remaining ${left})")

    # Never allow leadgen to flip factory GPU on
    if os.getenv("LEADGEN_ALLOW_RUNPOD", "").strip() in {"1", "true", "yes"}:
        errors.append("LEADGEN_ALLOW_RUNPOD is set — SOP forbids GPU on this agent")

    if _runpod_env_hot():
        warnings.append(
            "YouTube IMAGE_BACKEND is RunPod — leadgen will still NOT start a pod; "
            "avoid overlapping a hunt with a GREEN farm if VPS RAM is tight"
        )

    tts = (os.getenv("TTS_BACKEND") or "").strip().lower()
    if tts and tts not in {"kokoro", "kokoro_local", ""}:
        warnings.append(f"TTS_BACKEND={tts} is factory-only; leadgen must not call it")

    over = _warn_last_job_over_benchmark(rates)
    if over:
        warnings.append(over)

    from src.agents.leadgen.email_auth import check_domain, send_domain

    mail_domain = send_domain()
    email_auth: dict[str, Any] | None = None
    if mail_domain:
        email_auth = check_domain(mail_domain)
        warnings.extend(str(w) for w in (email_auth.get("warnings") or []))
        if not email_auth.get("ok"):
            warnings.append(
                "email SPF/DKIM/DMARC not green — drafts only; do not send outreach "
                f"from {mail_domain} until `leadgen email-check` passes "
                "(see config/sop/leadgen_email_sop.md)"
            )
            for e in email_auth.get("errors") or []:
                warnings.append(f"email-auth: {e}")
    else:
        warnings.append(
            "no agency send domain yet — use Gmail only for ops/tests to yourself; "
            "outreach From needs SPF+DKIM+DMARC on a domain you own"
        )

    ok = not errors
    payload = {
        "ok": ok,
        "errors": errors,
        "warnings": warnings,
        "hunts_today": hunts,
        "leads_today": leads,
        "remaining_month_usd": left,
        "caps": caps,
        "bans": rates.get("bans") or [],
        "sop": "config/sop/leadgen_sop.md",
        "email_sop": "config/sop/leadgen_email_sop.md",
        "email_auth": email_auth,
        "send_domain": mail_domain,
    }
    LAST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def tick(*, dry_run: bool = False) -> dict[str, Any]:
    """Periodic SOP check. Does not spawn farms or kill YouTube pods."""
    pf = preflight(limit=0)
    spend_lines = 0
    if SPEND_LOG.is_file():
        spend_lines = sum(1 for _ in SPEND_LOG.open(encoding="utf-8") if _.strip())
    out = {
        "ok": pf["ok"],
        "dry_run": dry_run,
        "preflight": pf,
        "spend_events": spend_lines,
        "agent": "leadgen_watchdog",
        "not": ["runpod_watchdog", "ceo_smm_beat", "sleep_factory"],
    }
    LAST_PATH.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out
