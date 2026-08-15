#!/usr/bin/env python3
"""Live schedule-arm once (force) — expect OAuth 403 surface; then dry eligible."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def summarize(p: dict) -> dict:
    armed = p.get("armed") or []
    first_err = None
    for a in armed:
        if isinstance(a, dict) and (a.get("ok") is False or a.get("error") or a.get("reason")):
            if a.get("ok") is False:
                first_err = {
                    "job_id": a.get("job_id"),
                    "reason": (a.get("reason") or a.get("error") or "")[:300],
                    "oauth": a.get("oauth_insufficient_scopes"),
                    "reauth_command": a.get("reauth_command"),
                }
                break
    return {
        "ok": p.get("ok"),
        "eligible_count": p.get("eligible_count"),
        "armed_ok": p.get("armed_ok"),
        "errors": p.get("errors"),
        "message": p.get("message"),
        "oauth_insufficient_scopes": p.get("oauth_insufficient_scopes"),
        "reauth_command": p.get("reauth_command"),
        "reauth_docs": (p.get("reauth_docs") or "")[:180],
        "next_slot": (p.get("schedule_status") or {}).get("next_slot_local"),
        "first_err": first_err,
        "skipped": p.get("skipped"),
        "reason": p.get("reason"),
    }


def main() -> int:
    from src.agents.schedule_agent import maybe_arm_public_approved_schedule
    from src.runpod.watchdog import WatchdogTickResult, run_watchdog_tick

    print("=== dry preview (no mutate) ===")
    dry = maybe_arm_public_approved_schedule(dry_run=True, force=True)
    print(json.dumps(summarize(dry), indent=2))
    print("eligible_ids", [e.get("job_id") for e in (dry.get("eligible") or [])])

    print("=== live arm force (YouTube apply) ===")
    live = maybe_arm_public_approved_schedule(dry_run=False, force=True, apply_youtube=True)
    print(json.dumps(summarize(live), indent=2))

    # Persist a minimal synthetic last field check via dry watchdog is heavy;
    # write schedule_arm onto last.json carefully if present.
    last = ROOT / "output" / "ops" / "runpod_watchdog_last.json"
    if last.exists():
        d = json.loads(last.read_text(encoding="utf-8"))
        d["schedule_arm"] = live
        last.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print("wrote schedule_arm onto runpod_watchdog_last.json")

    cron = subprocess.check_output(["crontab", "-l"], text=True)
    print("=== cron ===")
    for line in cron.splitlines():
        if "runpod_watchdog" in line or "sleep_factory beat" in line:
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
