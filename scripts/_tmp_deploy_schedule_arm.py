#!/usr/bin/env python3
"""Deploy public_approved schedule arm on */10 watchdog (+ sleep_factory shared helper).

Idempotent. Does not touch crontab, farm arming, or disk_guard logic beyond
adding a sibling tick step. Never auto-sets public_approved.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/home/ubuntu/new_yt_automation")
SCHEDULE = ROOT / "src" / "agents" / "schedule_agent.py"
WD = ROOT / "src" / "runpod" / "watchdog.py"
SF = ROOT / "src" / "agents" / "sleep_factory.py"
OPS = ROOT / "output" / "ops"

HELPER_MARKER = "def maybe_arm_public_approved_schedule("
HELPER_BLOCK = r'''

_REAUTH_CMD = ".venv/bin/python -m src.cli.youtube_auth"
_REAUTH_DOCS = (
    "src/cli/youtube_auth.py — AUTH_SCOPES = youtube.upload + youtube.force-ssl; "
    "videos().update(part=status) / publishAt needs force-ssl (upload-only token → 403)"
)
_COALESCE_PATH_NAME = "schedule_arm_coalesce.json"


def _oauth_fix_payload(err: str) -> dict[str, Any] | None:
    low = (err or "").lower()
    if ("insufficient" in low and "scope" in low) or (
        "403" in low and "scope" in low
    ):
        return {
            "oauth_insufficient_scopes": True,
            "reauth_command": _REAUTH_CMD,
            "reauth_docs": _REAUTH_DOCS,
            "fix": (
                "YouTube OAuth token lacks publish/manage scopes for "
                "videos.update(part=status). Re-auth with AUTH_SCOPES, then ensure "
                "config/youtube_token.json on the VPS is the new token."
            ),
        }
    return None


def maybe_arm_public_approved_schedule(
    *,
    dry_run: bool = False,
    force: bool = False,
    apply_youtube: bool | None = None,
    coalesce_minutes: float | None = None,
    store: OpsStore | None = None,
    ledger: OpsLedger | None = None,
    queue: TitleQueue | None = None,
) -> dict[str, Any]:
    """Arm publishAt for private jobs that already have public_approved=TRUE.

    Shared by */10 runpod_watchdog and */15 sleep_factory. Never sets
    public_approved — only arms rows/jobs already approved. Coalesces so dual
    beats do not double-hit YouTube for the same videos within ~10 minutes.
    """
    from src.agents.store import OPS_DIR

    store = store or OpsStore()
    ledger = ledger or OpsLedger(store)
    queue = queue or TitleQueue()
    sched = ScheduleAgent(store, ledger, queue)

    if apply_youtube is None:
        apply_youtube = not dry_run
    coalesce_m = (
        float(coalesce_minutes)
        if coalesce_minutes is not None
        else float((sched.cfg or {}).get("arm_coalesce_minutes") or 9)
    )

    coalesce_path = OPS_DIR / _COALESCE_PATH_NAME
    now = datetime.now(timezone.utc)
    if not force and not dry_run and coalesce_m > 0 and coalesce_path.exists():
        try:
            prev = json.loads(coalesce_path.read_text(encoding="utf-8"))
            raw_at = prev.get("at") or prev.get("ts")
            if raw_at:
                at = datetime.fromisoformat(str(raw_at).replace("Z", "+00:00"))
                if at.tzinfo is None:
                    at = at.replace(tzinfo=timezone.utc)
                age = now - at.astimezone(timezone.utc)
                if age < timedelta(minutes=coalesce_m):
                    return {
                        "ok": True,
                        "skipped": True,
                        "reason": "quota_coalesce",
                        "coalesce_minutes": coalesce_m,
                        "age_seconds": int(age.total_seconds()),
                        "eligible_count": int(prev.get("eligible_count") or 0),
                        "schedule_status": sched.status(),
                        "last_at": raw_at,
                        "armed": [],
                    }
        except Exception as exc:  # noqa: BLE001
            logger.info("schedule_arm coalesce check failed: %s", exc)

    eligible: list[dict[str, Any]] = []
    for job in store.list_jobs(status="private"):
        meta = job.meta or {}
        row = queue.find_by_job_id(job.id)
        public_ok = bool(meta.get("public_approved")) or (
            bool(getattr(row, "public_approved", False)) if row else False
        )
        if not public_ok or not job.video_id:
            continue
        eligible.append(
            {
                "job_id": job.id,
                "video_id": job.video_id,
                "title": getattr(job, "title", None) or (meta.get("title") if meta else None),
            }
        )

    status = sched.status()
    out: dict[str, Any] = {
        "ok": True,
        "dry_run": bool(dry_run),
        "apply_youtube": bool(apply_youtube),
        "eligible_count": len(eligible),
        "eligible": eligible[:20],
        "schedule_status": status,
        "armed": [],
        "errors": 0,
        "oauth_insufficient_scopes": False,
    }

    if not eligible:
        out["message"] = "no private+public_approved+video_id jobs"
        if not dry_run:
            try:
                coalesce_path.write_text(
                    json.dumps(
                        {
                            "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "eligible_count": 0,
                            "armed_ok": 0,
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            except OSError:
                pass
        return out

    armed: list[dict[str, Any]] = []
    oauth_hit = False
    for item in eligible:
        try:
            result = sched.assign_slot_for_job(
                job_id=item["job_id"],
                video_id=item["video_id"],
                public_approved=True,
                dry_run=bool(dry_run),
                apply_youtube=bool(apply_youtube) and not dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "job_id": item["job_id"], "error": str(exc)[:400]}
        hint = _oauth_fix_payload(
            str(result.get("reason") or result.get("error") or "")
        )
        if hint:
            oauth_hit = True
            result = {**result, **hint}
            out["oauth_fix"] = hint
            out["reauth_command"] = hint["reauth_command"]
            out["reauth_docs"] = hint["reauth_docs"]
        if result.get("ok") is False or result.get("error"):
            out["errors"] = int(out["errors"]) + 1
        armed.append(result)

    out["armed"] = armed
    out["armed_ok"] = sum(1 for a in armed if a.get("ok") is True)
    out["oauth_insufficient_scopes"] = oauth_hit
    if oauth_hit:
        out["ok"] = False
        out["message"] = (
            "schedule arm blocked by YouTube OAuth scopes — "
            f"run `{_REAUTH_CMD}` (see {_REAUTH_DOCS})"
        )
    else:
        out["message"] = (
            f"armed {out['armed_ok']}/{len(eligible)} "
            f"(apply_youtube={bool(apply_youtube) and not dry_run})"
        )

    # Refresh status after any successful local/slot updates
    out["schedule_status"] = sched.status()

    if not dry_run:
        try:
            coalesce_path.write_text(
                json.dumps(
                    {
                        "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "eligible_count": len(eligible),
                        "armed_ok": out.get("armed_ok"),
                        "errors": out.get("errors"),
                        "oauth_insufficient_scopes": oauth_hit,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("schedule_arm coalesce write failed: %s", exc)

    return out

'''


def _backup(path: Path, tag: str) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = path.with_suffix(path.suffix + f".bak_{tag}_{ts}")
    shutil.copy2(path, bak)
    print(f"BACKUP {bak}")
    return bak


def patch_schedule_agent() -> None:
    text = SCHEDULE.read_text(encoding="utf-8")
    if HELPER_MARKER in text:
        print("SCHEDULE_HELPER_ALREADY_PRESENT")
        return
    _backup(SCHEDULE, "schedule_arm")
    # Insert before _load_schedule_config
    anchor = "\ndef _load_schedule_config() -> dict[str, Any]:\n"
    if anchor not in text:
        raise SystemExit("SCHEDULE_ANCHOR_MISSING")
    text = text.replace(anchor, HELPER_BLOCK + anchor, 1)
    SCHEDULE.write_text(text, encoding="utf-8")
    print("SCHEDULE_HELPER_ADDED")


def patch_sleep_factory() -> None:
    text = SF.read_text(encoding="utf-8")
    if "maybe_arm_public_approved_schedule(" in text:
        print("SLEEP_FACTORY_ALREADY_PATCHED")
        return
    _backup(SF, "schedule_arm")

    old = (
        "    # 5) Arm publishAt for private jobs that already have public_approved + video_id\n"
        "    sched = ScheduleAgent(store, ledger, queue)\n"
        "    armed: list[dict[str, Any]] = []\n"
        '    for job in store.list_jobs(status="private"):\n'
        "        meta = job.meta or {}\n"
        "        row = queue.find_by_job_id(job.id)\n"
        '        public_ok = bool(meta.get("public_approved")) or (\n'
        "            row.public_approved if row else False\n"
        "        )\n"
        "        if not public_ok or not job.video_id:\n"
        "            continue\n"
        "        try:\n"
        "            armed.append(\n"
        "                sched.assign_slot_for_job(\n"
        "                    job_id=job.id,\n"
        "                    video_id=job.video_id,\n"
        "                    public_approved=True,\n"
        "                    apply_youtube=True,\n"
        "                )\n"
        "            )\n"
        "        except Exception as exc:  # noqa: BLE001\n"
        '            armed.append({"job_id": job.id, "error": str(exc)})\n'
        '    out["steps"]["schedule_arm"] = armed\n'
        '    out["steps"]["schedule_status"] = sched.status()\n'
    )
    new = (
        "    # 5) Arm publishAt for private jobs that already have public_approved + video_id\n"
        "    # Shared helper with */10 watchdog (coalesce + OAuth error surfacing)\n"
        "    try:\n"
        "        from src.agents.schedule_agent import maybe_arm_public_approved_schedule\n"
        "\n"
        "        arm_payload = maybe_arm_public_approved_schedule(\n"
        "            store=store, ledger=ledger, queue=queue, dry_run=False\n"
        "        )\n"
        "    except Exception as exc:  # noqa: BLE001\n"
        '        logger.warning("sleep_factory schedule_arm failed: %s", exc)\n'
        '        arm_payload = {"ok": False, "error": str(exc)[:300]}\n'
        '    out["steps"]["schedule_arm"] = arm_payload\n'
        '    out["steps"]["schedule_status"] = (arm_payload or {}).get(\n'
        '        "schedule_status"\n'
        "    ) or ScheduleAgent(store, ledger, queue).status()\n"
    )
    if old not in text:
        raise SystemExit("SLEEP_FACTORY_SCHEDULE_BLOCK_MISSING")
    text = text.replace(old, new, 1)
    SF.write_text(text, encoding="utf-8")
    print("SLEEP_FACTORY_PATCHED")


def patch_watchdog() -> None:
    text = WD.read_text(encoding="utf-8")
    if "maybe_arm_public_approved_schedule" in text and "schedule_arm=schedule_arm_payload" in text:
        print("WATCHDOG_ALREADY_PATCHED")
        return
    _backup(WD, "schedule_arm")

    # 1) dataclass field
    if "    schedule_arm: dict[str, Any] | None = None\n" not in text:
        old_field = (
            "    disk_guard: dict[str, Any] | None = None\n"
            "    pending_ready: int = 0\n"
        )
        new_field = (
            "    disk_guard: dict[str, Any] | None = None\n"
            "    schedule_arm: dict[str, Any] | None = None\n"
            "    pending_ready: int = 0\n"
        )
        if old_field not in text:
            raise SystemExit("WATCHDOG_FIELD_ANCHOR_MISSING")
        text = text.replace(old_field, new_field, 1)

    # 2) early call after disk_guard
    early = (
        "    # Schedule arm: private + public_approved → publishAt (factory timings)\n"
        "    schedule_arm_payload: dict[str, Any] | None = None\n"
        "    try:\n"
        "        from src.agents.schedule_agent import maybe_arm_public_approved_schedule\n"
        "\n"
        "        schedule_arm_payload = maybe_arm_public_approved_schedule(dry_run=dry_run)\n"
        "    except Exception as exc:  # noqa: BLE001\n"
        '        logger.warning("schedule_arm tick failed: %s", exc)\n'
        '        schedule_arm_payload = {"ok": False, "error": str(exc)[:300]}\n'
        "\n"
    )
    if "schedule_arm_payload" not in text:
        anchor = (
            "    # Two-lane board + gpu_ready sheet sync + approvals digest (every beat)\n"
            "    two_lane_payload: dict[str, Any] | None = None\n"
        )
        if anchor not in text:
            raise SystemExit("WATCHDOG_TICK_ANCHOR_MISSING")
        text = text.replace(anchor, early + anchor, 1)

    # 3) attach schedule_arm= after every disk_guard=disk_guard_payload,
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    for i, line in enumerate(lines):
        out.append(line)
        stripped = line.lstrip(" ")
        if stripped.startswith("disk_guard=disk_guard_payload,"):
            indent = line[: len(line) - len(stripped)]
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if "schedule_arm=schedule_arm_payload" not in nxt:
                out.append(f"{indent}schedule_arm=schedule_arm_payload,\n")
    text = "".join(out)

    # 4) helper docstring near _maybe_smm_scan for discoverability
    if "def _maybe_schedule_arm" not in text:
        smm_def = "def _maybe_smm_scan(*, dry_run: bool = False)"
        if smm_def not in text:
            # optional — early call is enough
            pass
        else:
            note = (
                "def _maybe_schedule_arm(*, dry_run: bool = False) -> dict[str, Any] | None:\n"
                '    """Thin wrapper — primary call is early in run_watchdog_tick."""\n'
                "    try:\n"
                "        from src.agents.schedule_agent import maybe_arm_public_approved_schedule\n"
                "\n"
                "        return maybe_arm_public_approved_schedule(dry_run=dry_run)\n"
                "    except Exception as exc:  # noqa: BLE001\n"
                '        logger.warning("watchdog schedule_arm failed: %s", exc)\n'
                '        return {"ok": False, "error": str(exc)[:300]}\n'
                "\n"
                "\n"
            )
            text = text.replace(smm_def, note + smm_def, 1)

    WD.write_text(text, encoding="utf-8")
    print("WATCHDOG_PATCHED")


def main() -> None:
    OPS.mkdir(parents=True, exist_ok=True)
    if not SCHEDULE.exists():
        raise SystemExit(f"MISSING {SCHEDULE}")
    if not WD.exists():
        raise SystemExit(f"MISSING {WD}")
    if not SF.exists():
        raise SystemExit(f"MISSING {SF}")
    patch_schedule_agent()
    patch_sleep_factory()
    patch_watchdog()
    print("DONE")


if __name__ == "__main__":
    main()
