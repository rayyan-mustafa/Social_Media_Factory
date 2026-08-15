"""JobHealthWatchdog — detect and auto-fix dead farms, SFX stalls, hung ffmpeg, upload gaps.

Runs every */10 runpod_watchdog tick and */15 sleep_factory beat (before stuck scan).
Always attempts resume — never leaves zombie ``farming`` rows with ``farm_pid=null``.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.farm import (
    ACTIVE_FARM_STATUSES,
    infer_last_good_stage,
    pid_alive,
    spawn_farm_job,
)
from src.agents.ledger import OpsLedger
from src.agents.store import JobRecord, OpsStore
from src.services.settings import CONFIG_DIR, ROOT

logger = logging.getLogger(__name__)

HUNG_FFMPEG_S = 30 * 60
SFX_STALL_S = 15 * 60
_SKIP_MARKERS = (
    "permanent skip",
    "do not spawn",
    "do not enqueue",
    "do not resume",
    "parked mongols",
    "parked ottoman",
)


def _agents_cfg() -> dict[str, Any]:
    path = CONFIG_DIR / "agents_settings.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _cfg() -> dict[str, Any]:
    return dict((_agents_cfg().get("job_health_watchdog") or {}))


def _job_skipped(job: JobRecord) -> bool:
    meta = job.meta or {}
    if meta.get("skip_auto_farm") or meta.get("park_reason"):
        return True
    blob = f"{job.error or ''} {meta.get('note') or ''} {meta.get('park_note') or ''}".lower()
    return any(m in blob for m in _SKIP_MARKERS)


def _probe_duration(path: Path) -> float:
    try:
        from src.services.compose_sfx import _probe_duration as _pd

        return float(_pd(path))
    except Exception:  # noqa: BLE001
        return 0.0


def _file_mtime(path: Path) -> float | None:
    try:
        if path.is_file():
            return path.stat().st_mtime
    except OSError:
        pass
    return None


def _ffmpeg_pids_for_job(job_dir: Path) -> list[int]:
    """PIDs of ffmpeg processes writing under this job's video dir."""
    needle = str(job_dir.resolve())
    out: list[int] = []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return out
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            )
        except OSError:
            continue
        if "ffmpeg" not in cmdline.lower() or needle not in cmdline:
            continue
        try:
            out.append(int(entry.name))
        except ValueError:
            continue
    return out


def _farm_pids_for_job(job_id: str) -> list[int]:
    """Detached run_farm_job PIDs referencing this job_id."""
    needle = job_id
    out: list[int] = []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return out
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            )
        except OSError:
            continue
        if "run_farm_job" not in cmdline or needle not in cmdline:
            continue
        try:
            out.append(int(entry.name))
        except ValueError:
            continue
    return out


def _kill_pids(pids: list[int], *, sig: int = signal.SIGTERM) -> list[int]:
    killed: list[int] = []
    for pid in pids:
        try:
            os.kill(pid, sig)
            killed.append(pid)
        except OSError:
            continue
    return killed


def _needs_compose_sfx(job: JobRecord) -> bool:
    meta = job.meta or {}
    if meta.get("compose_sfx_expected") is False:
        return False
    if meta.get("compose_sfx_expected") is True:
        return True
    channel = (meta.get("channel") or meta.get("sheet_tab") or "napstorian").lower()
    try:
        from src.services.compose_sfx import resolve_compose_sfx

        return bool(resolve_compose_sfx(channel))
    except Exception:  # noqa: BLE001
        return channel == "napstorian"


def _sfx_applied(job_dir: Path) -> bool:
    pre = job_dir / "video" / "final_pre_sfx.mp4"
    if pre.is_file() and pre.stat().st_size > 1000:
        return True
    manifest = job_dir / "video" / "edit_manifest.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            meta = data.get("compose_sfx_meta") or {}
            if meta.get("applied") or meta.get("compose_sfx"):
                return True
        except (OSError, json.JSONDecodeError):
            pass
    return False


def _publish_wanted(job: JobRecord) -> bool:
    meta = job.meta or {}
    flags = meta.get("farm_flags") or {}
    if flags.get("publish") is False:
        return False
    if meta.get("skip_publish_keep_schedule") or meta.get("preserve_scheduled_video_id"):
        return False
    return True


