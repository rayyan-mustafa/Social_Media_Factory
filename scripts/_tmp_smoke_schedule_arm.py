#!/usr/bin/env python3
"""Smoke: dry schedule arm + eligible count + cron confirmation."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from src.agents.schedule_agent import (
        ScheduleAgent,
        maybe_arm_public_approved_schedule,
    )
    from src.runpod.watchdog import run_watchdog_tick

    print("=== compile ===")
    for mod in (
        "src/agents/schedule_agent.py",
        "src/agents/sleep_factory.py",
        "src/runpod/watchdog.py",
    ):
        subprocess.check_call([sys.executable, "-m", "py_compile", mod], cwd=ROOT)
        print("OK", mod)

    print("=== dry helper ===")
    payload = maybe_arm_public_approved_schedule(dry_run=True, force=True)
    print(
        json.dumps(
            {
                "ok": payload.get("ok"),
                "eligible_count": payload.get("eligible_count"),
                "apply_youtube": payload.get("apply_youtube"),
                "dry_run": payload.get("dry_run"),
                "message": payload.get("message"),
                "schedule_status": payload.get("schedule_status"),
                "armed_preview": (payload.get("armed") or [])[:3],
                "oauth": payload.get("oauth_insufficient_scopes"),
                "reauth_command": payload.get("reauth_command"),
            },
            indent=2,
        )
    )

    print("=== schedule status ===")
    print(json.dumps(ScheduleAgent().status(), indent=2))

    print("=== watchdog dry tick (schedule_arm field) ===")
    result = run_watchdog_tick(
        dry_run=True, skip_harvest=True, skip_learn=True, force_probe=False
    )
    d = result.to_dict()
    arm = d.get("schedule_arm") or {}
    print(
        json.dumps(
            {
                "action": d.get("action"),
                "schedule_arm_keys": sorted(arm.keys()) if isinstance(arm, dict) else type(arm).__name__,
                "eligible_count": arm.get("eligible_count") if isinstance(arm, dict) else None,
                "message": arm.get("message") if isinstance(arm, dict) else None,
                "next_slot": (arm.get("schedule_status") or {}).get("next_slot_local")
                if isinstance(arm, dict)
                else None,
                "oauth_insufficient_scopes": arm.get("oauth_insufficient_scopes")
                if isinstance(arm, dict)
                else None,
                "reauth_command": arm.get("reauth_command") if isinstance(arm, dict) else None,
            },
            indent=2,
        )
    )

    last = ROOT / "output" / "ops" / "runpod_watchdog_last.json"
    if last.exists():
        last_d = json.loads(last.read_text(encoding="utf-8"))
        print("=== last.json schedule_arm present ===", "schedule_arm" in last_d)
        if "schedule_arm" in last_d:
            sa = last_d["schedule_arm"] or {}
            print(
                json.dumps(
                    {
                        "eligible_count": sa.get("eligible_count"),
                        "message": sa.get("message"),
                        "next_slot": (sa.get("schedule_status") or {}).get("next_slot_local"),
                    },
                    indent=2,
                )
            )

    print("=== crontab (schedule-related) ===")
    try:
        cron = subprocess.check_output(["crontab", "-l"], text=True)
    except subprocess.CalledProcessError as exc:
        cron = exc.output or ""
    for line in cron.splitlines():
        if "runpod_watchdog" in line or "sleep_factory" in line:
            print(line)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
