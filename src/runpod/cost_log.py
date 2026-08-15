"""Persistent RunPod pod cost recording.

Every ephemeral pod session (src/runpod/lifecycle.py) logs its burn on exit:
one JSON line in output/ops/runpod_pod_costs.jsonl (audit trail) plus an
OpsStore.record_spend(category="runpod", ...) item so CostGuardian's monthly
budget checks see real pod burn, not only per-video estimates.

Estimates are $/hr × wall duration; the RunPod billing API stays the
authority for actual charges.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.services.settings import ROOT

logger = logging.getLogger(__name__)

JSONL_PATH: Path = ROOT / "output" / "ops" / "runpod_pod_costs.jsonl"
_LOCK = threading.Lock()

# Live 2026-08-07 on-demand $/hr for the configured GPU walk — fallback when
# the pod payload carries no costPerHr.
FALLBACK_USD_PER_HOUR: dict[tuple[str, str], float] = {
    ("NVIDIA RTX A5000", "COMMUNITY"): 0.16,
    ("NVIDIA RTX A5000", "SECURE"): 0.27,
    ("NVIDIA GEFORCE RTX 3090", "COMMUNITY"): 0.22,
    ("NVIDIA A40", "SECURE"): 0.44,
    ("NVIDIA RTX 2000 ADA GENERATION", "SECURE"): 0.24,
    ("NVIDIA GEFORCE RTX 4090", "SECURE"): 0.74,
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def rate_for(gpu: str | None, cloud: str | None) -> float | None:
    key = ((gpu or "").strip().upper(), (cloud or "").strip().upper())
    return FALLBACK_USD_PER_HOUR.get(key)


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _make_store():
    # Deferred import: keeps lifecycle import light and eases test monkeypatching.
    from src.agents.store import OpsStore

    return OpsStore()


def log_pod_cost(
    *,
    pod_id: str,
    gpu: str | None,
    cloud: str | None,
    usd_per_hour: float | None,
    started_at: Any,
    ended_at: Any = None,
    purpose: str = "unknown",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Record one pod's burn in the JSONL log + ops spend ledger.

    Never raises — cost recording must not break pod teardown.
    """
    try:
        start = _parse_dt(started_at)
        end = _parse_dt(ended_at) or datetime.now(timezone.utc)
        minutes: float | None = None
        if start is not None:
            minutes = round(max(0.0, (end - start).total_seconds()) / 60.0, 2)
        rate = float(usd_per_hour) if usd_per_hour else rate_for(gpu, cloud)
        estimated: float | None = None
        if rate is not None and minutes is not None:
            estimated = round(rate * minutes / 60.0, 4)
        entry: dict[str, Any] = {
            "ts": utcnow_iso(),
            "pod_id": pod_id,
            "purpose": purpose,
            "gpu": gpu,
            "cloud": cloud,
            "usd_per_hour": rate,
            "started_at": start.isoformat() if start else None,
            "ended_at": end.isoformat(),
            "minutes": minutes,
            "estimated_usd": estimated,
            "extra": extra or {},
        }
        with _LOCK:
            JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
            with JSONL_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if estimated is not None:
            try:
                _make_store().record_spend(
                    category="runpod",
                    amount_usd=estimated,
                    note=(
                        f"pod {pod_id} {purpose} {gpu}/{cloud} "
                        f"{minutes}min @ ${rate}/hr"
                    ),
                )
            except Exception:
                logger.exception(
                    "cost_log: record_spend failed for pod %s", pod_id
                )
        else:
            logger.warning(
                "cost_log: no rate/duration for pod %s (gpu=%s cloud=%s) — "
                "JSONL entry only",
                pod_id,
                gpu,
                cloud,
            )
        return entry
    except Exception:
        logger.exception("cost_log: failed to log pod %s", pod_id)
        return None


def log_session_cost(session: Any, *, ended_at: Any = None) -> dict[str, Any] | None:
    """Log the cost of an EphemeralPodSession at exit. Never raises."""
    try:
        pod = session.pod or {}
        pod_id = session.pod_id
        if not pod_id:
            return None
        spec = session.spec
        gpu = session.selected_gpu_type_id
        if not gpu:
            raw_gpu = pod.get("gpu")
            if isinstance(raw_gpu, dict):
                gpu = raw_gpu.get("id")
        gpu = gpu or getattr(spec, "gpu_type_id", None)
        cloud = (
            session.selected_cloud_type
            or pod.get("cloud")
            or getattr(spec, "cloud_type", None)
        )
        rate: float | None = None
        for key in ("costPerHr", "adjustedCostPerHr", "cost"):
            value = pod.get(key)
            if value:
                try:
                    rate = float(value)
                    break
                except (TypeError, ValueError):
                    continue
        started = (
            pod.get("createdAt")
            or pod.get("created_at")
            or pod.get("startedAt")
            or pod.get("lastStartedAt")
            or getattr(session, "created_at_utc", None)
        )
        return log_pod_cost(
            pod_id=str(pod_id),
            gpu=gpu,
            cloud=cloud,
            usd_per_hour=rate,
            started_at=started,
            ended_at=ended_at,
            purpose=str(getattr(spec, "name_prefix", "") or "unknown"),
            extra={"attempts": len(getattr(session, "attempts", None) or [])},
        )
    except Exception:
        logger.exception("cost_log: failed to log session cost")
        return None
