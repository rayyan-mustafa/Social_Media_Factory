"""Production farm — spawn Script→TTS→RunPod→FFmpeg→private YouTube for ops jobs.

Sleep beat picks approved titles, then this module starts a detached
``run_farm_job`` process so the 15-minute cron cycle does not block.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from src.agents.cost_guardian import CostGuardian
from src.agents.ledger import OpsLedger
from src.agents.store import OpsStore
from src.agents.title_queue import TitleQueue
from src.agents.watchdog import WatchdogAgent
from src.services.retention_profile import normalize_format
from src.services.settings import CONFIG_DIR, ROOT, get_settings

logger = logging.getLogger(__name__)

# In-flight stages that consume the VPS farm slot (Plan C max_jobs=1).
ACTIVE_FARM_STATUSES = frozenset(
    {
        "queued",
        "farming",
        "scripting",
        "tts",
        "visuals",
        "edit",
        "gate_a",
        "gate_b",
        "running",
    }
)

# Phase A complete — script+voice on disk; waiting for GREEN RunPod (not encoding).
READY_FOR_STILLS_STATUSES = frozenset({"ready_for_stills", "awaiting_gpu"})

# Terminal / waiting-on-human / parked pre-GPU — do not count as concurrent farm work.
IDLE_STATUSES = frozenset(
    {
        "failed",
        "public",
        "done",
        "hold",
        "private",
        "scheduled",
        "ready_for_stills",
        "awaiting_gpu",
    }
)

FarmPhase = Literal["full", "prep", "visuals"]


def normalize_farm_phase(raw: str | None) -> FarmPhase:
    p = (raw or "full").strip().lower()
    if p in {"prep", "voice", "stop_after_voice", "phase_a"}:
        return "prep"
    if p in {"visuals", "from_visuals", "stills", "phase_b", "gpu"}:
        return "visuals"
    return "full"


def list_ready_for_stills(store: OpsStore | None = None) -> list:
    """Ops jobs parked after Phase A (script+Kokoro) awaiting GREEN visuals."""
    store = store or OpsStore()
    out = []
    for job in store.list_jobs():
        st = (job.status or "").lower()
        stage = (job.stage or "").lower()
        if st in READY_FOR_STILLS_STATUSES or stage in READY_FOR_STILLS_STATUSES:
            out.append(job)
    # Oldest first so FIFO consumes prep buffer
    out.sort(key=lambda j: j.updated_at or j.created_at or "")
    return out


def count_ready_for_stills(store: OpsStore | None = None) -> int:
    return len(list_ready_for_stills(store))


def voice_artifacts_ready(job_dir: Path | str | None) -> bool:
    if not job_dir:
        return False
    root = Path(job_dir)
    return (root / "script" / "script.json").exists() and (
        root / "audio" / "voice_manifest.json"
    ).exists()


def content_kernel_ready(job_dir: Path | str | None) -> bool:
    """True when Phase A reusable assets exist (no RunPod required).

    Kernel: ``script/script.json``, ``script/narration.txt`` (optional but
    preferred), ``audio/voice_manifest.json``. Same gate as stills resume for
    the required pair; narration.txt is soft (written by pipeline when present).
    """
    if not voice_artifacts_ready(job_dir):
        return False
    return True


def mark_ready_for_derivatives(meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Stamp ops job meta after Phase A — hook for future text/audio modules.

    Does **not** start TikTok/ebook/etc. See ``output/ops/CONTENT_REUSE.md``.
    """
    out = dict(meta or {})
    out["ready_for_derivatives"] = True
    out["content_kernel"] = True
    out.setdefault(
        "derivative_modules_planned",
        ["podcast_audio", "blog_newsletter", "ebook_pdf", "tiktok_shorts_text"],
    )
    return out


# Classes that stay HOLD for a human (no auto-resume). Others are resumable.
BLOCKED_HOLD_CLASSES = frozenset({"auth", "budget", "publish"})
# Watchdog-owned: park until next GREEN — repair must not create-retry.
CAPACITY_HOLD_CLASS = "capacity_out"


def is_capacity_hold_error(error: str | None) -> bool:
    """True when failure is stock/GREEN gate — park, do not grind creates."""
    try:
        from src.agents.repair_watchdog import CAPACITY_HOLD_CLASSES, classify_failure

        return classify_failure(error or "") in CAPACITY_HOLD_CLASSES
    except Exception:  # noqa: BLE001
        text = (error or "").lower()
        return any(
            k in text
            for k in (
                "out_of_stock",
                "supply_constraint",
                "capacity pre-flight deferred",
                "farm spawn blocked",
                "farm blocked (green-light only)",
                "wait for next green",
            )
        )