def _publish_terminal_status(job: JobRecord) -> bool:
    """True when job is past upload — schedule-arm or done only, never RunPod re-stills."""
    status = (job.status or "").lower()
    if status in {"private", "scheduled", "public", "done"}:
        return True
    # video_id set but status still farming/queued (watchdog race after publish)
    return bool(job.video_id)


def _guard_published_job(
    job: JobRecord,
    *,
    store: OpsStore,
    ledger: OpsLedger,
    reason: str,
) -> dict[str, Any]:
    """Normalize uploaded jobs — clear farm locks, arm schedule, never re-farm visuals."""
    meta = dict(job.meta or {})
    meta["farm_pid"] = None
    meta.pop("farm_phase", None)
    meta["job_health_publish_guard_at"] = datetime.now(timezone.utc).isoformat()
    meta["job_health_reason"] = reason[:400]
    status = (job.status or "").lower()
    if status not in {"private", "scheduled", "public", "done"}:
        status = "private"
    store.update_job(
        job.id,
        status=status,
        stage=status,
        error=None,
        meta=meta,
    )
    ledger.write(
        agent="job_health_watchdog",
        problem=reason[:240],
        action=f"publish_guard video_id={job.video_id} — no re-farm",
        job_id=job.id,
        severity="warn",
        publish_status=status,
    )
    try:
        from src.agents.schedule_agent import maybe_arm_public_approved_schedule

        maybe_arm_public_approved_schedule(store=store, ledger=ledger, dry_run=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("schedule arm after publish_guard failed: %s", exc)
    return {
        "job_id": job.id,
        "action": "publish_guard",
        "video_id": job.video_id,
        "reason": reason,
    }


def _schedule_passed(job: JobRecord) -> bool:
    meta = job.meta or {}
    raw = meta.get("publish_at_utc") or meta.get("force_publish_at_utc")
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= dt.astimezone(timezone.utc)
    except ValueError:
        return False


def _mtime_stalled(
    path: Path,
    meta: dict[str, Any],
    *,
    stall_s: float,
    key: str,
) -> tuple[bool, float | None]:
    """True when file mtime unchanged for stall_s since last watchdog check."""
    mtime = _file_mtime(path)
    if mtime is None:
        return False, None
    checks = dict(meta.get("job_health_mtime_checks") or {})
    prev = checks.get(key)
    now_ts = time.time()
    checks[key] = {"mtime": mtime, "checked_at": now_ts}
    meta["job_health_mtime_checks"] = checks
    if not prev or prev.get("mtime") != mtime:
        return False, mtime
    age = now_ts - float(prev.get("checked_at") or now_ts)
    return age >= stall_s, mtime


def _run_sfx_for_job(job: JobRecord, job_dir: Path) -> dict[str, Any]:
    """Retry compose SFX from edit_manifest when final.mp4 exists."""
    from src.services.compose_sfx import (
        ComposeSfxError,
        mix_compose_sfx_into_final,
        plan_compose_sfx,
    )

    manifest_path = job_dir / "video" / "edit_manifest.json"
    final = job_dir / "video" / "final.mp4"
    if not manifest_path.is_file():
        return {"ok": False, "error": "no edit_manifest.json"}
    if not final.is_file():
        return {"ok": False, "error": "no final.mp4"}

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    scenes = data.get("scenes") or []
    channel = (job.meta or {}).get("channel") or (job.meta or {}).get("sheet_tab") or "napstorian"
    boundaries: set[int] = set()
    for i, sc in enumerate(scenes[:-1]):
        if not isinstance(sc, dict):
            continue
        # chapter boundaries optional — plan works without
        pass

    plan = plan_compose_sfx(
        channel=str(channel),
        scene_clips=scenes,
        chapter_boundary_after=boundaries,
    )
    if not plan.enabled:
        return {"ok": True, "skipped": True, "reason": "compose_sfx_disabled"}
    try:
        applied = mix_compose_sfx_into_final(final, plan)
        return {"ok": True, "applied": applied}
    except ComposeSfxError as exc:
        return {"ok": False, "error": str(exc)}


def _finish_publish_for_job(
    job: JobRecord,
    job_dir: Path,
    *,
    store: OpsStore,
    ledger: OpsLedger,
    reason: str,
) -> dict[str, Any]:
    """SFX (if needed) + YouTube private upload — no RunPod re-stills."""
    channel = (job.meta or {}).get("channel") or (job.meta or {}).get("sheet_tab")
    final = job_dir / "video" / "final.mp4"
    if _needs_compose_sfx(job) and not _sfx_applied(job_dir):
        sfx = _run_sfx_for_job(job, job_dir)
        if not sfx.get("ok"):
            return {"job_id": job.id, "ok": False, "error": sfx.get("error"), "stage": "sfx"}

    from src.services.publish_youtube import PublishModule, PublishModuleError

    script_path = job_dir / "script" / "script.json"
    visual_manifest = job_dir / "images" / "visual_manifest.json"
    try:
        pub = PublishModule(channel=channel).publish_private(
            final_path=final,
            script_path=script_path if script_path.is_file() else None,
            visual_manifest=visual_manifest if visual_manifest.is_file() else None,
            job_dir=job_dir,
            channel=channel,
            dry_run=False,
        )
    except PublishModuleError as exc:
        err = str(exc)
        ledger.write(
            agent="job_health_watchdog",
            problem=f"publish failed job={job.id}: {err[:200]}",
            action="HOLD — check YouTube auth/network",
            job_id=job.id,
            severity="critical",
        )
        return {
            "job_id": job.id,
            "ok": False,
            "error": err,
            "stage": "publish",
            "youtube_blocked": "auth" in err.lower() or "network" in err.lower(),
        }

    meta = dict(job.meta or {})
    meta["farm_pid"] = None
    meta["job_health_publish_at"] = datetime.now(timezone.utc).isoformat()
    meta["job_health_reason"] = reason[:400]
    store.update_job(
        job.id,
        status="private",
        stage="private",
        video_id=pub.video_id,
        watch_url=pub.watch_url,
        error=None,
        meta=meta,
    )
    ledger.write(
        agent="job_health_watchdog",
        problem=reason[:240],
        action=f"published video_id={pub.video_id}",
        job_id=job.id,
        severity="info",
        publish_status="private",
    )
    try:
        from src.agents.schedule_agent import maybe_arm_public_approved_schedule

        maybe_arm_public_approved_schedule(store=store, ledger=ledger, dry_run=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("schedule arm after publish-only failed: %s", exc)

    return {
        "job_id": job.id,
        "ok": True,
        "action": "publish_only",
        "video_id": pub.video_id,
        "watch_url": pub.watch_url,
        "reason": reason,
    }


def _resume_job(
    job: JobRecord,
    *,
    store: OpsStore,
    ledger: OpsLedger,
    reason: str,
    spawn: bool,
) -> dict[str, Any]:
    """Requeue + spawn farm from last checkpoint (post-pod publish skips GREEN gate)."""
    if job.video_id:
        return _guard_published_job(
            job, store=store, ledger=ledger, reason=f"resume blocked — already uploaded: {reason}"
        )

    meta = dict(job.meta or {})
    meta["farm_pid"] = None
    meta["resume"] = True
    meta["resumable"] = True
    good = infer_last_good_stage(job.job_dir, fallback=job.stage or "scripting")
    meta["last_good_stage"] = good
    meta["job_health_resume_at"] = datetime.now(timezone.utc).isoformat()
    meta["job_health_reason"] = reason[:400]
    meta.pop("repair_blocked", None)
    meta.pop("repair_exhausted", None)

    job_dir = Path(job.job_dir) if job.job_dir else None
    # final.mp4 on disk → publish-only (no RunPod re-stills)
    if (
        good == "publish"
        and job_dir
        and (job_dir / "video" / "final.mp4").is_file()
        and _publish_wanted(job)
        and not job.video_id
        and spawn
    ):
        return _finish_publish_for_job(
            job, job_dir, store=store, ledger=ledger, reason=reason
        )

    # Post-compose: visuals+resume re-enters pipeline at stills but skips assets.
    if good == "publish":
        meta["farm_phase"] = "visuals"
    elif good == "edit":
        meta["farm_phase"] = "visuals"
    else:
        meta.setdefault("farm_phase", good if good in {"prep", "visuals"} else "visuals")

    store.update_job(
        job.id,
        status="queued",
        stage="queued",
        error=None,
        meta=meta,
    )
    ledger.write(
        agent="job_health_watchdog",
        problem=reason[:240],
        action=f"resume queued from {good}",
        job_id=job.id,
        severity="warn",
        publish_status="queued",
    )
    out: dict[str, Any] = {
        "job_id": job.id,
        "action": "resume_queued",
        "last_good_stage": good,
        "reason": reason,
    }
    if not spawn:
        out["spawned"] = False
        return out

    post_pod = good == "publish" and bool(job.job_dir)
    spawned = spawn_farm_job(
        job.id,
        store=store,
        phase=meta.get("farm_phase"),
        skip_capacity_gate=post_pod,
    )
    out["spawn"] = spawned
    out["spawned"] = bool(spawned.get("spawned") or spawned.get("already_running"))
    return out


def heal_job(
    job: JobRecord,
    *,
    store: OpsStore | None = None,
    ledger: OpsLedger | None = None,
    spawn: bool = True,
) -> dict[str, Any] | None:
    """Apply health rules to one job; return action dict or None if healthy."""
    store = store or OpsStore()
    ledger = ledger or OpsLedger(store)
    cfg = _cfg()

    if cfg.get("enabled", True) is False:
        return None
    if _job_skipped(job):
        return None
    if _publish_terminal_status(job):
        return None

    meta = dict(job.meta or {})
    status = (job.status or "").lower()
    stage = (job.stage or "").lower()
    job_dir = Path(job.job_dir) if job.job_dir else None
    actions: list[str] = []

    # --- Rule 3: duplicate farm PIDs on same job ---
    farm_pids = _farm_pids_for_job(job.id)
    recorded = meta.get("farm_pid")
    if recorded and pid_alive(recorded):
        farm_pids = [p for p in farm_pids if p != int(recorded)]
    if len(farm_pids) > 1:
        keep = farm_pids[0]
        dupes = farm_pids[1:]
        killed = _kill_pids(dupes)
        actions.append(f"kill_duplicate_farm_pids kept={keep} killed={killed}")
        meta["farm_pid"] = keep
        store.update_job(job.id, meta=meta)
        ledger.write(
            agent="job_health_watchdog",
            problem=f"duplicate farm PIDs job={job.id}",
            action=f"kept pid={keep} killed={killed}",
            job_id=job.id,
            severity="warn",
        )
        return {"job_id": job.id, "rule": "duplicate_farm_pids", "killed": killed}

    # Duplicate orphan ffmpeg on same job dir (e.g. twin SFX mixes)
    if job_dir:
        ff_all = _ffmpeg_pids_for_job(job_dir)
        if len(ff_all) > 1:
            keep_ff = max(ff_all)  # prefer newest
            dupes_ff = [p for p in ff_all if p != keep_ff]
            killed_ff = _kill_pids(dupes_ff)
            ledger.write(
                agent="job_health_watchdog",
                problem=f"duplicate ffmpeg job={job.id}",
                action=f"kept={keep_ff} killed={killed_ff}",
                job_id=job.id,
                severity="warn",
            )
            actions.append(f"duplicate_ffmpeg kept={keep_ff} killed={killed_ff}")

    if len(farm_pids) == 1 and not pid_alive(meta.get("farm_pid")):
        meta["farm_pid"] = farm_pids[0]
        store.update_job(job.id, meta=meta)

    # Live farm — nothing to heal for dead-farm rules
    if pid_alive(meta.get("farm_pid")):
        return None

    video_dir = job_dir / "video" if job_dir else None
    final_mp4 = video_dir / "final.mp4" if video_dir else None
    sfx_tmp = video_dir / "final_sfx_tmp.mp4" if video_dir else None

    # --- Rule 5: hung ffmpeg (any expected output mtime stuck >30min) ---
    hung_paths: list[Path] = []
    if sfx_tmp and sfx_tmp.is_file():
        hung_paths.append(sfx_tmp)
    if final_mp4 and final_mp4.is_file() and status in ACTIVE_FARM_STATUSES:
        hung_paths.append(final_mp4)

    for hp in hung_paths:
        key = f"hung:{hp.name}"
        stalled, _ = _mtime_stalled(hp, meta, stall_s=HUNG_FFMPEG_S, key=key)
        ff_pids = _ffmpeg_pids_for_job(job_dir) if job_dir else []
        if stalled and ff_pids:
            killed = _kill_pids(ff_pids)
            if hp.name == "final_sfx_tmp.mp4":
                try:
                    hp.unlink(missing_ok=True)
                except OSError:
                    pass
            actions.append(f"hung_ffmpeg killed={killed} path={hp.name}")
            store.update_job(job.id, meta=meta)
            ledger.write(
                agent="job_health_watchdog",
                problem=f"hung ffmpeg >{HUNG_FFMPEG_S}s job={job.id}",
                action=f"killed={killed} retry stage",
                job_id=job.id,
                severity="warn",
            )
            break

    # --- Rule 2: SFX stall ---
    if job_dir and final_mp4 and final_mp4.is_file() and _needs_compose_sfx(job):
        if not _sfx_applied(job_dir):
            ff_pids = _ffmpeg_pids_for_job(job_dir)
            sfx_stalled = False
            if sfx_tmp and sfx_tmp.is_file():
                final_dur = _probe_duration(final_mp4)
                tmp_dur = _probe_duration(sfx_tmp)
                if tmp_dur > 0 and final_dur > 0 and tmp_dur + 1.0 < final_dur:
                    sfx_stalled = True
                    actions.append(
                        f"sfx_partial dur={tmp_dur:.1f}<{final_dur:.1f}"
                    )
                elif final_dur > 0 and sfx_tmp.stat().st_size > 1000:
                    # Partial mux (no moov yet) — stall on byte growth
                    size_checks = dict(meta.get("job_health_size_checks") or {})
                    prev_sz = size_checks.get("sfx_tmp_bytes")
                    cur_sz = sfx_tmp.stat().st_size
                    size_checks["sfx_tmp_bytes"] = cur_sz
                    meta["job_health_size_checks"] = size_checks
                    if prev_sz is not None and cur_sz == prev_sz and ff_pids:
                        key = "sfx:final_sfx_tmp"
                        m_stalled, _ = _mtime_stalled(
                            sfx_tmp, meta, stall_s=SFX_STALL_S, key=key
                        )
                        if m_stalled:
                            sfx_stalled = True
                            actions.append(f"sfx_bytes_stuck>{SFX_STALL_S}s")
                key = "sfx:final_sfx_tmp"
                if not sfx_stalled:
                    m_stalled, _ = _mtime_stalled(
                        sfx_tmp, meta, stall_s=SFX_STALL_S, key=key
                    )
                    if m_stalled:
                        sfx_stalled = True
                        actions.append(f"sfx_mtime_stuck>{SFX_STALL_S}s")
            if sfx_stalled:
                if ff_pids:
                    _kill_pids(ff_pids)
                if sfx_tmp and sfx_tmp.is_file():
                    sfx_tmp.unlink(missing_ok=True)
                store.update_job(job.id, meta=meta)
                sfx_result = _run_sfx_for_job(job, job_dir)
                if sfx_result.get("ok"):
                    actions.append("sfx_retried")
                    ledger.write(
                        agent="job_health_watchdog",
                        problem=f"SFX stall job={job.id}",
                        action="deleted partial + retried SFX",
                        job_id=job.id,
                        severity="warn",
                    )
                    if _publish_wanted(job) and not job.video_id:
                        return _resume_job(
                            job,
                            store=store,
                            ledger=ledger,
                            reason="SFX retried — resume publish",
                            spawn=spawn,
                        )
                    return {"job_id": job.id, "rule": "sfx_stall", "sfx": sfx_result}
                return {
                    "job_id": job.id,
                    "rule": "sfx_stall",
                    "sfx_error": sfx_result.get("error"),
                }
            prev_orphans = meta.get("sfx_orphan_pids") or []
            if prev_orphans and not ff_pids:
                meta.pop("sfx_orphan_pids", None)
                store.update_job(job.id, meta=meta)
                if _sfx_applied(job_dir):
                    return _resume_job(
                        job,
                        store=store,
                        ledger=ledger,
                        reason="orphan SFX finished — publish resume",
                        spawn=spawn,
                    )
                sfx_result = _run_sfx_for_job(job, job_dir)
                if sfx_result.get("ok") and _publish_wanted(job) and not job.video_id:
                    return _resume_job(
                        job,
                        store=store,
                        ledger=ledger,
                        reason="orphan SFX failed — inline retry + publish",
                        spawn=spawn,
                    )
            elif ff_pids:
                # Orphan SFX ffmpeg still running — wait unless publish overdue
                meta["sfx_orphan_pids"] = ff_pids
                store.update_job(job.id, meta=meta)
                if _publish_wanted(job) and not job.video_id and _schedule_passed(job):
                    # Schedule missed — kill stale orphan and retry SFX inline
                    _kill_pids(ff_pids)
                    if sfx_tmp and sfx_tmp.is_file():
                        sfx_tmp.unlink(missing_ok=True)
                    sfx_result = _run_sfx_for_job(job, job_dir)
                    if sfx_result.get("ok"):
                        return _resume_job(
                            job,
                            store=store,
                            ledger=ledger,
                            reason="missed schedule — SFX+publish resume",
                            spawn=spawn,
                        )
                return None

    store.update_job(job.id, meta=meta)

    # --- Rule 4: compose done but no upload ---
    if (
        job_dir
        and final_mp4
        and final_mp4.is_file()
        and final_mp4.stat().st_size > 1000
        and not job.video_id
        and _publish_wanted(job)
    ):
        sfx_ok = not _needs_compose_sfx(job) or _sfx_applied(job_dir)
        if sfx_ok:
            overdue = _schedule_passed(job) or status in ACTIVE_FARM_STATUSES
            if overdue or stage in {"edit", "edit_done", "gate_a", "gate_b", "farming"}:
                return _resume_job(
                    job,
                    store=store,
                    ledger=ledger,
                    reason="final.mp4 ready — upload+schedule resume",
                    spawn=spawn,
                )

    # --- Rule 1: dead farm (farming/active status, farm_pid null/dead) ---
    if status in ACTIVE_FARM_STATUSES or stage in ACTIVE_FARM_STATUSES:
        if job.video_id:
            return _guard_published_job(
                job,
                store=store,
                ledger=ledger,
                reason=f"dead_farm ignored — already uploaded status={status}",
            )
        if not pid_alive(meta.get("farm_pid")):
            err = meta.get("last_error") or job.error or f"dead farm status={status} stage={stage}"
            return _resume_job(
                job,
                store=store,
                ledger=ledger,
                reason=f"dead_farm: {err[:200]}",
                spawn=spawn,
            )

    if actions:
        return {"job_id": job.id, "actions": actions}
    return None


def scan_and_heal_jobs(
    *,
    store: OpsStore | None = None,
    ledger: OpsLedger | None = None,
    spawn: bool = True,
    job_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Scan all (or selected) jobs; detect and fix health issues."""
    store = store or OpsStore()
    ledger = ledger or OpsLedger(store)
    cfg = _cfg()
    if cfg.get("enabled", True) is False:
        return [{"skipped": True, "reason": "job_health_watchdog disabled"}]

    allow = set(job_ids) if job_ids else None
    results: list[dict[str, Any]] = []
    # Priority: farm_priority first, then oldest updated
    jobs = store.list_jobs()
    jobs.sort(
        key=lambda j: (
            0 if (j.meta or {}).get("farm_priority") else 1,
            j.updated_at or j.created_at or "",
        )
    )
    for job in jobs:
        if allow is not None and job.id not in allow:
            continue
        try:
            action = heal_job(job, store=store, ledger=ledger, spawn=spawn)
            if action:
                results.append(action)
        except Exception as exc:  # noqa: BLE001
            logger.warning("job_health heal failed job=%s: %s", job.id, exc)
            results.append(
                {"job_id": job.id, "ok": False, "error": str(exc)[:300]}
            )
    if results:
        ledger.write(
            agent="job_health_watchdog",
            problem=f"healed n={len(results)}",
            action="scan_and_heal_jobs",
            severity="info",
            extra={"results": results[:20]},
        )
    return results
