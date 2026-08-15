"""RunPod cost-safety guards: circuit breaker, bootstrap preflight, stills smoke gate.

Industrial spend caps for ephemeral pods. Prefer refusing work over burning money.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.services.settings import ROOT

logger = logging.getLogger(__name__)

OPS_DIR: Path = ROOT / "output" / "ops"
CIRCUIT_PATH: Path = OPS_DIR / "runpod_circuit.json"
SMOKE_OK_PATH: Path = OPS_DIR / "runpod_stills_smoke_ok.json"
SMOKE_LOCK_PATH: Path = OPS_DIR / "runpod_stills_smoke.lock"

CIRCUIT_WINDOW_HOURS = 6
CIRCUIT_FAIL_THRESHOLD = 2

_LOCK = threading.Lock()


class RunPodGuardError(RuntimeError):
    """Raised when a cost-safety guard refuses pod work."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


def _ensure_ops_dir() -> None:
    OPS_DIR.mkdir(parents=True, exist_ok=True)


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def default_max_minutes(*, kind: str = "stills") -> float:
    """Resolve RUNPOD_POD_MAX_MINUTES.

    Authoritative policy (Rayyan/CTO 2026-08-07):
    - 60 min is a **ceiling for a healthy run** (weights download + stills).
    - Failure → kill immediately; success after local download → kill immediately.
    - Never treat 60 as a minimum dwell time.
    """
    raw = (os.environ.get("RUNPOD_POD_MAX_MINUTES") or "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            logger.warning("Invalid RUNPOD_POD_MAX_MINUTES=%r — using defaults", raw)
    if kind == "voice":
        return 45.0
    # stills + smoke proof share the same healthy-run ceiling
    return 60.0


# ---------------------------------------------------------------------------
# Bootstrap preflight (bash -n)
# ---------------------------------------------------------------------------


def bash_n_check(script: str) -> tuple[bool, str]:
    """Return (ok, stderr). Never raises."""
    try:
        proc = subprocess.run(
            ["bash", "-n"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return False, f"bash -n unavailable: {exc}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or f"rc={proc.returncode}").strip()
    return True, ""


def preflight_docker_args(docker_args: str | None) -> None:
    """Refuse create if dockerArgs / decoded bootstrap fails ``bash -n``.

    Raises RunPodGuardError on syntax failure.
    """
    if not docker_args or not str(docker_args).strip():
        return
    args = str(docker_args).strip()
    ok, err = bash_n_check(args)
    if not ok:
        raise RunPodGuardError(
            f"PREFLIGHT REFUSED: dockerArgs fails bash -n — {err}"
        )
    # Also syntax-check the decoded bootstrap when base64-wrapped.
    try:
        from src.runpod.lifecycle import extract_script_from_docker_args

        script = extract_script_from_docker_args(args)
    except Exception:  # noqa: BLE001
        return
    ok2, err2 = bash_n_check(script)
    if not ok2:
        raise RunPodGuardError(
            f"PREFLIGHT REFUSED: decoded bootstrap fails bash -n — {err2}"
        )
    logger.info("Preflight bash -n OK for dockerArgs (%d chars script)", len(script))


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def _empty_circuit() -> dict[str, Any]:
    return {
        "armed": False,
        "failures": [],
        "trip_count": 0,
        "last_success_at": None,
        "last_failure_at": None,
        "updated_at": utcnow_iso(),
        "note": "armed=true blocks all ephemeral pod creates",
    }


def load_circuit(*, path: Path | None = None) -> dict[str, Any]:
    p = path or CIRCUIT_PATH
    if not p.exists():
        return _empty_circuit()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Corrupt circuit file %s: %s — treating as empty", p, exc)
        return _empty_circuit()
    if not isinstance(data, dict):
        return _empty_circuit()
    data.setdefault("armed", False)
    data.setdefault("failures", [])
    data.setdefault("trip_count", 0)
    return data


def save_circuit(data: dict[str, Any], *, path: Path | None = None) -> Path:
    _ensure_ops_dir()
    p = path or CIRCUIT_PATH
    payload = dict(data)
    payload["updated_at"] = utcnow_iso()
    with _LOCK:
        p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return p


def _prune_failures(
    failures: list[Any], *, now: datetime | None = None, window_h: float = CIRCUIT_WINDOW_HOURS
) -> list[dict[str, Any]]:
    now = now or utcnow()
    cutoff = now - timedelta(hours=window_h)
    kept: list[dict[str, Any]] = []
    for item in failures or []:
        if not isinstance(item, dict):
            continue
        ts = _parse_dt(item.get("at"))
        if ts is None or ts >= cutoff:
            kept.append(item)
    return kept


def circuit_reset(*, reason: str = "explicit_reset", path: Path | None = None) -> dict[str, Any]:
    data = load_circuit(path=path)
    data["armed"] = False
    data["failures"] = []
    data["reset_at"] = utcnow_iso()
    data["reset_reason"] = reason
    save_circuit(data, path=path)
    logger.warning("CIRCUIT RESET (%s) — pod creates allowed again", reason)
    return data


def maybe_env_circuit_reset(*, path: Path | None = None) -> bool:
    """If RUNPOD_CIRCUIT_RESET=1, clear armed and consume the flag from process env."""
    flag = (os.environ.get("RUNPOD_CIRCUIT_RESET") or "").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return False
    circuit_reset(reason="RUNPOD_CIRCUIT_RESET=1", path=path)
    # Prevent repeated resets within same process without operator intent.
    os.environ.pop("RUNPOD_CIRCUIT_RESET", None)
    return True


def assert_circuit_allows_create(*, path: Path | None = None) -> None:
    """Raise RunPodGuardError when the circuit is armed (blocking).

    Nice-to-have: if armed but all failures have aged out of the window,
    auto-unarm so the factory can resume after CIRCUIT_WINDOW_HOURS without
    a manual reset (balance errors still require funds + optional reset).
    """
    maybe_env_circuit_reset(path=path)
    data = load_circuit(path=path)
    if data.get("armed"):
        fails = _prune_failures(list(data.get("failures") or []))
        if not fails:
            circuit_reset(reason="window_expired_auto_unarm", path=path)
            return
        # Persist pruned list so the file stays accurate.
        if len(fails) != len(data.get("failures") or []):
            data["failures"] = fails
            save_circuit(data, path=path)
        msg = (
            "CIRCUIT BREAKER ARMED — refusing pod create after "
            f"{len(fails)} failure(s) in {CIRCUIT_WINDOW_HOURS}h. "
            "Clear by writing armed=false after a successful proof, or set "
            "RUNPOD_CIRCUIT_RESET=1. See output/ops/runpod_circuit.json"
        )
        logger.error(msg)
        raise RunPodGuardError(msg)


def record_create_failure(
    *,
    error: str,
    pod_id: str | None = None,
    gpu: str | None = None,
    cloud: str | None = None,
    path: Path | None = None,
    force_arm: bool = False,
) -> dict[str, Any]:
    """Record a create/ready failure; arm circuit after threshold in window.

    ``force_arm=True`` arms immediately (used for INSUFFICIENT_BALANCE so we
    never burn retries / walk other GPU tiers on a known empty wallet).
    """
    data = load_circuit(path=path)
    now = utcnow()
    failures = _prune_failures(list(data.get("failures") or []), now=now)
    failures.append(
        {
            "at": now.isoformat(),
            "error": (error or "")[:500],
            "pod_id": pod_id,
            "gpu": gpu,
            "cloud": cloud,
            "force_arm": bool(force_arm),
        }
    )
    data["failures"] = failures
    data["last_failure_at"] = now.isoformat()
    if force_arm or len(failures) >= CIRCUIT_FAIL_THRESHOLD:
        data["armed"] = True
        data["trip_count"] = int(data.get("trip_count") or 0) + 1
        data["armed_at"] = now.isoformat()
        if force_arm:
            data["force_armed_reason"] = (error or "")[:200]
            logger.error(
                "CIRCUIT BREAKER FORCE-ARMED — %s — NO MORE POD CREATES until "
                "funds + reset. last_error=%s",
                "INSUFFICIENT_BALANCE / credit guard",
                (error or "")[:200],
            )
        else:
            logger.error(
                "CIRCUIT BREAKER TRIPPED (armed=true) after %d failures in %dh — "
                "NO MORE POD CREATES until reset. last_error=%s",
                len(failures),
                CIRCUIT_WINDOW_HOURS,
                (error or "")[:200],
            )
    else:
        logger.warning(
            "Circuit failure %d/%d in %dh window: %s",
            len(failures),
            CIRCUIT_FAIL_THRESHOLD,
            CIRCUIT_WINDOW_HOURS,
            (error or "")[:200],
        )
    save_circuit(data, path=path)
    return data


def record_create_success(
    *,
    pod_id: str | None = None,
    gpu: str | None = None,
    cloud: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Clear armed on successful ready; keep audit trail lightly."""
    data = load_circuit(path=path)
    was_armed = bool(data.get("armed"))
    data["armed"] = False
    data["failures"] = []
    data["last_success_at"] = utcnow_iso()
    data["last_success"] = {
        "at": data["last_success_at"],
        "pod_id": pod_id,
        "gpu": gpu,
        "cloud": cloud,
    }
    save_circuit(data, path=path)
    if was_armed:
        logger.warning(
            "CIRCUIT CLEARED by success (pod=%s gpu=%s/%s)", pod_id, gpu, cloud
        )
    else:
        logger.info("Circuit success recorded (pod=%s)", pod_id)
    return data


# ---------------------------------------------------------------------------
# Stills smoke gate (farm / sleep_beat)
# ---------------------------------------------------------------------------


def load_smoke_ok(*, path: Path | None = None) -> dict[str, Any] | None:
    p = path or SMOKE_OK_PATH
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def stills_smoke_ok(*, path: Path | None = None) -> bool:
    data = load_smoke_ok(path=path)
    return bool(data and data.get("ok") is True)


def assert_vps_still_paths(
    paths: list[str] | list[Path],
    *,
    min_bytes: int = 100,
    require_under_root: bool = True,
) -> list[Path]:
    """Require still files on the VPS workspace (NOT the RunPod container disk).

    Success = JPGs under ``/home/ubuntu/new_yt_automation/...`` (project ROOT).
    Raises RunPodGuardError if any path is missing, tiny, or outside the repo.
    """
    root = ROOT.resolve()
    verified: list[Path] = []
    for raw in paths or []:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = (ROOT / p).resolve()
        else:
            p = p.resolve()
        if require_under_root:
            try:
                p.relative_to(root)
            except ValueError as exc:
                raise RunPodGuardError(
                    f"VPS path required under project workspace {root}; got {p}"
                ) from exc
        if not p.is_file():
            raise RunPodGuardError(f"Still missing on VPS disk: {p}")
        size = p.stat().st_size
        if size < min_bytes:
            raise RunPodGuardError(
                f"Still too small on VPS disk ({size}B < {min_bytes}B): {p}"
            )
        verified.append(p)
    if not verified:
        raise RunPodGuardError(
            "No still files on VPS workspace — refuse success / refuse kill-as-done"
        )
    return verified


def write_smoke_ok(
    *,
    stills_count: int,
    pod_id: str | None = None,
    gpu: str | None = None,
    cloud: str | None = None,
    paths: list[str] | None = None,
    cost_usd: float | None = None,
    cold_start_s: float | None = None,
    extra: dict[str, Any] | None = None,
    path: Path | None = None,
) -> Path:
    """Write smoke_ok only after JPGs exist on the VPS workspace under ROOT."""
    verified = assert_vps_still_paths(list(paths or []))
    if len(verified) < int(stills_count):
        raise RunPodGuardError(
            f"smoke_ok refused: need {stills_count} VPS JPGs, found {len(verified)}"
        )
    _ensure_ops_dir()
    p = path or SMOKE_OK_PATH
    payload: dict[str, Any] = {
        "ok": True,
        "at": utcnow_iso(),
        "stills_count": int(stills_count),
        "pod_id": pod_id,
        "gpu": gpu,
        "cloud": cloud,
        "paths": [str(x) for x in verified],
        "vps_workspace_root": str(ROOT.resolve()),
        "cost_usd": cost_usd,
        "cold_start_s": cold_start_s,
        "kill_policy": (
            "Pictures must land on our VPS computer first "
            f"({ROOT}/output/...), then we shut off the rented GPU."
        ),
        "note": (
            "Proof only — does NOT re-arm farm cron. "
            "Rayyan/CTO must explicitly re-enable sleep_beat."
        ),
    }
    if extra:
        payload["extra"] = extra
    with _LOCK:
        p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    logger.warning(
        "SMOKE OK written → %s (stills=%s gpu=%s/%s VPS paths verified) — farm stays DISARMED",
        p,
        stills_count,
        gpu,
        cloud,
    )
    return p


def write_smoke_failure(
    *,
    error: str,
    path: Path | None = None,
) -> Path:
    """Optional failure marker beside smoke_ok (does not set ok:true)."""
    _ensure_ops_dir()
    p = (path or SMOKE_OK_PATH).with_name("runpod_stills_smoke_last.json")
    payload = {"ok": False, "at": utcnow_iso(), "error": (error or "")[:1000]}
    p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return p


def image_backend_is_runpod_pod() -> bool:
    try:
        from src.services.settings import get_settings

        s = get_settings()
        raw = (getattr(s, "visuals_backend", None) or getattr(s, "image_backend", "") or "")
        raw = str(raw).strip().lower()
        return raw in {"runpod_pod", "pod"}
    except Exception:  # noqa: BLE001
        env = (
            os.environ.get("VISUALS_BACKEND")
            or os.environ.get("IMAGE_BACKEND")
            or ""
        ).strip().lower()
        return env in {"runpod_pod", "pod"}


def assert_stills_smoke_gate_for_farm(
    *,
    smoke_path: Path | None = None,
    force_check: bool = False,
) -> None:
    """Refuse farm spawn of runpod_pod stills until smoke_ok exists.

    Raises RunPodGuardError when IMAGE_BACKEND=runpod_pod and gate missing.
    """
    if not force_check and not image_backend_is_runpod_pod():
        return
    if stills_smoke_ok(path=smoke_path):
        return
    msg = (
        "STILLS SMOKE GATE CLOSED — refusing farm/sleep_beat runpod_pod stills. "
        "Need output/ops/runpod_stills_smoke_ok.json with ok:true from a controlled "
        "runpod_smoke --stills --count 2 proof. Farm cron stays DISARMED."
    )
    logger.error(msg)
    raise RunPodGuardError(msg)


def farm_may_use_runpod_pod_stills(*, smoke_path: Path | None = None) -> tuple[bool, str]:
    """Non-raising check for sleep_beat / worker."""
    if not image_backend_is_runpod_pod():
        return True, "image backend is not runpod_pod"
    if stills_smoke_ok(path=smoke_path):
        return True, "smoke_ok present"
    return (
        False,
        "runpod_pod stills blocked until output/ops/runpod_stills_smoke_ok.json "
        "has ok:true (run: python -m src.cli.runpod_smoke --stills --count 2)",
    )
