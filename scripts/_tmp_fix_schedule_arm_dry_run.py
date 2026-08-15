#!/usr/bin/env python3
"""Fix dry_run schedule mutation + revert smoke-damaged private jobs."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/home/ubuntu/new_yt_automation")
SCHEDULE = ROOT / "src" / "agents" / "schedule_agent.py"
JOBS = ROOT / "output" / "ops" / "jobs.json"

# Jobs flipped by dry_run smoke at ~2026-08-08T07:29Z without YouTube apply
REVERT_IDS = {
    "job_e4ab66f03917",
    "job_7d42ea1f619f",
    "job_618c8f00a20f",
    "job_5129e39a9343",
    "job_b8ab04aa9cc2",
}


def backup(path: Path, tag: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = path.with_suffix(path.suffix + f".bak_{tag}_{ts}")
    shutil.copy2(path, bak)
    print(f"BACKUP {bak}")


def fix_assign_dry_run() -> None:
    text = SCHEDULE.read_text(encoding="utf-8")
    marker = '            "dry_run": dry_run,\n            "would_apply_youtube": bool(apply_youtube),\n'
    if marker in text:
        print("ASSIGN_DRY_RUN_ALREADY_FIXED")
        return

    # After can_arm failure return, before slot assignment — insert early dry_run return
    # Prefer: after publish_at_iso computed, before YT/store mutation
    old = '''        slot_local = self.next_slot()
        slot_utc = slot_local.astimezone(timezone.utc)
        publish_at_iso = slot_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        yt_ok = None
        if apply_youtube and not dry_run:
'''
    new = '''        slot_local = self.next_slot()
        slot_utc = slot_local.astimezone(timezone.utc)
        publish_at_iso = slot_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        if dry_run:
            return {
                "ok": True,
                "job_id": job_id,
                "video_id": video_id,
                "scheduled_at_local": slot_local.isoformat(),
                "publish_at_utc": publish_at_iso,
                "timezone": str(self.tz),
                "pkt_vs_ny": self.pkt_to_eastern_note(),
                "dry_run": True,
                "would_apply_youtube": bool(apply_youtube),
            }

        yt_ok = None
        if apply_youtube and not dry_run:
'''
    if old not in text:
        raise SystemExit("ASSIGN_DRY_RUN_ANCHOR_MISSING")
    backup(SCHEDULE, "dry_run_fix")
    SCHEDULE.write_text(text.replace(old, new, 1), encoding="utf-8")
    print("ASSIGN_DRY_RUN_FIXED")


def revert_jobs() -> None:
    backup(JOBS, "revert_dry_arm")
    jobs = json.loads(JOBS.read_text(encoding="utf-8"))
    if not isinstance(jobs, list):
        raise SystemExit(f"unexpected jobs.json type {type(jobs)}")
    reverted = []
    for j in jobs:
        if j.get("id") not in REVERT_IDS:
            continue
        if j.get("status") != "scheduled":
            continue
        meta = dict(j.get("meta") or {})
        # Keep approval flags if present; drop schedule-only keys from dry smoke
        for k in ("scheduled_at_local", "publish_at_utc", "timezone"):
            meta.pop(k, None)
        j["status"] = "private"
        j["stage"] = "private"
        j["meta"] = meta
        j["updated_at"] = datetime.now(timezone.utc).isoformat()
        reverted.append(j["id"])
    JOBS.write_text(json.dumps(jobs, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("REVERTED", reverted)

    # Best-effort sheet row revert via TitleQueue (may 429)
    try:
        import sys

        sys.path.insert(0, str(ROOT))
        from src.agents.title_queue import TitleQueue

        q = TitleQueue()
        for jid in reverted:
            row = q.find_by_job_id(jid)
            if not row:
                continue
            if (row.status or "").lower() == "scheduled":
                q.update_row(
                    row.row_index,
                    status="private",
                    scheduled_at="",
                    channel=getattr(row, "channel", None) or None,
                )
                print("SHEET_REVERTED", jid, "row", row.row_index)
    except Exception as exc:  # noqa: BLE001
        print("SHEET_REVERT_SKIP", str(exc)[:200])


def main() -> None:
    fix_assign_dry_run()
    revert_jobs()
    print("DONE")


if __name__ == "__main__":
    main()
