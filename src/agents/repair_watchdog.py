"""RepairWatchdog — classify failed/HOLD farm jobs, apply playbooks, resume pipeline.

On each sleep beat (after farm reconcile) and capacity-watchdog ticks:
1. Find failed + resumable HOLD jobs (dead-farm leftovers already marked HOLD)
2. Read job.error + farm log tail
3. Classify failure → playbook
4. Repair artifacts when needed (dark visual prompts, drop bad still, compose resume)
5. Spawn resumed farm so Script/TTS/visuals/edit/publish continue

Image QA validation failures are NEVER capped — keep repairing/resuming until
the stills pass. Auth / budget / publish classes HOLD instead of looping.
After max repairs for capped classes → stay HOLD for human (not terminal FAIL).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.farm import (
    count_encoding_jobs,
    infer_last_good_stage,
    max_concurrent_jobs,
    pid_alive,
    spawn_farm_job,
    voice_artifacts_ready,
)
from src.agents.ledger import OpsLedger
from src.agents.store import JobRecord, OpsStore
from src.agents.title_queue import TitleQueue
from src.agents.watchdog import WatchdogAgent
from src.services.settings import CONFIG_DIR, ROOT
from src.services.llm import JSON_FAILOVER_EXHAUSTED
from src.services.visual_guardrails import apply_guardrails

logger = logging.getLogger(__name__)

# Job-level repair: validation is infinite; other classes use max_repairs.
INFINITE_REPAIR_CLASSES = frozenset({"visual_validation"})
DEFAULT_MAX_REPAIRS = 5
# Capacity watchdog auto-retries these even when full repair_watchdog.enabled=false.
SCRIPT_JSON_AUTO_CLASSES = frozenset({"script_json", "script"})
# Broader auto-resume classes for capacity watchdog (HOLD-first resilience).
# NOTE: capacity_out is intentionally EXCLUDED — RunPod capacity watchdog owns
# resume on the next GREEN tick only (no create-retry grind).
AUTO_RESUME_CLASSES = frozenset(
    {
        "script_json",
        "script",
        "runpod_transient",
        "process_crash",
        "edit",
        "tts",
        "visual_validation",
        "invalid_tags",
        "unknown",
    }
)
# GPU stock / GREEN gate failures — park until next GREEN; never repair-spawn.
CAPACITY_HOLD_CLASSES = frozenset({"capacity_out"})
# One clean overnight pass under failover policy, then HOLD (no $ burn loop).
SCRIPT_JSON_MAX_AUTO_REPAIRS = 2

_DARK_PROMPT = re.compile(
    r"\b("
    r"dimly lit|shadowy|pitch-black|shrouded in darkness|"
    r"in the shadows|crushed blacks|underexposed|silhouette only"
    r")\b",
    re.IGNORECASE,
)


def _agents_cfg() -> dict[str, Any]:
    path = CONFIG_DIR / "agents_settings.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def classify_failure(error: str, log_tail: str = "") -> str:
    """Map farm error + log snippet → playbook class."""
    text = f"{error or ''}\n{log_tail or ''}".lower()
    if not text.strip():
        return "unknown"

    # Capacity / stock — before runpod_transient so supply-out never auto-grinds.
    if any(
        k in text
        for k in (
            "out_of_stock",
            "supply_constraint",
            "no longer any instances",
            "no instances currently available",
            "could not find any pods",
            "capacity pre-flight deferred",
            "capacitydefer",
            "farm blocked (green-light only)",
            "farm spawn blocked",
            "wait for next green",
            "no fallbacks",
            "awaiting green",
            "hold_capacity",
            "not green (",
            "classification=red",
            "classification=yellow",
        )
    ):
        return "capacity_out"

    if any(
        k in text
        for k in (
            "validation failed after generate",
            "validation failed",
            "mean_luma",
            "bright_frac",
            "image validation",
        )
    ):
        return "visual_validation"

    if any(
        k in text
        for k in (
            "runpod http 403",
            "api_key is invalid",
            "runpod_api_key missing",
            "401 unauthorized",
            "invalid api key",
        )
    ):
        return "auth"

    if "monthly_budget" in text or "budget cap" in text or "over budget" in text:
        return "budget"

    # YouTube keyword rejection — sanitize tags and retry publish (not hard publish block).
    if "invalidtags" in text or "invalid video keywords" in text:
        return "invalid_tags"

    # Expand beat-count mismatches (not TTS).
    if re.search(r"returned\s+\d+\s+beats.*target\s+\d+", text) or (
        "beats, target" in text and "chapter" in text
    ):
        return "script"

    if any(
        k in text
        for k in (
            "comfy /prompt",
            "comfy /history",
            "waiting for service to respond",
            "runpod http 5",
            "runpod job timed out",
            "connection reset",
            "temporarily unavailable",
            "in_queue",
        )
    ) or (
        any(k in text for k in ("timed out", "timeout"))
        and ("runpod" in text or "comfy" in text or "visual" in text)
    ):
        if (
            "runpod" in text
            or "comfy" in text
            or "visual" in text
            or "http" in text
            or "scene " in text
        ):
            return "runpod_transient"

    if "voicemodule" in text or "kokoro" in text or (
        "tts" in text and "beats, target" not in text
    ):
        return "tts"

    if "editmodule" in text or "ffmpeg" in text or "composer" in text:
        return "edit"
    if any(
        k in text
        for k in (
            "visualmodule",
            "stills",
            "seedream",
            "flux",
            "runpod still",
            "image generate",
            "scene_",
            "scene ",
        )
    ) and any(
        k in text
        for k in ("failed", "error", "timeout", "oom", "killed", "crash", "502", "503")
    ):
        return "runpod_transient"

    if "publishmodule" in text or "gate a" in text:
        return "publish"
    if "gate a failed" in text or "outside gate a band" in text:
        return "publish"
    # Generic youtube errors (not invalidTags — handled above)
    if "youtube" in text and "keyword" not in text:
        return "publish"

    # Prep/script LLM JSON parse failures (often bare JSONDecodeError message).
    if any(
        k in text
        for k in (
            "jsondecodeerror",
            "expecting value",
            "expecting property name",
            "could not parse json",
            "json parse failed",
            "model json root",
            "not valid json",
            "unterminated string",
            "extra data:",
        )
    ):
        return "script_json"
    if "script" in text and ("llm" in text or "outline" in text or "expand" in text):
        return "script"
    if "_generate_outline" in text or "chat_json" in text or "parse_json_object" in text:
        return "script_json"
    if "json parse failover exhausted" in text:
        return "script_json"

    if "spawn failed" in text or "farm pid dead" in text or "process died" in text:
        return "process_crash"
    if "exited without video_id" in text:
        return "process_crash"
    if "stuck in queued" in text or "stuck_stage_timeout" in text or "stuck in " in text:
        return "process_crash"

    return "unknown"


def script_json_failover_exhausted(error: str, log_tail: str = "") -> bool:
    text = f"{error or ''}\n{log_tail or ''}".lower()
    return JSON_FAILOVER_EXHAUSTED.lower() in text


def job_has_meaningful_progress(job: JobRecord) -> bool:
    """True when job_dir already has script/voice/images — do not wipe/remake."""
    if not job.job_dir:
        return False
    job_dir = Path(job.job_dir)
    if not job_dir.is_dir():
        return False
    script = job_dir / "script" / "script.json"
    try:
        if script.is_file() and script.stat().st_size > 2:
            return True
        audio = job_dir / "audio"
        if audio.is_dir() and any(audio.glob("*.wav")):
            return True
        images = job_dir / "images"
        if images.is_dir() and (
            any(images.glob("*.jpg")) or any(images.glob("*.png"))
        ):
            return True
    except OSError:
        return False
    return False


def read_farm_log_tail(job: JobRecord, *, max_chars: int = 8000) -> str:
    meta = job.meta or {}
    candidates: list[Path] = []
    if meta.get("farm_log"):
        candidates.append(Path(str(meta["farm_log"])))
    candidates.append(ROOT / "output" / "ops" / f"farm_{job.id}.log")
    candidates.append(ROOT / "output" / "ops" / f"farm_job_{job.id}.log")
    # legacy / resume logs
    candidates.append(ROOT / "output" / "ops" / f"farm_job_{job.id}_resume.log")
    for path in candidates:
        try:
            if path.exists() and path.is_file():
                data = path.read_text(encoding="utf-8", errors="replace")
                return data[-max_chars:]
        except OSError:
            continue
    return ""


def rewrite_dark_visual_prompts(job_dir: Path) -> dict[str, Any]:
    """Rewrite dim/shadowy scene prompts in script.json via guardrail rules."""
    script_path = Path(job_dir) / "script" / "script.json"
    if not script_path.exists():
        return {"ok": False, "reason": "no_script", "rewritten": 0}

    raw = json.loads(script_path.read_text(encoding="utf-8"))
    scenes = raw.get("scenes") or []
    changed = 0
    details: list[dict[str, Any]] = []
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        vp = (scene.get("visual_prompt") or "").strip()
        if not vp or not _DARK_PROMPT.search(vp):
            continue
        guarded = apply_guardrails(vp)
        new_vp = vp
        if guarded.rewritten:
            for old, new in guarded.rewritten:
                new_vp = re.sub(re.escape(old), new, new_vp, flags=re.IGNORECASE)
            new_vp = re.sub(r"\s{2,}", " ", new_vp).strip(" ,")
        if new_vp and new_vp != vp:
            details.append(
                {"index": scene.get("index"), "from": vp[:120], "to": new_vp[:120]}
            )
            scene["visual_prompt"] = new_vp
            changed += 1

    if changed:
        script_path.write_text(
            json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return {"ok": True, "rewritten": changed, "details": details[:20]}


def resume_phase_for_job(job: JobRecord, klass: str = "") -> str | None:
    """Pick farm phase for repair spawn from artifacts / failure class."""
    if klass in CAPACITY_HOLD_CLASSES:
        # Watchdog GREEN Phase B only — never prep/full grind on stock-out.
        return "visuals" if voice_artifacts_ready(job.job_dir) else None
    if klass in SCRIPT_JSON_AUTO_CLASSES and not job_has_meaningful_progress(job):
        return "prep"
    good = infer_last_good_stage(job.job_dir, fallback="")
    if good in {"visuals", "edit", "publish"} and voice_artifacts_ready(job.job_dir):
        return "visuals"
    if good == "tts" or (job.job_dir and (Path(job.job_dir) / "script" / "script.json").exists()):
        # Script present — full resume (skip regenerate when resume=True).
        return None
    if klass in SCRIPT_JSON_AUTO_CLASSES:
        return "prep"
    return None


def job_is_repair_candidate(
    job: JobRecord, *, require_explicit: bool = False
) -> bool:
    """True for failed or resumable HOLD (not repair_blocked / exhausted)."""
    st = (job.status or "").lower()
    if st not in {"failed", "hold"}:
        return False
    meta = dict(job.meta or {})
    if meta.get("repair_blocked") or meta.get("repair_exhausted"):
        return False
    if st == "hold" and meta.get("resumable") is False:
        return False
    if require_explicit and meta.get("resumable") is not True:
        # Capacity auto-resume lane: only farm-stamped resumable=True.
        return False
    if pid_alive(meta.get("farm_pid")):
        return False
    return True


_HEAL_SKIP_MARKERS = (
    "parked:",
    "manually stopped",
    "aborted expensive rebuild",
    "do not resume",
    "human-kill",
    "awaiting public_approved",
)


def heal_legacy_failed_jobs(
    store: OpsStore | None = None,
    *,
    queue: TitleQueue | None = None,
    ledger: OpsLedger | None = None,
    sync_sheet: bool = True,
) -> list[dict[str, Any]]:
    """Migrate recoverable terminal ``failed`` (and unstamped stuck HOLD) → HOLD resumable.

    Legacy rows from before HOLD-first stay ``failed`` without ``resumable=True``,
    so capacity resume (``require_explicit``) skips them forever. Safe classes
    (Comfy 502, invalidTags, process_crash, …) become HOLD stamped for the
    next */10 tick. Auth / budget / publish stay blocked HOLD (not auto-resume).
    """
    from src.agents.farm import BLOCKED_HOLD_CLASSES, hold_farm_job

    store = store or OpsStore()
    queue = queue or TitleQueue()
    ledger = ledger or OpsLedger(store)
    actions: list[dict[str, Any]] = []

    for job in store.list_jobs():
        st = (job.status or "").lower()
        meta = dict(job.meta or {})
        if meta.get("repair_blocked") or meta.get("repair_exhausted"):
            continue
        if meta.get("skip_auto_farm") or meta.get("park_reason"):
            continue
        if pid_alive(meta.get("farm_pid")):
            continue

        err = job.error or ""
        err_l = err.lower()
        if any(m in err_l for m in _HEAL_SKIP_MARKERS):
            continue

        # Already stamped — nothing to heal.
        if st == "hold" and meta.get("resumable") is True:
            continue
        # Explicit non-resumable HOLD (human) — leave alone.
        if st == "hold" and meta.get("resumable") is False:
            continue

        # Targets: terminal failed OR unstamped HOLD (legacy stuck scan).
        if st == "failed":
            pass
        elif st == "hold" and meta.get("resumable") is None:
            pass
        else:
            continue

        log_tail = read_farm_log_tail(job)
        klass = classify_failure(err, log_tail)
        if not klass or klass == "unknown":
            # Bare failed with thin signal — process_crash if we have error text.
            if st == "failed" and err:
                klass = "process_crash"
            else:
                continue
        if klass in BLOCKED_HOLD_CLASSES:
            held = hold_farm_job(
                job.id,
                error=err or f"legacy failed class={klass}",
                store=store,
                queue=queue,
                ledger=ledger,
                hold_class=klass,
                repair_blocked=True,
                reason=f"heal_legacy_blocked:{klass}",
                sync_sheet=sync_sheet,
            )
            actions.append(
                {
                    "job_id": job.id,
                    "healed": True,
                    "from": st,
                    "class": klass,
                    "resumable": False,
                    "blocked": True,
                    "hold": held,
                }
            )
            continue
        if klass not in AUTO_RESUME_CLASSES:
            continue

        held = hold_farm_job(
            job.id,
            error=err or f"legacy {st} → HOLD ({klass})",
            store=store,
            queue=queue,
            ledger=ledger,
            hold_class=klass,
            repair_blocked=False,
            reason=f"heal_legacy_failed:{klass}",
            sync_sheet=sync_sheet,
        )
        actions.append(
            {
                "job_id": job.id,
                "healed": True,
                "from": st,
                "class": klass,
                "resumable": True,
                "hold": held,
            }
        )
        logger.info(
            "healed legacy %s → HOLD resumable job=%s class=%s",
            st,
            job.id,
            klass,
        )

    if actions and ledger is not None:
        try:
            ledger.write(
                agent="repair_watchdog",
                problem="legacy failed/HOLD without resumable stamp",
                action=f"healed n={len(actions)} → HOLD resumable (or blocked)",
                severity="info",
                extra={
                    "healed": [
                        {
                            "job_id": a["job_id"],
                            "class": a.get("class"),
                            "from": a.get("from"),
                        }
                        for a in actions
                    ]
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("heal ledger write failed: %s", exc)
    return actions


class RepairWatchdog:
    """Analyze failed/HOLD jobs and resume the production farm with playbooks."""

    def __init__(
        self,
        store: OpsStore | None = None,
        ledger: OpsLedger | None = None,
        queue: TitleQueue | None = None,
        watchdog: WatchdogAgent | None = None,
    ):
        self.store = store or OpsStore()
        self.ledger = ledger or OpsLedger(self.store)
        self.queue = queue or TitleQueue()
        self.watchdog = watchdog or WatchdogAgent(self.store, self.ledger)
        self.cfg = dict((_agents_cfg().get("repair_watchdog") or {}))

    def scan_and_repair(self, *, spawn: bool = True) -> list[dict[str, Any]]:
        if self.cfg.get("enabled", True) is False:
            return [{"skipped": True, "reason": "repair_watchdog disabled"}]

        actions: list[dict[str, Any]] = []
        max_c = max_concurrent_jobs()
        encoding = count_encoding_jobs(self.store)

        candidates = [
            j for j in self.store.list_jobs() if job_is_repair_candidate(j)
        ]
        candidates.sort(key=lambda j: j.updated_at or j.created_at or "")

        for job in candidates:
            # Prefer repairing before picking new titles; respect farm slot.
            if spawn and encoding >= max_c:
                actions.append(
                    {
                        "job_id": job.id,
                        "skipped": True,
                        "reason": f"farm slot full encoding={encoding} max={max_c}",
                    }
                )
                continue

            result = self.repair_job(job.id, spawn=spawn)
            actions.append(result)
            if result.get("spawned"):
                encoding += 1

        return actions

    def scan_resumable_failures(self, *, spawn: bool = True) -> list[dict[str, Any]]:
        """Capacity-watchdog path: resume stamped HOLD/failed for safe auto classes.

        Runs even when full ``repair_watchdog.enabled`` is false (like script_json
        auto-retry). Only ``resumable=True`` jobs — legacy parked rows stay out so
        they cannot monopolize the farm slot. After repair cap → HOLD exhausted and
        continue to the next candidate (never block forever on one title).
        """
        if self.cfg.get("auto_resume_hold", True) is False:
            return [{"skipped": True, "reason": "auto_resume_hold disabled"}]

        actions: list[dict[str, Any]] = []
        max_c = max_concurrent_jobs()
        encoding = count_encoding_jobs(self.store)
        candidates = [
            j
            for j in self.store.list_jobs()
            if job_is_repair_candidate(j, require_explicit=True)
        ]
        candidates.sort(key=lambda j: j.updated_at or j.created_at or "")

        for job in candidates:
            meta = dict(job.meta or {})
            if meta.get("farm_pid") and not pid_alive(meta.get("farm_pid")):
                meta["farm_pid"] = None
                self.store.update_job(job.id, meta=meta)

            log_tail = read_farm_log_tail(job)
            klass = classify_failure(job.error or "", log_tail)
            last_klass = str(meta.get("last_repair_class") or meta.get("hold_class") or "")
            effective = klass if klass != "unknown" else (last_klass or klass)
            if effective in CAPACITY_HOLD_CLASSES or klass in CAPACITY_HOLD_CLASSES:
                actions.append(
                    {
                        "job_id": job.id,
                        "skipped": True,
                        "reason": (
                            "capacity_out — watchdog GREEN only "
                            "(no repair create-retry)"
                        ),
                        "class": "capacity_out",
                        "awaiting_green": True,
                    }
                )
                continue
            if effective not in AUTO_RESUME_CLASSES and klass not in AUTO_RESUME_CLASSES:
                actions.append(
                    {
                        "job_id": job.id,
                        "skipped": True,
                        "reason": f"class={effective} not in auto-resume set",
                        "class": effective,
                    }
                )
                continue

            if spawn and encoding >= max_c:
                actions.append(
                    {
                        "job_id": job.id,
                        "skipped": True,
                        "reason": f"farm slot full encoding={encoding} max={max_c}",
                        "class": effective,
                    }
                )
                continue

            result = self.repair_job(job.id, spawn=spawn)
            result["auto_resume"] = True
            actions.append(result)
            if result.get("spawned"):
                encoding += 1
            # Cap / auth / publish HOLD → leave parked; keep scanning next job.
            if result.get("blocked") or result.get("exhausted"):
                continue

        return actions

    def scan_script_json_failures(self, *, spawn: bool = True) -> list[dict[str, Any]]:
        """Auto-retry prep/script JSON failures (capacity watchdog path).

        Runs even when full ``repair_watchdog.enabled`` is false. Only touches
        failed/HOLD jobs classified as ``script_json``/``script`` with **no**
        script/voice/image progress — keeps the same ``job_id`` (never remakes
        a sheet row that already has assets).

        Also re-spawns queued jobs whose prior repair was deferred (prep/gpu lock)
        **or** stranded queued with ``last_repair_class=script_json`` and a dead
        ``farm_pid`` (requeue without spawn / crash mid-repair).
        """
        if self.cfg.get("auto_retry_script_json", True) is False:
            return [{"skipped": True, "reason": "auto_retry_script_json disabled"}]

        actions: list[dict[str, Any]] = []
        for job in self.store.list_jobs():
            status = (job.status or "").lower()
            meta = dict(job.meta or {})
            if meta.get("repair_blocked") or meta.get("repair_exhausted"):
                continue
            if pid_alive(meta.get("farm_pid")):
                continue

            # Dead farm_pid blocks nothing but must not look "running".
            if meta.get("farm_pid") and not pid_alive(meta.get("farm_pid")):
                meta["farm_pid"] = None
                self.store.update_job(job.id, meta=meta)

            log_tail = read_farm_log_tail(job)
            klass = classify_failure(job.error or "", log_tail)
            last_klass = str(meta.get("last_repair_class") or meta.get("hold_class") or "")
            stranded_queued = (
                status == "queued"
                and last_klass in SCRIPT_JSON_AUTO_CLASSES
                and not meta.get("farm_pid")
            )
            deferred_queued = status == "queued" and bool(
                meta.get("script_json_spawn_deferred")
            )
            hold_or_failed = status in {"failed", "hold"}

            if hold_or_failed:
                if klass not in SCRIPT_JSON_AUTO_CLASSES and last_klass not in SCRIPT_JSON_AUTO_CLASSES:
                    continue
                if status == "hold" and meta.get("resumable") is False:
                    continue
                klass = klass if klass in SCRIPT_JSON_AUTO_CLASSES else last_klass
            elif deferred_queued or stranded_queued:
                if (
                    last_klass not in SCRIPT_JSON_AUTO_CLASSES
                    and klass not in SCRIPT_JSON_AUTO_CLASSES
                ):
                    continue
                klass = last_klass or klass or "script_json"
            else:
                continue

            if job_has_meaningful_progress(job):
                actions.append(
                    {
                        "job_id": job.id,
                        "skipped": True,
                        "reason": "has progress (script/voice/images) — no auto remake",
                        "class": klass,
                    }
                )
                continue

            # Both models already exhausted in-process → HOLD, move to next title.
            if hold_or_failed and script_json_failover_exhausted(
                job.error or "", log_tail
            ):
                result = self._block(
                    job,
                    klass,
                    reason=(
                        "script JSON failover exhausted "
                        "(primary 1+2 retries + fallback 1+2 retries) — HOLD; next title"
                    ),
                    out={
                        "ok": True,
                        "job_id": job.id,
                        "class": klass,
                        "auto_script_json": True,
                        "held": True,
                    },
                    alert=True,
                    exhausted=True,
                )
                actions.append(result)
                continue

            if deferred_queued or stranded_queued:
                # Already requeued — only try spawn again (prep phase = respects
                # prep_lock while Anne/ffmpeg holds local CPU).
                if not spawn:
                    actions.append(
                        {
                            "job_id": job.id,
                            "ok": True,
                            "class": klass,
                            "requeued": True,
                            "spawned": False,
                            "deferred": True,
                        }
                    )
                    continue
                spawned = spawn_farm_job(
                    job.id,
                    store=self.store,
                    watchdog=self.watchdog,
                    phase="prep",
                )
                ok_spawn = bool(
                    spawned.get("spawned") or spawned.get("already_running")
                )
                cur = self.store.get_job(job.id)
                cur_meta = dict((cur.meta if cur else {}) or {})
                if ok_spawn:
                    cur_meta.pop("script_json_spawn_deferred", None)
                    cur_meta.pop("script_json_spawn_error", None)
                    self.store.update_job(job.id, meta=cur_meta)
                else:
                    err = str(spawned.get("error") or "spawn failed")
                    cur_meta["script_json_spawn_deferred"] = True
                    cur_meta["script_json_spawn_error"] = err[:300]
                    cur_meta["last_repair_class"] = klass
                    self.store.update_job(job.id, meta=cur_meta)
                actions.append(
                    {
                        "ok": True,
                        "job_id": job.id,
                        "class": klass,
                        "requeued": True,
                        "spawned": ok_spawn,
                        "spawn": spawned,
                        "auto_script_json": True,
                        "farm_phase": "prep",
                    }
                )
                continue

            result = self.repair_job(job.id, spawn=spawn)
            result["auto_script_json"] = True
            actions.append(result)
        return actions

    def repair_job(self, job_id: str, *, spawn: bool = True) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            return {"ok": False, "job_id": job_id, "error": "job not found"}

        log_tail = read_farm_log_tail(job)
        klass = classify_failure(job.error or "", log_tail)
        meta = dict(job.meta or {})
        repair_counts = dict(meta.get("repair_counts") or {})
        count = int(repair_counts.get(klass, 0))
        infinite = klass in INFINITE_REPAIR_CLASSES or (
            klass == "visual_validation"
            and bool(self.cfg.get("infinite_visual_validation", True))
        )

        out: dict[str, Any] = {
            "ok": True,
            "job_id": job_id,
            "class": klass,
            "repair_count": count,
            "error_preview": (job.error or "")[:240],
        }

        if klass == "auth":
            return self._block(
                job,
                klass,
                reason="auth/credential failure — fix RUNPOD_API_KEY / endpoint",
                out=out,
            )
        if klass == "budget":
            return self._block(
                job,
                klass,
                reason="budget cap — waiting for human / next month",
                out=out,
            )
        # Publish / Gate A: NEVER auto-resume or start a new stills farm.
        # final.mp4 often already exists — human should publish/allow-short/fix duration.
        if klass == "publish":
            job_dir = Path(job.job_dir) if job.job_dir else None
            has_final = bool(
                job_dir
                and (job_dir / "video" / "final.mp4").exists()
                and (job_dir / "video" / "final.mp4").stat().st_size > 1000
            )
            reason = (
                "publish/Gate A failure — HOLD (no auto-rebuild). "
                + (
                    "final.mp4 exists: fix Gate A / re-publish only."
                    if has_final
                    else "Inspect publish error before any resume."
                )
            )
            return self._block(job, klass, reason=reason, out=out, alert=True)

        # Sleep policy: in-process failover already tried both models → HOLD.
        if klass in SCRIPT_JSON_AUTO_CLASSES and script_json_failover_exhausted(
            job.error or "", log_tail
        ):
            return self._block(
                job,
                klass,
                reason=(
                    "script JSON failover exhausted "
                    "(primary 1+2 retries + fallback 1+2 retries) — HOLD; next title"
                ),
                out=out,
                alert=True,
                exhausted=True,
            )

        # Cap script_json auto-requeues (allow one clean overnight pass, then HOLD).
        if klass in SCRIPT_JSON_AUTO_CLASSES:
            max_repairs = int(
                self.cfg.get("max_script_json_repairs") or SCRIPT_JSON_MAX_AUTO_REPAIRS
            )
        else:
            max_repairs = int(self.cfg.get("max_repairs_per_class") or DEFAULT_MAX_REPAIRS)

        if not infinite and count >= max_repairs:
            return self._block(
                job,
                klass,
                reason=(
                    f"max repairs ({max_repairs}) reached for class={klass} "
                    "— HOLD exhausted; move to next job"
                ),
                out=out,
                exhausted=True,
            )

        playbook_notes: list[str] = []
        job_dir = Path(job.job_dir) if job.job_dir else None
        good_stage = infer_last_good_stage(
            job_dir, fallback=str(meta.get("last_good_stage") or job.stage or "scripting")
        )
        playbook_notes.append(f"last_good_stage={good_stage}")

        if klass == "visual_validation" and job_dir and job_dir.exists():
            rw = rewrite_dark_visual_prompts(job_dir)
            playbook_notes.append(f"rewrite_dark_prompts rewritten={rw.get('rewritten')}")
            out["prompt_rewrite"] = rw
            # Drop invalid failing still so resume regenerates it
            m = re.search(r"(scene_\d+\.(?:jpg|png))", (job.error or "") + "\n" + log_tail)
            if m:
                bad = job_dir / "images" / m.group(1)
                if bad.exists():
                    try:
                        bad.unlink()
                        playbook_notes.append(f"deleted {bad.name}")
                    except OSError as exc:
                        playbook_notes.append(f"delete failed: {exc}")

        # Stills / RunPod transient: drop partial corrupt still named in error if any.
        if klass == "runpod_transient" and job_dir and job_dir.exists():
            m = re.search(r"(scene_\d+\.(?:jpg|png))", (job.error or "") + "\n" + log_tail)
            if m:
                bad = job_dir / "images" / m.group(1)
                if bad.exists() and bad.stat().st_size < 500:
                    try:
                        bad.unlink()
                        playbook_notes.append(f"deleted tiny/corrupt {bad.name}")
                    except OSError as exc:
                        playbook_notes.append(f"delete failed: {exc}")
            playbook_notes.append("resume stills from last good scene")

        # invalidTags: sanitize youtube_meta/tags.json then resume publish.
        if klass == "invalid_tags" and job_dir and job_dir.exists():
            tags_path = job_dir / "youtube_meta" / "tags.json"
            if tags_path.is_file():
                try:
                    from src.services.youtube_meta import rewrite_tags_json

                    rw = rewrite_tags_json(tags_path)
                    playbook_notes.append(
                        f"sanitize_tags before={len(rw.get('before') or [])} "
                        f"after={rw.get('count')} changed={rw.get('changed')}"
                    )
                    out["tag_rewrite"] = rw
                except Exception as exc:  # noqa: BLE001
                    playbook_notes.append(f"sanitize_tags failed: {exc}")
            else:
                playbook_notes.append("tags.json missing — publish will use defaults")
            good_stage = "publish"
            playbook_notes.append("resume publish after tag sanitize")

        # Compose / ffmpeg: keep stills; resume into edit.
        if klass == "edit":
            playbook_notes.append("compose resume — keep images; re-run ffmpeg")

        # Dead farm PID / crash: resume from inferred artifact stage.
        if klass == "process_crash":
            playbook_notes.append(f"process_crash resume from {good_stage}")

        if klass == "tts":
            playbook_notes.append("tts resume — keep script.json; regenerate voice")

        if klass == "script" or klass == "script_json":
            playbook_notes.append(
                "script resume — beat fill / JSON failover; wipe empty script progress"
            )

        repair_counts[klass] = count + 1
        meta["repair_counts"] = repair_counts
        meta["last_repair_class"] = klass
        meta["last_repair_at"] = datetime.now(timezone.utc).isoformat()
        meta["last_repair_notes"] = playbook_notes
        meta["last_good_stage"] = good_stage
        meta["resumable"] = True
        meta["resume"] = True
        meta["hold_class"] = klass
        meta.pop("repair_blocked", None)
        meta.pop("repair_block_reason", None)
        # Stamp intended phase so deferred queued resume does not spawn as full
        # (which falsely holds gpu_lock during scripting/TTS).
        spawn_phase = resume_phase_for_job(job, klass)
        if spawn_phase:
            meta["farm_phase"] = spawn_phase
        if klass in SCRIPT_JSON_AUTO_CLASSES:
            meta["script_json_policy"] = "v2_failover"
            meta["script_json_attempt_cap"] = (
                "per_run: primary 1+2 retries → fallback 1+2 retries; "
                f"watchdog max_script_json_repairs={max_repairs}; then HOLD"
            )
            playbook_notes.append(meta["script_json_attempt_cap"])

        self.store.update_job(
            job_id,
            status="queued",
            stage="queued",
            error=None,
            meta=meta,
        )
        try:
            row = self.queue.find_by_job_id(job_id)
            if row:
                note = f"repair:{klass}#{repair_counts[klass]} from={good_stage}"
                if klass in SCRIPT_JSON_AUTO_CLASSES:
                    note = (
                        f"repair:{klass}#{repair_counts[klass]}/{max_repairs} "
                        "v2_failover (primary 1+2 → fallback 1+2 → HOLD)"
                    )
                self.queue.update_row(
                    row.row_index,
                    status="queued",
                    notes=note[:500],
                    channel=getattr(row, "channel", None) or None,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("title queue sync on repair failed: %s", exc)

        self.ledger.write(
            agent="repair_watchdog",
            problem=f"class={klass} error={(job.error or '')[:200]}",
            action=(
                f"resume farm (count={repair_counts[klass]}"
                f"{', infinite' if infinite else f'/{max_repairs}'}); "
                + "; ".join(playbook_notes)
            ),
            job_id=job_id,
            severity="warn",
            publish_status="queued",
        )
        out["playbook"] = playbook_notes
        out["requeued"] = True
        out["last_good_stage"] = good_stage

        if not spawn:
            out["spawned"] = False
            return out

        # Prefer phase stamped on requeue.
        spawn_phase = meta.get("farm_phase") or resume_phase_for_job(job, klass)
        spawned = spawn_farm_job(
            job_id,
            store=self.store,
            watchdog=self.watchdog,
            phase=spawn_phase,
            # resume flag carried via job.meta + execute auto-detect
        )
        out["farm_phase"] = spawn_phase or "full"
        # Ensure execute uses resume: stamp meta before spawn already set resume=True
        ok_spawn = bool(spawned.get("spawned") or spawned.get("already_running"))
        out["spawned"] = ok_spawn
        out["spawn"] = spawned
        cur = self.store.get_job(job_id)
        cur_meta = dict((cur.meta if cur else {}) or {})
        if not ok_spawn:
            err = str(spawned.get("error") or "spawn failed")
            # Keep row queued + flag deferred so capacity watchdog retries when
            # prep/gpu lock frees — do not leave stranded without farm_pid.
            cur_meta["script_json_spawn_deferred"] = True
            cur_meta["script_json_spawn_error"] = err[:300]
            self.store.update_job(job_id, meta=cur_meta)
            out["deferred_spawn"] = True
            out["spawn_error"] = err
            logger.info(
                "repair spawn deferred job=%s class=%s err=%s",
                job_id,
                klass,
                err[:200],
            )
        elif cur_meta.get("script_json_spawn_deferred"):
            cur_meta.pop("script_json_spawn_deferred", None)
            cur_meta.pop("script_json_spawn_error", None)
            self.store.update_job(job_id, meta=cur_meta)
        return out

    def _block(
        self,
        job: JobRecord,
        klass: str,
        *,
        reason: str,
        out: dict[str, Any],
        alert: bool = True,
        exhausted: bool = False,
    ) -> dict[str, Any]:
        meta = dict(job.meta or {})
        meta["repair_blocked"] = True
        meta["repair_block_reason"] = reason
        meta["last_repair_class"] = klass
        meta["hold_class"] = klass
        meta["resumable"] = False
        meta["resume"] = False
        meta["farm_pid"] = None
        meta["hold_at"] = datetime.now(timezone.utc).isoformat()
        meta["hold_attempt"] = int(meta.get("hold_attempt") or 0) + 1
        # Retry-cap exhaustion: stay HOLD, never terminal FAIL; factory moves on.
        if exhausted or "max repairs" in (reason or "").lower() or "exhausted" in (
            reason or ""
        ).lower():
            meta["repair_exhausted"] = True
            exhausted = True
        self.store.update_job(
            job.id,
            status="hold",
            stage="hold",
            error=f"repair blocked ({klass}): {reason}",
            meta=meta,
        )
        try:
            row = self.queue.find_by_job_id(job.id)
            if row:
                note_prefix = "HOLD exhausted" if exhausted else f"HOLD:{klass}"
                self.queue.update_row(
                    row.row_index,
                    status="hold",
                    notes=f"{note_prefix} — {reason}"[:500],
                    channel=getattr(row, "channel", None) or None,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("title queue sync on hold failed: %s", exc)

        self.ledger.write(
            agent="repair_watchdog",
            problem=f"blocked class={klass}: {reason}",
            action=(
                "HOLD exhausted — skip to next job (no auto-rebuild)"
                if exhausted
                else "HOLD — human required (no auto-rebuild)"
            ),
            job_id=job.id,
            severity="critical",
            publish_status="hold",
            extra={"repair_exhausted": exhausted, "hold_class": klass},
        )
        if alert:
            try:
                self.ledger.alert_now(
                    subject=f"[FARM HOLD] {job.title[:60]}",
                    body=(
                        f"Job `{job.id}` held — class={klass}\n"
                        f"Title: {job.title}\n"
                        f"Reason: {reason}\n"
                        f"Exhausted: {exhausted}\n"
                        f"Error: {(job.error or '')[:800]}\n"
                        f"Job dir: {job.job_dir}\n"
                        f"Watchdog will NOT auto-resume this job; next tick "
                        f"picks the next resumable or approved title.\n"
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("immediate hold alert failed: %s", exc)
        out.update(
            {
                "ok": False,
                "blocked": True,
                "exhausted": exhausted,
                "reason": reason,
                "status": "hold",
            }
        )
        return out
