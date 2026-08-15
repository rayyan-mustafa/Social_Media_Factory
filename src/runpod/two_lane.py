"""Two-lane factory board + gpu_ready sync + approvals digest.

Lanes (CTO 2026-08-08):
  - CPU_PREP  : non-RunPod work (script + Kokoro) for approved+queued titles
  - GPU_READY : voice parked (``ready_for_stills`` / sheet status ``gpu_ready``)
  - GPU_RENDER: GREEN-only stills→compose→publish (inflight visuals)

Runs inside the existing */10 ``runpod_watchdog`` beat (365d, no holiday skip).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from src.runpod.cost_log import utcnow_iso
from src.runpod.ops_readiness import queue_buffer_target, prep_voice_buffer_target
from src.services.settings import ROOT

logger = logging.getLogger(__name__)
_LOCK = threading.Lock()

BOARD_PATH = ROOT / "output" / "ops" / "two_lane_board.json"
DIGEST_JSON = ROOT / "output" / "ops" / "approvals_needed_digest.json"
DIGEST_MD = ROOT / "output" / "ops" / "approvals_needed_digest.md"

GPU_READY_STATUS = "gpu_ready"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def _job_brief(job: Any) -> dict[str, Any]:
    return {
        "job_id": getattr(job, "id", None),
        "title": getattr(job, "title", None),
        "status": getattr(job, "status", None),
        "stage": getattr(job, "stage", None),
        "job_dir": getattr(job, "job_dir", None),
        "updated_at": getattr(job, "updated_at", None),
        "farm_phase": (getattr(job, "meta", None) or {}).get("farm_phase"),
    }


def _row_brief(row: Any) -> dict[str, Any]:
    return {
        "channel": getattr(row, "channel", ""),
        "row_index": getattr(row, "row_index", None),
        "title": getattr(row, "title", ""),
        "approved": bool(getattr(row, "approved", False)),
        "policy_ok": bool(getattr(row, "policy_ok", False)),
        "status": getattr(row, "status", ""),
        "job_id": getattr(row, "job_id", "") or "",
        "trend_score": getattr(row, "trend_score", 0),
        "notes": (getattr(row, "notes", "") or "")[:160],
    }


def build_two_lane_board() -> dict[str, Any]:
    """Snapshot CPU_PREP / GPU_READY / GPU_RENDER lanes."""
    from src.agents.farm import (
        count_gpu_jobs,
        count_prep_jobs,
        list_ready_for_stills,
        voice_artifacts_ready,
    )
    from src.agents.store import OpsStore
    from src.agents.title_queue import TitleQueue

    q = TitleQueue()
    store = OpsStore()
    buffer_target = queue_buffer_target()
    prep_target = prep_voice_buffer_target()

    pending_rows = q.pick_approved(limit=10_000, mutate_state=False)
    gpu_ready_jobs = list_ready_for_stills(store)
    gpu_ready_ok = [
        j for j in gpu_ready_jobs if voice_artifacts_ready(getattr(j, "job_dir", None))
    ]

    # Sheet rows already marked gpu_ready
    sheet_gpu_ready = [
        r
        for r in q.list_rows()
        if (r.status or "").lower() == GPU_READY_STATUS
    ]

    # Approvals candidates: policy_ok + queued + NOT approved
    need_approve = [
        r
        for r in q.list_rows()
        if r.policy_ok
        and not r.approved
        and (r.status or "queued").lower() in ("queued", "")
        and r.title.strip()
        and not (r.job_id or "").strip()
    ]
    need_approve.sort(key=lambda r: (-float(r.trend_score or 0), r.row_index))

    pending_n = len(pending_rows)
    voice_n = len(gpu_ready_ok)
    ready_total = pending_n + voice_n
    approvals_needed = max(0, buffer_target - ready_total)

    # Inflight GPU render jobs
    gpu_inflight = []
    for job in store.list_jobs():
        st = (job.status or "").lower()
        stage = (job.stage or "").lower()
        phase = (job.meta or {}).get("farm_phase")
        if st in {"farming", "encoding", "running"} or stage in {
            "visuals",
            "stills",
            "composing",
            "editing",
            "uploading",
        }:
            if st not in {"ready_for_stills", "awaiting_gpu"}:
                gpu_inflight.append(job)
        elif phase == "visuals" and st not in {
            "private",
            "public",
            "failed",
            "hold",
        }:
            gpu_inflight.append(job)

    board = {
        "at": utcnow_iso(),
        "model": "two_lane",
        "lanes": {
            "CPU_PREP": {
                "description": (
                    "Non-RunPod: approved+queued sheet titles waiting for "
                    "script+Kokoro Phase A"
                ),
                "count": pending_n,
                "items": [_row_brief(r) for r in pending_rows[:20]],
                "prep_jobs_inflight": count_prep_jobs(store),
            },
            "GPU_READY": {
                "description": (
                    "Voice parked — script+audio ready; sheet status gpu_ready; "
                    "waiting for GREEN stills"
                ),
                "count": voice_n,
                "target": prep_target,
                "jobs": [_job_brief(j) for j in gpu_ready_ok[:20]],
                "sheet_rows": [_row_brief(r) for r in sheet_gpu_ready[:20]],
            },
            "GPU_RENDER": {
                "description": "GREEN-only RunPod stills → compose → publish",
                "count": len(gpu_inflight),
                "gpu_lock_jobs": count_gpu_jobs(store),
                "items": [_job_brief(j) for j in gpu_inflight[:10]],
            },
        },
        "buffer": {
            "target": buffer_target,
            "pending_ready": pending_n,
            "gpu_ready": voice_n,
            "ready_total": ready_total,
            "deficit": max(0, buffer_target - ready_total),
            "approvals_needed_to_fill_buffer": approvals_needed,
        },
        "approvals_pool": {
            "unapproved_policy_ok_queued": len(need_approve),
            "top": [_row_brief(r) for r in need_approve[:15]],
        },
    }
    _write_json(BOARD_PATH, board)
    return board


def sync_sheet_gpu_ready(*, dry_run: bool = False) -> dict[str, Any]:
    """Mark sheet rows ``status=gpu_ready`` when matching jobs have voice parked."""
    from src.agents.farm import list_ready_for_stills, voice_artifacts_ready
    from src.agents.store import OpsStore
    from src.agents.title_queue import TitleQueue

    q = TitleQueue()
    store = OpsStore()
    updated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for job in list_ready_for_stills(store):
        jid = str(getattr(job, "id", "") or "")
        if not jid:
            continue
        if not voice_artifacts_ready(getattr(job, "job_dir", None)):
            skipped.append({"job_id": jid, "reason": "voice_artifacts_missing"})
            continue
        row = q.find_by_job_id(jid)
        if row is None:
            skipped.append({"job_id": jid, "reason": "no_sheet_row"})
            continue
        cur = (row.status or "").lower()
        if cur == GPU_READY_STATUS:
            skipped.append({"job_id": jid, "reason": "already_gpu_ready"})
            continue
        if cur in {"private", "public", "failed"}:
            skipped.append({"job_id": jid, "reason": f"terminal_status={cur}"})
            continue
        note = (row.notes or "").strip()
        tag = "gpu_ready — voice parked; waiting GREEN stills"
        if "gpu_ready" not in note.lower():
            note = f"{tag}" if not note else f"{note} | {tag}"
        if dry_run:
            updated.append(
                {
                    "job_id": jid,
                    "channel": row.channel,
                    "row_index": row.row_index,
                    "title": row.title,
                    "dry_run": True,
                }
            )
            continue
        q.update_row(
            row.row_index,
            channel=row.channel,
            status=GPU_READY_STATUS,
            notes=note[:500],
        )
        updated.append(
            {
                "job_id": jid,
                "channel": row.channel,
                "row_index": row.row_index,
                "title": row.title,
            }
        )
        logger.info(
            "sheet_gpu_ready job=%s channel=%s row=%s",
            jid,
            row.channel,
            row.row_index,
        )

    return {
        "at": utcnow_iso(),
        "updated": updated,
        "updated_n": len(updated),
        "skipped": skipped[:30],
        "dry_run": dry_run,
    }


def write_approvals_digest(board: dict[str, Any] | None = None) -> dict[str, Any]:
    """Digest: how many approvals needed to fill buffer + which titles to approve."""
    board = board or build_two_lane_board()
    buf = board.get("buffer") or {}
    pool = board.get("approvals_pool") or {}
    needed = int(buf.get("approvals_needed_to_fill_buffer") or 0)
    top = list(pool.get("top") or [])
    suggest = top[: max(needed, 3)] if top else []

    digest = {
        "at": board.get("at") or utcnow_iso(),
        "headline": (
            f"Approvals needed to fill buffer: {needed} "
            f"(target={buf.get('target')}, ready_total={buf.get('ready_total')}, "
            f"pending={buf.get('pending_ready')}, gpu_ready={buf.get('gpu_ready')})"
        ),
        "buffer": buf,
        "lanes": {
            "CPU_PREP": (board.get("lanes") or {}).get("CPU_PREP", {}).get("count"),
            "GPU_READY": (board.get("lanes") or {}).get("GPU_READY", {}).get("count"),
            "GPU_RENDER": (board.get("lanes") or {}).get("GPU_RENDER", {}).get("count"),
        },
        "approvals_needed": needed,
        "unapproved_pool_size": pool.get("unapproved_policy_ok_queued"),
        "approve_next": suggest,
        "action": (
            "Open Google Sheet → set approved=TRUE on the titles in approve_next "
            "(policy_ok already TRUE). Cron */10 will CPU_PREP then park gpu_ready."
            if needed > 0
            else "Buffer full — no approvals required this tick."
        ),
    }
    _write_json(DIGEST_JSON, digest)

    lines = [
        "# Approvals needed to fill buffer",
        "",
        f"- At: `{digest['at']}`",
        f"- **Approvals needed:** **{needed}**",
        f"- Buffer target: `{buf.get('target')}`",
        f"- Ready total: `{buf.get('ready_total')}` "
        f"(pending `{buf.get('pending_ready')}` + gpu_ready `{buf.get('gpu_ready')}`)",
        f"- Lanes: CPU_PREP=`{digest['lanes']['CPU_PREP']}` · "
        f"GPU_READY=`{digest['lanes']['GPU_READY']}` · "
        f"GPU_RENDER=`{digest['lanes']['GPU_RENDER']}`",
        f"- Unapproved policy_ok pool: `{digest['unapproved_pool_size']}`",
        "",
        "## Approve next (highest trend first)",
        "",
    ]
    if not suggest:
        lines.append("_None — buffer full or pool empty._")
    else:
        lines.append("| # | channel | row | trend | title |")
        lines.append("|---:|---|---:|---:|---|")
        for i, row in enumerate(suggest, 1):
            title = (row.get("title") or "").replace("|", "/")
            lines.append(
                f"| {i} | {row.get('channel')} | {row.get('row_index')} | "
                f"{row.get('trend_score')} | {title} |"
            )
    lines.extend(["", f"_{digest['action']}_", ""])
    DIGEST_MD.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        DIGEST_MD.write_text("\n".join(lines), encoding="utf-8")
    return digest


def maybe_alert_approvals_needed(digest: dict[str, Any]) -> dict[str, Any] | None:
    """Alert when buffer needs approvals and pool has candidates (deduped ~6h)."""
    needed = int(digest.get("approvals_needed") or 0)
    pool_n = int(digest.get("unapproved_pool_size") or 0)
    if needed <= 0 or pool_n <= 0:
        return None

    state_path = ROOT / "output" / "ops" / "approvals_digest_alert_state.json"
    last_at = None
    try:
        if state_path.exists():
            last_at = json.loads(state_path.read_text(encoding="utf-8")).get("last_alert_at")
    except Exception:  # noqa: BLE001
        last_at = None

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    if last_at:
        try:
            prev = datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))
            if (now - prev).total_seconds() < 6 * 3600:
                return {"skipped": True, "reason": "alerted_within_6h"}
        except ValueError:
            pass

    subject = f"[ops] Approvals needed to fill buffer: {needed}"
    tops = digest.get("approve_next") or []
    body_lines = [
        digest.get("headline") or subject,
        "",
        "Approve these next (sheet approved=TRUE):",
    ]
    for i, row in enumerate(tops[:8], 1):
        body_lines.append(
            f"{i}. [{row.get('channel')}] row {row.get('row_index')}: {row.get('title')}"
        )
    body_lines.append("")
    body_lines.append(str(digest.get("action") or ""))
    body = "\n".join(body_lines)

    detail: dict[str, Any] = {"at": utcnow_iso(), "subject": subject, "alerted": False}
    try:
        from src.agents.ledger import OpsLedger
        from src.agents.store import OpsStore

        out = OpsLedger(OpsStore()).alert_now(subject=subject, body=body)
        detail["alerted"] = True
        detail["send_detail"] = out
        _write_json(state_path, {"last_alert_at": detail["at"], "needed": needed})
    except Exception as exc:  # noqa: BLE001
        detail["error"] = str(exc)[:300]
        logger.warning("approvals digest alert failed: %s", exc)
    return detail


def run_two_lane_tick(*, dry_run: bool = False) -> dict[str, Any]:
    """Full two-lane maintenance for one cron tick."""
    sync = sync_sheet_gpu_ready(dry_run=dry_run)
    board = build_two_lane_board()
    digest = write_approvals_digest(board)
    alert = None
    if not dry_run:
        alert = maybe_alert_approvals_needed(digest)
    out = {
        "at": utcnow_iso(),
        "sync_gpu_ready": sync,
        "board_path": str(BOARD_PATH),
        "digest_json": str(DIGEST_JSON),
        "digest_md": str(DIGEST_MD),
        "buffer": board.get("buffer"),
        "lanes": {
            k: (v or {}).get("count")
            for k, v in (board.get("lanes") or {}).items()
        },
        "approvals_needed": digest.get("approvals_needed"),
        "alert": alert,
    }
    logger.info(
        "two_lane tick CPU_PREP=%s GPU_READY=%s GPU_RENDER=%s approvals_needed=%s",
        out["lanes"].get("CPU_PREP"),
        out["lanes"].get("GPU_READY"),
        out["lanes"].get("GPU_RENDER"),
        out["approvals_needed"],
    )
    return out