def park_for_capacity(
    job_id: str,
    *,
    error: str,
    store: OpsStore | None = None,
    queue: TitleQueue | None = None,
    watchdog: WatchdogAgent | None = None,
    ledger: OpsLedger | None = None,
    reason: str = "capacity_out",
    sync_sheet: bool = True,
) -> dict[str, Any]:
    """Park a video until the next GREEN tick — no create retries.

    Prefer ``ready_for_stills`` when Phase A artifacts exist so the capacity
    watchdog Phase B owns resume. Otherwise HOLD with ``hold_class=capacity_out``
    (excluded from repair AUTO_RESUME).
    """
    store = store or OpsStore()
    job = store.get_job(job_id)
    if not job:
        return {"ok": False, "error": f"job not found: {job_id}"}

    err = (error or "capacity unavailable — wait for GREEN")[:1200]
    good_stage = infer_last_good_stage(
        job.job_dir,
        fallback=(job.stage if job.stage not in {"failed", "hold"} else "visuals"),
    )
    voice_ok = voice_artifacts_ready(job.job_dir)
    meta = dict(job.meta or {})
    meta["farm_pid"] = None
    meta["hold_class"] = CAPACITY_HOLD_CLASS
    meta["last_repair_class"] = CAPACITY_HOLD_CLASS
    meta["awaiting_green"] = True
    meta["capacity_hold"] = True
    meta["last_error"] = err[:800]
    meta["hold_at"] = datetime.now(timezone.utc).isoformat()
    meta["hold_reason"] = (reason or CAPACITY_HOLD_CLASS)[:400]
    meta["last_good_stage"] = good_stage
    meta["resumable"] = True
    meta["resume"] = True
    meta.pop("repair_blocked", None)
    meta.pop("repair_exhausted", None)
    meta["note"] = "HOLD_CAPACITY — awaiting GREEN (watchdog; no create retries)"

    if voice_ok:
        status = "ready_for_stills"
        stage = "awaiting_gpu"
        sheet_status = "hold"
        sheet_note = (
            f"HOLD_CAPACITY awaiting GREEN stage={good_stage}: {err}"
        )[:500]
    else:
        status = "hold"
        stage = "hold"
        sheet_status = "hold"
        sheet_note = (
            f"HOLD:{CAPACITY_HOLD_CLASS} stage={good_stage} awaiting_green: {err}"
        )[:500]

    store.update_job(
        job_id,
        status=status,
        stage=stage,
        error=err,
        meta=meta,
        job_dir=job.job_dir,
    )
    if watchdog is not None:
        watchdog.on_stage(
            job_id,
            stage,
            status=status,
            error=err,
            meta={
                "resumable": True,
                "hold_class": CAPACITY_HOLD_CLASS,
                "awaiting_green": True,
                "last_good_stage": good_stage,
            },
        )
    if sync_sheet:
        queue = queue or TitleQueue()
        try:
            row = queue.find_by_job_id(job_id)
            if row:
                queue.update_row(
                    row.row_index,
                    status=sheet_status,
                    notes=sheet_note,
                    channel=getattr(row, "channel", None) or None,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("title queue sync on capacity park failed: %s", exc)
    if ledger is not None:
        try:
            ledger.write(
                agent="farm",
                problem=f"HOLD_CAPACITY: {err[:200]}",
                action="park until next GREEN (watchdog; no create retries)",
                job_id=job_id,
                severity="warn",
                extra={
                    "hold_class": CAPACITY_HOLD_CLASS,
                    "awaiting_green": True,
                    "status": status,
                    "last_good_stage": good_stage,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("capacity park ledger failed: %s", exc)
    logger.warning(
        "park_for_capacity job=%s status=%s stage=%s — %s",
        job_id,
        status,
        stage,
        err[:160],
    )
    return {
        "ok": True,
        "job_id": job_id,
        "status": status,
        "stage": stage,
        "hold_class": CAPACITY_HOLD_CLASS,
        "awaiting_green": True,
        "resumable": True,
        "last_good_stage": good_stage,
        "voice_ready": voice_ok,
    }


def infer_last_good_stage(job_dir: Path | str | None, *, fallback: str = "scripting") -> str:
    """Best resume stage from on-disk artifacts (script → stills → compose)."""
    if not job_dir:
        return fallback
    root = Path(job_dir)
    try:
        final = root / "video" / "final.mp4"
        if final.is_file() and final.stat().st_size > 1000:
            return "publish"
        images = root / "images"
        if images.is_dir() and (
            any(images.glob("scene_*.jpg")) or any(images.glob("scene_*.png"))
        ):
            return "edit"
        if voice_artifacts_ready(root):
            return "visuals"
        script = root / "script" / "script.json"
        if script.is_file() and script.stat().st_size > 2:
            return "tts"
    except OSError:
        return fallback
    return fallback


def stamp_hold_resume_meta(
    meta: dict[str, Any] | None,
    *,
    error: str,
    hold_class: str,
    last_good_stage: str,
    repair_blocked: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    """Attach HOLD resume metadata (stage, class, attempt) for watchdog repair."""
    out = dict(meta or {})
    out["farm_pid"] = None
    out["hold_class"] = hold_class
    out["last_good_stage"] = last_good_stage
    out["last_error"] = (error or "")[:800]
    out["hold_at"] = datetime.now(timezone.utc).isoformat()
    out["hold_attempt"] = int(out.get("hold_attempt") or 0) + 1
    if reason:
        out["hold_reason"] = reason[:400]
    if repair_blocked or hold_class in BLOCKED_HOLD_CLASSES:
        out["repair_blocked"] = True
        out["repair_block_reason"] = reason or out.get("repair_block_reason") or hold_class
        out["resumable"] = False
        out["resume"] = False
    else:
        out["resumable"] = True
        out["resume"] = True
        # Fresh recoverable HOLD — clear prior block so repair can run again.
        out.pop("repair_blocked", None)
        out.pop("repair_block_reason", None)
        out.pop("repair_exhausted", None)
    return out


def hold_farm_job(
    job_id: str,
    *,
    error: str,
    store: OpsStore | None = None,
    queue: TitleQueue | None = None,
    watchdog: WatchdogAgent | None = None,
    ledger: OpsLedger | None = None,
    hold_class: str | None = None,
    last_good_stage: str | None = None,
    repair_blocked: bool | None = None,
    reason: str = "",
    sync_sheet: bool = True,
) -> dict[str, Any]:
    """Park a farm job as HOLD (prefer over terminal failed) with resume meta.

    Recoverable classes stay ``resumable=True`` for factory/watchdog repair.
    Auth / budget / publish (or explicit ``repair_blocked``) stay HOLD for human.
    """
    store = store or OpsStore()
    job = store.get_job(job_id)
    if not job:
        return {"ok": False, "error": f"job not found: {job_id}"}

    # Lazy import avoids circular import at module load.
    if hold_class is None:
        try:
            from src.agents.repair_watchdog import classify_failure, read_farm_log_tail

            hold_class = classify_failure(error or "", read_farm_log_tail(job))
        except Exception:  # noqa: BLE001
            hold_class = "unknown"
    hold_class = (hold_class or "unknown").strip().lower() or "unknown"

    stage_hint = last_good_stage or (job.stage if job.stage not in {"failed", "hold"} else None)
    good_stage = infer_last_good_stage(
        job.job_dir, fallback=str(stage_hint or "scripting")
    )
    blocked = (
        bool(repair_blocked)
        if repair_blocked is not None
        else hold_class in BLOCKED_HOLD_CLASSES
    )
    meta = stamp_hold_resume_meta(
        job.meta,
        error=error,
        hold_class=hold_class,
        last_good_stage=good_stage,
        repair_blocked=blocked,
        reason=reason or f"hold:{hold_class}",
    )
    store.update_job(
        job_id,
        status="hold",
        stage="hold",
        error=error,
        meta=meta,
        job_dir=job.job_dir,
    )
    if watchdog is not None:
        watchdog.on_stage(
            job_id,
            "hold",
            status="hold",
            error=error,
            meta={
                "resumable": meta.get("resumable"),
                "hold_class": hold_class,
                "last_good_stage": good_stage,
            },
        )
    if sync_sheet:
        queue = queue or TitleQueue()
        try:
            row = queue.find_by_job_id(job_id)
            if row:
                note = (
                    f"HOLD:{hold_class} stage={good_stage} resumable={meta.get('resumable')}: "
                    f"{error}"
                )[:500]
                queue.update_row(
                    row.row_index,
                    status="hold",
                    notes=note,
                    channel=getattr(row, "channel", None) or None,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("title queue sync on hold failed: %s", exc)
    if ledger is not None:
        try:
            ledger.write(
                agent="farm",
                problem=f"HOLD class={hold_class}: {(error or '')[:200]}",
                action=(
                    "HOLD repair_blocked — human"
                    if blocked
                    else f"HOLD resumable from {good_stage}"
                ),
                job_id=job_id,
                severity="warn" if not blocked else "critical",
                publish_status="hold",
                extra={
                    "hold_class": hold_class,
                    "last_good_stage": good_stage,
                    "resumable": meta.get("resumable"),
                    "hold_attempt": meta.get("hold_attempt"),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ledger write on hold failed: %s", exc)
    return {
        "ok": True,
        "job_id": job_id,
        "status": "hold",
        "hold_class": hold_class,
        "last_good_stage": good_stage,
        "resumable": bool(meta.get("resumable")),
        "repair_blocked": blocked,
    }


def list_resumable_hold_jobs(
    store: OpsStore | None = None,
    *,
    require_explicit: bool = False,
) -> list:
    """HOLD or failed jobs that repair/watchdog may resume (not repair_blocked).

    ``require_explicit=True`` (capacity watchdog start path): only jobs stamped
    ``resumable=True`` — legacy parked HOLD/failed without the flag stay out of
    the auto-resume lane so one ancient row cannot block new approved titles.
    """
    store = store or OpsStore()
    out = []
    for job in store.list_jobs():
        st = (job.status or "").lower()
        if st not in {"hold", "failed"}:
            continue
        meta = job.meta or {}
        if meta.get("repair_blocked") or meta.get("repair_exhausted"):
            continue
        # Explicit resumable flag OR legacy failed without block.
        if st == "hold" and meta.get("resumable") is False:
            continue
        if require_explicit and meta.get("resumable") is not True:
            continue
        if pid_alive(meta.get("farm_pid")):
            continue
        out.append(job)
    out.sort(key=lambda j: j.updated_at or j.created_at or "")
    return out


def list_ready_for_derivatives(store: OpsStore | None = None) -> list:
    """Jobs with Phase A kernel parked (meta flag or ready_for_stills + artifacts)."""
    store = store or OpsStore()
    out = []
    for job in list_ready_for_stills(store):
        meta = job.meta or {}
        if meta.get("ready_for_derivatives") or content_kernel_ready(job.job_dir):
            out.append(job)
    return out


def _agents_cfg() -> dict[str, Any]:
    path = CONFIG_DIR / "agents_settings.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _sleep_beat_cfg() -> dict[str, Any]:
    return dict((_agents_cfg().get("sleep_beat") or {}))


def _slug(text: str, max_len: int = 48) -> str:
    s = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text.strip())
    s = "_".join(p for p in s.split("_") if p)
    return (s or "job")[:max_len]


def pid_alive(pid: int | None) -> bool:
    if not pid or int(pid) <= 0:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _is_encoding_job(job) -> bool:
    """True when a farm process is running or mid-pipeline (not idle queued).

    Requires a **live** ``farm_pid``. Zombie rows (``farming``/``tts`` with
    ``farm_pid=None`` or a dead PID after SIGTERM) must not hold ``gpu_lock`` /
    local-CPU prep blocks — that was the Rome TTS stale-lock failure mode.
    """
    if job.status in IDLE_STATUSES:
        return False
    meta = job.meta or {}
    pid = meta.get("farm_pid")
    if pid_alive(pid):
        return True
    # Dead or missing PID ⇒ not actively encoding (stale / crashed / cleared).
    return False


def job_farm_phase(job) -> FarmPhase:
    """Resolve farm phase from meta (default full — GPU path)."""
    return normalize_farm_phase((job.meta or {}).get("farm_phase"))


# After stills the pod is dead — compose/gates are VPS-only and must not
# hold ``gpu_lock`` (Video B can take RunPod while Video A ffmpeg's locally).
_GPU_POST_POD_STAGES = frozenset({"edit", "gate_a", "gate_b"})


def _job_past_gpu_pod(job) -> bool:
    """True when stills finished — compose/publish needs no pod / no gpu_lock.

    Signals (any):
    - stage is edit/gate_*
    - meta ``gpu_lock_released``
    - ``images/visual_manifest.json`` exists (written only after VisualModule
      completes; ephemeral pod is already terminated by then)
    """
    stage = (job.stage or "").lower()
    if stage in _GPU_POST_POD_STAGES:
        return True
    meta = job.meta or {}
    if meta.get("gpu_lock_released"):
        return True
    jd = (job.job_dir or "").strip()
    if not jd:
        return False
    return (Path(jd) / "images" / "visual_manifest.json").exists()


def _is_gpu_encoding_job(job) -> bool:
    """True when an encoding job holds the GPU/pod lock (full or visuals).

    Compose after stills does **not** hold the lock — pod is already gone.
    """
    if not _is_encoding_job(job):
        return False
    if job_farm_phase(job) == "prep":
        return False
    if _job_past_gpu_pod(job):
        return False
    return True


def _is_prep_encoding_job(job) -> bool:
    """True when Phase A (script+Kokoro) is mid-flight — no pod."""
    if not _is_encoding_job(job):
        return False
    return job_farm_phase(job) == "prep"


# Local VPS-heavy stages on a GPU-path job — do not also start Kokoro prep.
_GPU_LOCAL_HEAVY_STAGES = frozenset(
    {"scripting", "tts", "edit", "gate_a", "gate_b", "running"}
)


def _is_gpu_path_midflight(job) -> bool:
    """Full/visuals farm process alive (including local compose after pod)."""
    if job.status in IDLE_STATUSES:
        return False
    if job_farm_phase(job) == "prep":
        return False
    return _is_encoding_job(job)


def _gpu_job_holds_local_cpu(job) -> bool:
    """True when a GPU-path job is on VPS CPU (script/TTS/compose), not pod stills.

    Prep may overlap RunPod ``visuals`` (VPS mostly waiting on the pod). Overlap
    during scripting/TTS/ffmpeg risks OOM on 4vCPU/8GB.

    Note: compose releases ``gpu_lock`` but still holds local CPU — prep stays
    blocked; GREEN may start Video B visuals/full on the free pod slot.
    """
    if not _is_gpu_path_midflight(job):
        return False
    stage = (job.stage or "").lower()
    if stage == "visuals" and not _job_past_gpu_pod(job):
        return False
    if stage in _GPU_LOCAL_HEAVY_STAGES or stage in _GPU_POST_POD_STAGES:
        return True
    if _job_past_gpu_pod(job):
        return True
    # Unknown / farming without stage detail — be conservative.
    return True


def count_encoding_jobs(store: OpsStore | None = None) -> int:
    """Jobs with an active encode / live farm PID (any phase)."""
    store = store or OpsStore()
    return sum(1 for j in store.list_jobs() if _is_encoding_job(j))


def count_gpu_jobs(store: OpsStore | None = None) -> int:
    """Jobs holding the GPU lock (full/visuals — at most one pod).

    **Global across channels** — napstorian + napping_historian share one
    ``gpu_lock`` (counts every ops job, never scoped per sheet tab).
    """
    store = store or OpsStore()
    return sum(1 for j in store.list_jobs() if _is_gpu_encoding_job(j))


def count_prep_jobs(store: OpsStore | None = None) -> int:
    """Jobs holding the prep lock (Phase A script+Kokoro only).

    **Global across channels** — same shared ``prep_lock`` for both tabs.
    """
    store = store or OpsStore()
    return sum(1 for j in store.list_jobs() if _is_prep_encoding_job(j))


def count_inflight_jobs(store: OpsStore | None = None) -> int:
    """GPU-path slots used (encoding GPU + GPU-bound queued waiting to spawn).

    Prep jobs (and prep-phase queued repairs) do **not** consume the GPU
    inflight slot so GREEN can start visuals while Phase A runs (when
    ``RUNPOD_PREP_WHILE_GPU=1``). Local compose after stills also does **not**
    consume the slot (pod free).

    Only ``queued`` rows with farm_phase full/visuals reserve the slot —
    script/prep deferred queues must not fake ``gpu_lock busy``.
    """
    store = store or OpsStore()
    n = 0
    for job in store.list_jobs():
        if job.status in IDLE_STATUSES:
            continue
        if job_farm_phase(job) == "prep":
            continue
        if _job_past_gpu_pod(job):
            continue
        if _is_prep_encoding_job(job):
            continue
        if _is_encoding_job(job):
            n += 1
            continue
        status = (job.status or "").lower()
        if status == "queued":
            # Script/prep repairs often sit queued under farm_phase=full by
            # mistake; treat hold_class script* as non-GPU reserved.
            meta = job.meta or {}
            hold_c = str(meta.get("hold_class") or meta.get("last_repair_class") or "").lower()
            if hold_c in {"script", "script_json"}:
                continue
            n += 1
    return n


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_truthy(name: str, default: str = "0") -> bool:
    raw = (os.getenv(name) if name in os.environ else default) or default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def max_concurrent_jobs() -> int:
    """Alias for max GPU concurrent (backward compatible)."""
    return max_gpu_concurrent()


def max_gpu_concurrent() -> int:
    """Max simultaneous GPU/pod jobs (**fleet-wide**, both channels).

    **Hard rule:** always ``1``. Video A on RunPod (any channel) ⇒ Video B on
    the other channel cannot create a second pod. ``MAX_GPU_CONCURRENT`` /
    sleep_beat values >1 are clamped so a mis-set env can never open a
    second GPU slot.
    """
    if "MAX_GPU_CONCURRENT" in os.environ:
        requested = max(1, _env_int("MAX_GPU_CONCURRENT", 1))
    else:
        beat = _sleep_beat_cfg()
        cfg = _agents_cfg()
        requested = max(
            1,
            int(beat.get("max_concurrent_jobs") or cfg.get("max_jobs_per_pick") or 1),
        )
    if requested > 1:
        logger.warning(
            "MAX_GPU_CONCURRENT=%s clamped to 1 (one-pod hard lock — never 2 pods)",
            requested,
        )
    return 1


def max_prep_concurrent() -> int:
    """Max simultaneous Phase A prep jobs (**fleet-wide**, both channels).

    Default 1 on 4vCPU/8GB — shared ``prep_lock``, not 1 per channel.
    """
    return max(0, _env_int("MAX_PREP_CONCURRENT", 1))


def prep_while_gpu_enabled() -> bool:
    """Allow script+TTS prep while a GPU stills job is running (default on)."""
    if "RUNPOD_PREP_WHILE_GPU" in os.environ:
        return _env_truthy("RUNPOD_PREP_WHILE_GPU", "1")
    return True


def gpu_slot_available(
    store: OpsStore | None = None, *, exclude_job_id: str | None = None
) -> tuple[bool, str]:
    """Shared GPU slot across napstorian + napping_historian (not per-channel)."""
    store = store or OpsStore()
    gpu_n = 0
    for j in store.list_jobs():
        if exclude_job_id and j.id == exclude_job_id:
            continue
        if _is_gpu_encoding_job(j):
            gpu_n += 1
    max_g = max_gpu_concurrent()
    if gpu_n >= max_g:
        return False, f"gpu_lock busy gpu={gpu_n}>={max_g}"
    return True, f"gpu_lock free gpu={gpu_n}/{max_g}"


def prep_slot_available(
    store: OpsStore | None = None, *, exclude_job_id: str | None = None
) -> tuple[bool, str]:
    """Whether a new Phase A prep spawn is allowed (separate from gpu_lock).

    Shared across both channels. With ``RUNPOD_PREP_WHILE_GPU=1``, prep may
    start only when the GPU job is pod-bound (``stage=visuals``) — not while
    it is also doing local script/TTS or ffmpeg compose.
    """
    store = store or OpsStore()
    prep_n = 0
    gpu_n = 0
    gpu_local_n = 0
    for j in store.list_jobs():
        if exclude_job_id and j.id == exclude_job_id:
            continue
        if _is_prep_encoding_job(j):
            prep_n += 1
            continue
        if _is_gpu_encoding_job(j):
            gpu_n += 1
        # Compose after pod release still burns VPS CPU — block Kokoro overlap.
        if _gpu_job_holds_local_cpu(j):
            gpu_local_n += 1
    max_p = max_prep_concurrent()
    if max_p <= 0:
        return False, "MAX_PREP_CONCURRENT=0"
    if prep_n >= max_p:
        return False, f"prep_lock busy prep={prep_n}>={max_p}"
    if not prep_while_gpu_enabled() and gpu_n > 0:
        return False, f"RUNPOD_PREP_WHILE_GPU=0 and gpu={gpu_n}"
    if prep_while_gpu_enabled() and gpu_local_n > 0:
        return False, (
            f"gpu job on local VPS stage (script/TTS/compose) — "
            f"wait for visuals or idle (gpu_local={gpu_local_n})"
        )
    return True, f"prep_lock free prep={prep_n}/{max_p} gpu={gpu_n}"


def farm_flags_from_config() -> dict[str, Any]:
    """Production defaults from sleep_beat (real RunPod + private upload)."""
    beat = _sleep_beat_cfg()
    return {
        "mock_images": bool(beat.get("mock_images", False)),
        "publish": bool(beat.get("publish", True)),
        "publish_dry_run": bool(beat.get("publish_dry_run", False)),
        "test_mode": bool(beat.get("test_mode", False)),
    }


def spawn_farm_job(
    job_id: str,
    *,
    store: OpsStore | None = None,
    watchdog: WatchdogAgent | None = None,
    mock_images: bool | None = None,
    publish: bool | None = None,
    publish_dry_run: bool | None = None,
    test_mode: bool | None = None,
    resume: bool | None = None,
    phase: str | None = None,
    skip_capacity_gate: bool = False,
) -> dict[str, Any]:
    """Detach ``run_farm_job`` for an ops job. Returns immediately.

    Hard gate: RunPod stills farm is **GREEN-light only** for ``full`` / ``visuals``.
    Phase ``prep`` (script+Kokoro only) skips the capacity gate — CPU/local work
    may run on RED/YELLOW so GREEN windows go straight to stills.

    ``skip_capacity_gate`` is for unit tests only — never use in production cron.
    """
    store = store or OpsStore()
    watchdog = watchdog or WatchdogAgent(store)
    job = store.get_job(job_id)
    if not job:
        return {"ok": False, "error": f"job not found: {job_id}"}

    # Mandatory: every Brand-channel farm spawn carries latest new-format stamps
    # BEFORE Popen so the child never loads a stale/unstamped job record.
    from src.agents.smm_sop import (
        is_new_format_stamped,
        stamp_new_format_sop_checklist,
    )

    meta = dict(job.meta or {})
    ch_early = (meta.get("channel") or meta.get("sheet_tab") or "").strip()
    meta = stamp_new_format_sop_checklist(
        meta,
        channel=ch_early or None,
        stage="spawn_pre",
    )
    if not is_new_format_stamped(meta):
        return {
            "ok": False,
            "spawned": False,
            "error": "new-format SOP stamp failed — refusing farm spawn",
            "job_id": job_id,
        }
    store.update_job(job_id, meta=meta)
    job = store.get_job(job_id) or job

    # Load .env kill switches into this process before preflight (cron may not export).
    try:
        from dotenv import dotenv_values

        for key, raw in (dotenv_values(ROOT / ".env") or {}).items():
            if key in {"ASSET_FETCHER", "FARM_REQUIRE_VISION", "IMAGE_BACKEND"} and raw:
                os.environ.setdefault(key, str(raw).strip())
    except Exception:  # noqa: BLE001
        pass

    farm_phase = normalize_farm_phase(
        phase if phase is not None else meta.get("farm_phase")
    )
    # Auto-detect Phase B when parked ready_for_stills
    st = (job.status or "").lower()
    if (
        farm_phase == "full"
        and st in READY_FOR_STILLS_STATUSES
        and voice_artifacts_ready(job.job_dir)
    ):
        farm_phase = "visuals"

    existing_pid = meta.get("farm_pid")
    if pid_alive(existing_pid):
        return {
            "ok": True,
            "spawned": False,
            "already_running": True,
            "job_id": job_id,
            "farm_pid": existing_pid,
            "job_dir": job.job_dir,
            "farm_phase": farm_phase,
        }

    # Separate locks: never two pods; prep may overlap GPU when enabled.
    if farm_phase == "prep":
        ok_prep, prep_msg = prep_slot_available(store, exclude_job_id=job_id)
        if not ok_prep:
            return {
                "ok": False,
                "spawned": False,
                "error": f"prep_lock blocked: {prep_msg}",
                "job_id": job_id,
                "farm_phase": farm_phase,
            }
    else:
        ok_gpu, gpu_msg = gpu_slot_available(store, exclude_job_id=job_id)
        if not ok_gpu:
            return {
                "ok": False,
                "spawned": False,
                "error": f"gpu_lock blocked: {gpu_msg}",
                "job_id": job_id,
                "farm_phase": farm_phase,
            }

    # Resource preflight: refuse full/visuals grind when vision / archival /
    # RunPod tools are down — do not burn TTS/GPU/time.
    if farm_phase in {"full", "visuals"} and not skip_capacity_gate:
        try:
            from src.services.farm_preflight import farm_resource_preflight

            pre = farm_resource_preflight(farm_phase=farm_phase)
            meta["farm_preflight"] = {
                "ok": pre.get("ok"),
                "blockers": pre.get("blockers") or [],
                "checks": {
                    k: (v if not isinstance(v, dict) else {kk: v.get(kk) for kk in list(v)[:8]})
                    for k, v in (pre.get("checks") or {}).items()
                },
            }
            store.update_job(job_id, meta=meta)
            if not pre.get("ok"):
                blockers = "; ".join(pre.get("blockers") or ["resource preflight failed"])
                watchdog.record(
                    "farm",
                    "preflight_blocked",
                    f"farm spawn refused — {blockers}",
                    job_id=job_id,
                    severity="error",
                    action="fix vision/archival/runpod then respawn",
                )
                return {
                    "ok": False,
                    "spawned": False,
                    "error": f"resource_preflight blocked: {blockers}",
                    "job_id": job_id,
                    "farm_phase": farm_phase,
                    "preflight": pre,
                }
        except Exception as exc:  # noqa: BLE001
            logger.exception("farm_resource_preflight crashed")
            return {
                "ok": False,
                "spawned": False,
                "error": f"resource_preflight error: {exc}",
                "job_id": job_id,
                "farm_phase": farm_phase,
            }

    # Traffic light: factory never starts GPU phases on YELLOW/RED.
    needs_green = farm_phase in {"full", "visuals"}
    # Post-compose publish resume (final.mp4 on disk) — VPS-only, no pod.
    if (
        needs_green
        and _job_past_gpu_pod(job)
        and infer_last_good_stage(job.job_dir) == "publish"
    ):
        needs_green = False
    if needs_green and not skip_capacity_gate:
        try:
            from src.runpod.capacity import check_farm_capacity_green
            from src.runpod.guards import (
                assert_stills_smoke_gate_for_farm,
                image_backend_is_runpod_pod,
            )

            if image_backend_is_runpod_pod():
                assert_stills_smoke_gate_for_farm()
                ok_green, green_msg, bench = check_farm_capacity_green()
                if not ok_green:
                    cls = (
                        bench.classification if bench is not None else "UNKNOWN"
                    )
                    msg = (
                        f"FARM SPAWN BLOCKED (green-light only): {green_msg}"
                    )
                    logger.error(msg)
                    parked = park_for_capacity(
                        job_id,
                        error=msg,
                        store=store,
                        watchdog=watchdog,
                        ledger=OpsLedger(store),
                        reason=f"spawn_not_green:{cls}",
                    )
                    try:
                        ledger = OpsLedger(store)
                        ledger.write(
                            agent="farm",
                            problem=msg,
                            action="park HOLD_CAPACITY — wait next GREEN",
                            job_id=job_id,
                            severity="warn",
                            extra={
                                "classification": cls,
                                "green_only": True,
                                "farm_phase": farm_phase,
                                "parked": parked.get("status"),
                            },
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    return {
                        "ok": False,
                        "spawned": False,
                        "deferred_spawn": True,
                        "awaiting_green": True,
                        "error": msg,
                        "classification": cls,
                        "green_only": True,
                        "job_id": job_id,
                        "farm_phase": farm_phase,
                        "parked": parked,
                    }
        except Exception as gate_exc:  # noqa: BLE001
            msg = f"FARM SPAWN BLOCKED — capacity/smoke gate: {gate_exc}"
            logger.error(msg)
            parked = park_for_capacity(
                job_id,
                error=msg,
                store=store,
                watchdog=watchdog,
                reason="spawn_capacity_gate_error",
            )
            return {
                "ok": False,
                "spawned": False,
                "deferred_spawn": True,
                "awaiting_green": True,
                "error": msg,
                "green_only": True,
                "job_id": job_id,
                "farm_phase": farm_phase,
                "parked": parked,
            }

    flags = farm_flags_from_config()
    if mock_images is not None:
        flags["mock_images"] = mock_images
    # Job meta wins when caller did not override (short_test samples stamp publish=false).
    if publish is None and "publish" in meta:
        flags["publish"] = bool(meta.get("publish"))
    if publish_dry_run is None and "publish_dry_run" in meta:
        flags["publish_dry_run"] = bool(meta.get("publish_dry_run"))
    if publish is not None:
        flags["publish"] = publish
    if publish_dry_run is not None:
        flags["publish_dry_run"] = publish_dry_run
    if test_mode is not None:
        flags["test_mode"] = test_mode
    if farm_phase == "prep":
        flags["publish"] = False
        flags["publish_dry_run"] = False
    # Scheduled/private recomposes: never insert a duplicate YouTube upload.
    if meta.get("preserve_scheduled_video_id") or meta.get("skip_publish_keep_schedule"):
        flags["publish"] = False
        if publish is True:
            logger.warning(
                "job %s preserve_scheduled_video_id — forcing --no-publish", job_id
            )

    # Resume when explicitly requested, meta says so, or job folder already has script.
    job_dir = Path(job.job_dir) if job.job_dir else (
        ROOT / "output" / "jobs" / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{_slug(job.title)}"
    )
    auto_resume = bool(meta.get("resume")) or (
        (job_dir / "script" / "script.json").exists()
    )
    do_resume = bool(resume) if resume is not None else auto_resume
    if farm_phase == "visuals":
        do_resume = True
        if not voice_artifacts_ready(job_dir):
            return {
                "ok": False,
                "spawned": False,
                "error": (
                    "visuals phase needs script.json + voice_manifest.json "
                    f"under {job_dir}"
                ),
                "job_id": job_id,
                "farm_phase": farm_phase,
            }

    job_dir.mkdir(parents=True, exist_ok=True)
    log_path = ROOT / "output" / "ops" / f"farm_{job_id}.log"

    py = sys.executable
    cmd = [
        py,
        "-m",
        "src.cli.run_farm_job",
        "--job-id",
        job_id,
        "--out-dir",
        str(job_dir),
    ]
    if flags["mock_images"]:
        cmd.append("--mock-images")
    if flags["test_mode"]:
        cmd.append("--test")
    if flags["publish_dry_run"]:
        cmd.append("--publish-dry-run")
    if not flags["publish"]:
        cmd.append("--no-publish")
    if do_resume:
        cmd.append("--resume")
    if farm_phase == "prep":
        cmd.append("--stop-after-voice")
    elif farm_phase == "visuals":
        cmd.append("--from-visuals")

    # Per-job format from sheet → RETENTION_PROFILE (blank → env default / retention)
    profile = normalize_format(
        meta.get("retention_profile") or meta.get("format"),
        default=None,
    )
    channel = (meta.get("channel") or meta.get("sheet_tab") or "").strip()
    child_env = {**os.environ, "RETENTION_PROFILE": profile}
    if channel:
        child_env["SCRIPT_CHANNEL"] = channel
    # Honor .env kill switches even when parent cron didn't export them.
    try:
        from dotenv import dotenv_values

        dotenv_vals = dotenv_values(ROOT / ".env") or {}
        for key in ("ASSET_FETCHER", "FARM_REQUIRE_VISION", "IMAGE_BACKEND"):
            raw = dotenv_vals.get(key)
            if raw is not None and str(raw).strip() != "":
                child_env[key] = str(raw).strip()
                os.environ.setdefault(key, str(raw).strip())
    except Exception:  # noqa: BLE001
        pass
    # Ensure preflight in this process matches child (vision off when fetcher off).
    if str(child_env.get("ASSET_FETCHER", "1")).strip().lower() in {
        "0",
        "false",
        "off",
        "no",
    }:
        os.environ["ASSET_FETCHER"] = "0"
        os.environ["FARM_REQUIRE_VISION"] = child_env.get(
            "FARM_REQUIRE_VISION", "0"
        )

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "a", encoding="utf-8")
    try:
        log_f.write(
            f"\n--- spawn {datetime.now(timezone.utc).isoformat()} "
            f"phase={farm_phase} profile={profile} cmd={' '.join(cmd)} ---\n"
        )
        log_f.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=child_env,
        )
    except Exception as exc:  # noqa: BLE001
        log_f.close()
        hold_farm_job(
            job_id,
            error=f"spawn failed: {exc}",
            store=store,
            watchdog=watchdog,
            hold_class="process_crash",
            reason="spawn_popen_failed",
        )
        return {"ok": False, "error": str(exc), "status": "hold"}
    finally:
        # Child keeps the dup'd fd; parent can close
        try:
            log_f.close()
        except Exception:  # noqa: BLE001
            pass

    start_stage = "visuals" if farm_phase == "visuals" else "scripting"
    meta.update(
        {
            "farm_pid": proc.pid,
            "farm_log": str(log_path),
            "farm_cmd": cmd,
            "farm_spawned_at": datetime.now(timezone.utc).isoformat(),
            "farm_flags": flags,
            "farm_phase": farm_phase,
            "source": meta.get("source") or "title_queue",
            "resume": do_resume,
            "format": profile,
            "retention_profile": profile,
        }
    )
    # Worker/spawn path — stamp SOP checklist early so SMM can verify mid-flight.
    from src.agents.smm_sop import stamp_new_format_sop_checklist

    meta = stamp_new_format_sop_checklist(
        meta,
        channel=meta.get("channel") or meta.get("sheet_tab"),
        stage=f"spawn_{farm_phase}",
    )
    store.update_job(
        job_id,
        status="farming",
        stage=start_stage,
        job_dir=str(job_dir),
        error=None,
        meta=meta,
    )
    watchdog.on_stage(
        job_id,
        start_stage,
        status="farming",
        meta={"farm_pid": proc.pid, "farm_log": str(log_path), "farm_phase": farm_phase},
    )
    logger.info(
        "spawned farm job_id=%s phase=%s pid=%s profile=%s log=%s",
        job_id,
        farm_phase,
        proc.pid,
        profile,
        log_path,
    )
    notes = {
        "prep": "detached Phase A (Script→TTS) — no RunPod",
        "visuals": "detached Phase B (RunPod stills→FFmpeg→private YT)",
        "full": "detached production pipeline (Script→TTS→RunPod→FFmpeg→private YT)",
    }
    return {
        "ok": True,
        "spawned": True,
        "job_id": job_id,
        "farm_pid": proc.pid,
        "job_dir": str(job_dir),
        "farm_log": str(log_path),
        "flags": flags,
        "format": profile,
        "retention_profile": profile,
        "farm_phase": farm_phase,
        "note": notes[farm_phase],
    }


def execute_farm_job(
    job_id: str,
    *,
    out_dir: Path | None = None,
    mock_images: bool = False,
    publish: bool = True,
    publish_dry_run: bool = False,
    test_mode: bool = False,
    voice: str | None = None,
    speed: float | None = None,
    resume: bool | None = None,
    stop_after_voice: bool = False,
    from_visuals: bool = False,
) -> dict[str, Any]:
    """Run Pipeline in-process and sync ops job + title queue (child entrypoint)."""
    store = OpsStore()
    ledger = OpsLedger(store)
    queue = TitleQueue()
    watchdog = WatchdogAgent(store, ledger)
    cost = CostGuardian(store, ledger)

    job = store.get_job(job_id)
    if not job:
        return {"ok": False, "error": f"job not found: {job_id}"}

    job_dir = Path(out_dir) if out_dir else (
        Path(job.job_dir) if job.job_dir else None
    )
    meta = dict(job.meta or {})
    # Honor sheet format even when CLI invokes execute without spawn env
    profile = normalize_format(
        meta.get("retention_profile") or meta.get("format"),
        default=None,
    )
    os.environ["RETENTION_PROFILE"] = profile
    channel = (meta.get("channel") or meta.get("sheet_tab") or "").strip()
    if channel:
        os.environ["SCRIPT_CHANNEL"] = channel
    meta["format"] = profile
    meta["retention_profile"] = profile
    if channel:
        meta["channel"] = channel
    meta["farm_pid"] = os.getpid()

    if stop_after_voice and from_visuals:
        return {
            "ok": False,
            "error": "stop_after_voice and from_visuals are mutually exclusive",
            "job_id": job_id,
        }

    farm_phase: FarmPhase = "full"
    if stop_after_voice or normalize_farm_phase(meta.get("farm_phase")) == "prep":
        farm_phase = "prep"
    elif from_visuals or normalize_farm_phase(meta.get("farm_phase")) == "visuals":
        farm_phase = "visuals"
    elif (job.status or "").lower() in READY_FOR_STILLS_STATUSES:
        farm_phase = "visuals"
        from_visuals = True

    if farm_phase == "prep":
        stop_after_voice = True
        publish = False
        publish_dry_run = False
    if farm_phase == "visuals":
        from_visuals = True

    do_resume = bool(resume) if resume is not None else bool(
        meta.get("resume")
        or (job_dir and (Path(job_dir) / "script" / "script.json").exists())
    )
    if farm_phase == "visuals":
        do_resume = True
    meta["resume"] = do_resume
    meta["farm_phase"] = farm_phase
    # Selected quality pack + every-video SOP checklist (SMM verifies later).
    from src.agents.smm_sop import stamp_new_format_sop_checklist

    meta = stamp_new_format_sop_checklist(
        meta,
        channel=channel or meta.get("channel"),
        stage="farm_start",
    )
    start_stage = "visuals" if farm_phase == "visuals" else "scripting"
    store.update_job(
        job_id,
        status="farming",
        stage=start_stage,
        meta=meta,
        job_dir=str(job_dir) if job_dir else job.job_dir,
    )
    watchdog.on_stage(job_id, start_stage, status="farming")

    def _pipeline_stage(stage: str) -> None:
        """Keep ops stage fresh; release gpu_lock meta once compose starts.

        SOP stage gates: before advancing, audit the *completed* prerequisite
        stage (soft by default; hard only when ``sop_stage_gates_hard=true``).
        Compose→publish is the primary wired gate.
        """
        from src.agents.smm_sop import (
            SopStageGateError,
            maybe_gate_pipeline_stage,
            stamp_new_format_sop_checklist,
            stamp_stage_gate_meta,
        )

        cur = store.get_job(job_id)
        cur_meta = dict((cur.meta if cur else {}) or {})
        # Soft/hard stage gate against completed prerequisite before advancing.
        try:
            gate = maybe_gate_pipeline_stage(
                cur or {"id": job_id, "job_dir": str(job_dir) if job_dir else None, "meta": cur_meta},
                entering_stage=stage,
            )
            if not gate.get("skipped"):
                cur_meta = stamp_stage_gate_meta(cur_meta, gate)
                if gate.get("blocked"):
                    raise SopStageGateError(
                        (gate.get("remediation") or {}).get("message")
                        or f"SOP stage gate blocked entering {stage}",
                        digest=gate,
                    )
        except SopStageGateError:
            raise
        except Exception as gate_exc:  # noqa: BLE001
            logger.info("sop stage gate soft-fail job=%s stage=%s: %s", job_id, stage, gate_exc)

        if stage == "edit":
            cur_meta["gpu_lock_released"] = True
            cur_meta.setdefault(
                "gpu_lock_released_at",
                datetime.now(timezone.utc).isoformat(),
            )
            # Compose start — refresh SOP checklist for SMM verification.
            cur_meta = stamp_new_format_sop_checklist(
                cur_meta,
                channel=channel or cur_meta.get("channel"),
                stage="compose",
            )
        elif stage == "publish":
            cur_meta = stamp_new_format_sop_checklist(
                cur_meta,
                channel=channel or cur_meta.get("channel"),
                stage="publish",
            )
        store.update_job(
            job_id,
            status="farming",
            stage=stage,
            meta=cur_meta,
            job_dir=str(job_dir) if job_dir else (cur.job_dir if cur else None),
        )
        watchdog.on_stage(job_id, stage, status="farming")

    try:
        from src.services.pipeline import Pipeline

        # Prefer configured Kokoro voice unless CLI overrides
        s = get_settings()
        use_voice = voice or getattr(s, "kokoro_voice", None) or None

        result = Pipeline().run(
            job.title,
            job_dir=job_dir,
            mock_images=mock_images,
            test_mode=test_mode,
            voice=use_voice,
            speed=speed,
            publish=publish and not publish_dry_run and farm_phase != "prep",
            publish_dry_run=publish_dry_run and farm_phase != "prep",
            resume=do_resume,
            stop_after_voice=stop_after_voice,
            from_visuals=from_visuals,
            channel=(meta.get("channel") or meta.get("sheet_tab") or "").strip() or None,
            on_stage=_pipeline_stage,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("farm job failed job_id=%s", job_id)
        err = str(exc)
        err_l = err.lower()
        is_invalid_tags = (
            "invalidtags" in err_l or "invalid video keywords" in err_l
        )
        is_publish = (not is_invalid_tags) and (
            "PublishModule" in err
            or "Gate A" in err
            or "Gate B" in err
            or "Gate R" in err
        )
        # Prefer HOLD over terminal failed so watchdog can repair/resume.
        # Capacity / stock-out: park for next GREEN (no create-retry grind).
        # Publish/Gate: HOLD + block repair (final often already exists).
        # invalidTags: HOLD resumable — sanitize tags playbook (not human block).
        cur = store.get_job(job_id)
        if job_dir is None and cur and cur.job_dir:
            job_dir = Path(cur.job_dir)
        good_stage = infer_last_good_stage(
            job_dir,
            fallback=(cur.stage if cur and cur.stage not in {"failed", "hold"} else "scripting"),
        )
        if is_capacity_hold_error(err):
            parked = park_for_capacity(
                job_id,
                error=err,
                store=store,
                queue=queue,
                watchdog=watchdog,
                ledger=ledger,
                reason=f"farm_exception_capacity stage={good_stage}",
            )
            return {
                "ok": False,
                "error": err,
                "job_id": job_id,
                "status": parked.get("status") or "ready_for_stills",
                "hold": parked,
                "awaiting_green": True,
                "hold_class": CAPACITY_HOLD_CLASS,
            }
        if is_invalid_tags:
            good_stage = "publish"
        held = hold_farm_job(
            job_id,
            error=err,
            store=store,
            queue=queue,
            watchdog=watchdog,
            ledger=ledger,
            hold_class=(
                "invalid_tags"
                if is_invalid_tags
                else ("publish" if is_publish else None)
            ),
            last_good_stage=good_stage,
            repair_blocked=True if is_publish else False,
            reason=(
                "invalid_tags"
                if is_invalid_tags
                else (
                    "publish_or_gate_failure"
                    if is_publish
                    else f"farm_exception stage={good_stage}"
                )
            ),
        )
        if is_publish:
            has_final = bool(
                job_dir and (Path(job_dir) / "video" / "final.mp4").exists()
            )
            try:
                ledger.alert_now(
                    subject=f"[FARM HOLD] publish/gate — {job.title[:50]}",
                    body=(
                        f"Job `{job_id}` held after publish/gate failure.\n"
                        f"Title: {job.title}\n"
                        f"final.mp4 present: {has_final}\n"
                        f"Error:\n{err[:1200]}\n\n"
                        "No auto-rebuild. Re-publish existing cut or fix Gate A, then clear hold.\n"
                    ),
                )
            except Exception as alert_exc:  # noqa: BLE001
                logger.warning("publish-hold alert failed: %s", alert_exc)
        return {
            "ok": False,
            "error": err,
            "job_id": job_id,
            "status": "hold",
            "hold": held,
        }

    # Phase A park — do not notify success / do not burn GREEN chain
    if farm_phase == "prep" or result.stopped_after == "voice":
        cur = store.get_job(job_id)
        cur_meta = dict((cur.meta if cur else {}) or {})
        cur_meta.update(
            {
                "farm_phase": "prep",
                "resume": True,
                "note": "Phase A done — ready_for_stills (awaiting GREEN)",
                "prep_completed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        # Content kernel ready for future RED-day derivative modules (no RunPod).
        cur_meta = mark_ready_for_derivatives(cur_meta)
        cur_meta.pop("farm_pid", None)
        store.update_job(
            job_id,
            status="ready_for_stills",
            stage="awaiting_gpu",
            job_dir=str(result.job_dir),
            error=None,
            meta=cur_meta,
        )
        watchdog.on_stage(
            job_id,
            "awaiting_gpu",
            status="ready_for_stills",
        )
        row = queue.find_by_job_id(job_id)
        if row:
            queue.update_row(
                row.row_index,
                status="running",
                notes="ready_for_stills — awaiting GREEN for RunPod",
                channel=getattr(row, "channel", None) or None,
            )
        ledger.write(
            agent="farm",
            problem=f"Phase A complete: {job.title[:80]}",
            action="park ready_for_stills",
            job_id=job_id,
            severity="info",
            extra={"job_dir": str(result.job_dir), "scene_count": result.scene_count},
        )
        return {
            "ok": True,
            "job_id": job_id,
            "job_dir": str(result.job_dir),
            "status": "ready_for_stills",
            "stage": "awaiting_gpu",
            "farm_phase": "prep",
            "stopped_after": "voice",
            "note": "script+voice parked; watchdog will start visuals on GREEN",
        }

    video_id = result.video_id
    watch_url = result.watch_url
    privacy = result.privacy_status or ("private" if video_id else None)

    if video_id and not publish_dry_run:
        cur = store.get_job(job_id)
        cur_meta = dict((cur.meta if cur else {}) or {})
        cur_meta["farm_phase"] = farm_phase
        cur_meta.pop("needs_yt_media_replace", None)
        was_priority = bool(cur_meta.get("farm_priority"))
        if was_priority:
            cur_meta["note"] = "farm_priority published — awaiting schedule arm"

        store.update_job(
            job_id,
            status="private",
            stage="private",
            job_dir=str(result.job_dir),
            video_id=video_id,
            watch_url=watch_url,
            error=None,
            meta=cur_meta,
        )

        schedule_result = None
        if was_priority:
            try:
                from src.agents.farm_priority import mark_priority_done

                mark_priority_done(job_id, note="farm_published")
            except Exception as pri_exc:  # noqa: BLE001
                logger.warning("mark_priority_done failed: %s", pri_exc)
            # Lock reserved slots immediately when public_approved already set.
            if cur_meta.get("public_approved") and (
                cur_meta.get("schedule_slot_locked")
                or cur_meta.get("force_publish_at_utc")
                or cur_meta.get("reserved_publish_at_utc")
            ):
                try:
                    from src.agents.schedule_agent import ScheduleAgent

                    schedule_result = ScheduleAgent(
                        store, ledger, queue
                    ).assign_slot_for_job(
                        job_id=job_id,
                        video_id=video_id,
                        public_approved=True,
                        apply_youtube=True,
                    )
                except Exception as sched_exc:  # noqa: BLE001
                    logger.warning(
                        "priority schedule arm failed for %s: %s", job_id, sched_exc
                    )
                    schedule_result = {"ok": False, "error": str(sched_exc)[:300]}

            # Clear jump-queue flag after arm attempt (schedule write may have refreshed meta).
            refreshed = store.get_job(job_id)
            refreshed_meta = dict((refreshed.meta if refreshed else {}) or {})
            refreshed_meta["farm_priority"] = False
            if schedule_result and schedule_result.get("ok"):
                refreshed_meta["note"] = (
                    "farm_priority published+scheduled "
                    f"{schedule_result.get('scheduled_at_local')}"
                )
            else:
                refreshed_meta["note"] = cur_meta.get("note") or (
                    "farm_priority published — awaiting schedule arm"
                )
            store.update_job(job_id, meta=refreshed_meta)
            cur_meta = refreshed_meta

        scheduled_ok = bool(schedule_result and schedule_result.get("ok"))
        if scheduled_ok:
            # assign_slot_for_job already set status=scheduled; keep consistent.
            pass
        watchdog.report_publish(
            job_id,
            video_id=video_id,
            watch_url=watch_url,
            privacy=("private" if scheduled_ok else (privacy or "private")),
        )
        from src.services.retention_profile import active_profile_name

        cost.record_estimated_video(
            job_id=job_id,
            profile=(meta.get("retention_profile") or active_profile_name()),
        )
        row = queue.find_by_job_id(job_id)
        if row:
            queue.update_row(
                row.row_index,
                status="scheduled" if scheduled_ok else "private",
                video_id=video_id,
                notes=(
                    cur_meta.get("note")
                    or (
                        "scheduled ok"
                        if scheduled_ok
                        else "private upload ok — awaiting public_approved"
                    )
                ),
                channel=getattr(row, "channel", None) or None,
            )
        # SMM watches only after the video is actually public (sleep beat detects it).
        try:
            from src.runpod.watchdog import notify_farm_success

            notify_farm_success(job_id)
        except Exception as notify_exc:  # noqa: BLE001
            logger.warning("watchdog success notify failed: %s", notify_exc)
        out = {
            "ok": True,
            "job_id": job_id,
            "job_dir": str(result.job_dir),
            "video_id": video_id,
            "watch_url": watch_url,
            "privacy": privacy,
            "farm_phase": farm_phase,
            "status": "scheduled" if scheduled_ok else "private",
        }
        if schedule_result is not None:
            out["schedule"] = schedule_result
        return out

    # Dry-run / no-publish: keep artifacts; do not pretend we uploaded
    note = "pipeline finished (dry-run or no-publish)"
    cur = store.get_job(job_id)
    cur_meta = dict((cur.meta if cur else {}) or {})
    cur_meta["note"] = note
    cur_meta["farm_phase"] = farm_phase
    keep_vid = (cur.video_id if cur else None) or video_id
    keep_url = (cur.watch_url if cur else None) or watch_url
    preserve = bool(
        cur_meta.get("preserve_scheduled_video_id")
        or cur_meta.get("skip_publish_keep_schedule")
    )
    if preserve and keep_vid:
        # Surgical recompose for an already-scheduled upload — restore schedule
        # identity and flag media replace (YouTube Data API has no file replace).
        note = (
            "new-format recompose done — same video_id preserved; "
            "needs_yt_media_replace"
        )
        cur_meta["note"] = note
        cur_meta["needs_yt_media_replace"] = True
        cur_meta["recompose_completed_at"] = datetime.now(timezone.utc).isoformat()
        cur_meta["farm_priority"] = False  # clear jump-queue flag
        done_status = "scheduled"
        stage_out = "scheduled"
        try:
            from src.agents.farm_priority import mark_priority_done

            mark_priority_done(job_id, note="recompose_ok_awaiting_yt_replace")
        except Exception as pri_exc:  # noqa: BLE001
            logger.warning("mark_priority_done failed: %s", pri_exc)
    else:
        done_status = "done" if (publish_dry_run or not publish) else "private"
        stage_out = "private" if video_id else "edit"
        keep_vid = video_id
        keep_url = watch_url
        if cur_meta.get("farm_priority"):
            try:
                from src.agents.farm_priority import mark_priority_done

                mark_priority_done(job_id, note="farm_complete")
            except Exception as pri_exc:  # noqa: BLE001
                logger.warning("mark_priority_done failed: %s", pri_exc)
    store.update_job(
        job_id,
        status=done_status,
        stage=stage_out,
        job_dir=str(result.job_dir),
        video_id=keep_vid,
        watch_url=keep_url,
        error=None,
        meta=cur_meta,
    )
    row = queue.find_by_job_id(job_id)
    if row:
        queue.update_row(
            row.row_index,
            status=(
                "scheduled"
                if done_status == "scheduled"
                else ("private" if keep_vid else "running")
            ),
            video_id=keep_vid or "",
            notes=note,
            channel=getattr(row, "channel", None) or None,
        )
    try:
        from src.runpod.watchdog import notify_farm_success

        notify_farm_success(job_id)
    except Exception as notify_exc:  # noqa: BLE001
        logger.warning("watchdog success notify failed: %s", notify_exc)
    return {
        "ok": True,
        "job_id": job_id,
        "job_dir": str(result.job_dir),
        "video_id": keep_vid,
        "watch_url": keep_url,
        "note": note,
        "farm_phase": farm_phase,
        "status": done_status,
        "needs_yt_media_replace": bool(preserve and keep_vid),
    }


def reconcile_farms(
    *,
    store: OpsStore | None = None,
    ledger: OpsLedger | None = None,
    queue: TitleQueue | None = None,
    spawn_queued: bool = True,
) -> dict[str, Any]:
    """HOLD dead farm PIDs (resumable); optionally spawn capacity for orphan queued jobs."""
    store = store or OpsStore()
    ledger = ledger or OpsLedger(store)
    queue = queue or TitleQueue()
    watchdog = WatchdogAgent(store, ledger)
    dead: list[dict[str, Any]] = []
    spawned: list[dict[str, Any]] = []

    for job in store.list_jobs():
        if job.status in IDLE_STATUSES and job.status != "queued":
            continue
        meta = job.meta or {}
        pid = meta.get("farm_pid")
        if job.status in ("private", "scheduled", "public", "done"):
            continue
        if job.video_id:
            continue
        # Live PID ⇒ still encoding.
        if pid_alive(pid):
            continue
        status = (job.status or "").lower()
        stage = (job.stage or "").lower()
        # Pure queued waiters (no spawn yet) are not dead farms.
        if status == "queued" and not pid:
            continue
        encoding_labels = ACTIVE_FARM_STATUSES - {"queued"}
        if status not in encoding_labels and stage not in encoding_labels:
            continue
        # Null-PID zombies: only HOLD pre-compose lock-holders (Rome TTS).
        # edit/edit_done with finals stay as-is — locks already freed by
        # ``_is_encoding_job`` requiring a live PID.
        if not pid:
            if _job_past_gpu_pod(job) or stage in {"edit", "edit_done", "gate_a", "gate_b"}:
                jd = Path(job.job_dir) if job.job_dir else None
                final_ok = bool(
                    jd
                    and (jd / "video" / "final.mp4").is_file()
                    and (jd / "video" / "final.mp4").stat().st_size > 1000
                )
                if not (final_ok and status == "farming"):
                    continue
                err = (
                    f"dead farm post-compose: status={status} stage={stage} "
                    "farm_pid=null (no live process)"
                )
                reason = "dead_farm_post_compose"
            else:
                err = (
                    f"stale farm lock: status={status} stage={stage} "
                    "farm_pid=null (no live process)"
                )
                reason = "stale_null_farm_pid"
        else:
            err = f"farm process pid={pid} exited without video_id"
            reason = "dead_farm_pid"
        held = hold_farm_job(
            job.id,
            error=err,
            store=store,
            queue=queue,
            watchdog=watchdog,
            ledger=ledger,
            hold_class="process_crash",
            last_good_stage=infer_last_good_stage(
                job.job_dir, fallback=job.stage or "scripting"
            ),
            reason=reason,
        )
        dead.append(
            {
                "job_id": job.id,
                "farm_pid": pid,
                "error": err,
                "status": "hold",
                "resumable": held.get("resumable"),
                "last_good_stage": held.get("last_good_stage"),
                "reason": reason,
            }
        )

    if spawn_queued:
        max_c = max_concurrent_jobs()
        for job in store.list_jobs(status="queued"):
            if job.video_id:
                continue
            if count_encoding_jobs(store) >= max_c:
                break
            meta = job.meta or {}
            if pid_alive(meta.get("farm_pid")):
                continue
            if meta.get("skip_auto_farm"):
                continue
            # Prefer stamped farm_phase / hold_class so script repairs spawn as prep.
            phase = None
            try:
                from src.agents.repair_watchdog import (
                    classify_failure,
                    read_farm_log_tail,
                    resume_phase_for_job,
                )

                klass = str(
                    meta.get("hold_class") or meta.get("last_repair_class") or ""
                ).strip().lower()
                if not klass:
                    klass = classify_failure(job.error or "", read_farm_log_tail(job))
                phase = resume_phase_for_job(job, klass) or meta.get("farm_phase")
            except Exception:  # noqa: BLE001
                phase = meta.get("farm_phase")
            spawned.append(
                spawn_farm_job(
                    job.id, store=store, watchdog=watchdog, phase=phase
                )
            )

    if dead or spawned:
        ledger.write(
            agent="farm",
            problem="reconcile_farms",
            action=f"dead={len(dead)} spawned={len(spawned)}",
            severity="info",
            extra={"dead": dead, "spawned": [s.get("job_id") for s in spawned]},
        )
    return {
        "dead": dead,
        "spawned": spawned,
        "inflight": count_inflight_jobs(store),
        "encoding": count_encoding_jobs(store),
    }
