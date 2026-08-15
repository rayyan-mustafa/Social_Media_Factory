#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.agents.schedule_agent import maybe_arm_public_approved_schedule  # noqa: E402
from src.agents.store import OpsStore  # noqa: E402


def main() -> int:
    # force live once; coalesce may skip — force=True bypasses
    p = maybe_arm_public_approved_schedule(dry_run=False, force=True)
    print(
        json.dumps(
            {
                "ok": p.get("ok"),
                "eligible_count": p.get("eligible_count"),
                "armed_ok": p.get("armed_ok"),
                "errors": p.get("errors"),
                "oauth_insufficient_scopes": p.get("oauth_insufficient_scopes"),
                "reauth_command": p.get("reauth_command"),
                "message": p.get("message"),
                "next_slot": (p.get("schedule_status") or {}).get("next_slot_local"),
                "first_reason": ((p.get("armed") or [{}])[0].get("reason") if p.get("armed") else None),
            },
            indent=2,
        )
    )
    store = OpsStore()
    still_private = [
        j.id
        for j in store.list_jobs(status="private")
        if (j.meta or {}).get("public_approved") or j.video_id
    ]
    # count private with video
    priv = [j.id for j in store.list_jobs(status="private") if j.video_id]
    print("private_with_video", len(priv), priv[:8])

    last = ROOT / "output" / "ops" / "runpod_watchdog_last.json"
    d = json.loads(last.read_text(encoding="utf-8"))
    d["schedule_arm"] = p
    last.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("persisted schedule_arm on last.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
