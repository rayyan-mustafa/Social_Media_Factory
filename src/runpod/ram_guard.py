"""RAM guard — remediate VPS memory pressure on each */10 watchdog tick.

Safe actions only:
  - trim tmp under ``output/`` (xfade/tmp/cache) aligned with disk_guard protects
  - kill orphan/zombie ffmpeg NOT tied to active Live RTMP (both channels) or
    active farm compose / xfade jobs
  - optional ``drop_caches`` when still tight (needs root — escalate if blocked)

NEVER kills healthy Live RTMP ffmpeg (napstorian / napping_historian) or active
farming/composing job encodes.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    from src.services.settings import ROOT
except Exception:  # noqa: BLE001
    ROOT = Path(__file__).resolve().parents[2]

OPS_DIR: Path = ROOT / "output" / "ops"
POLICY_PATH: Path = OPS_DIR / "ram_guard_policy.json"
LAST_PATH: Path = OPS_DIR / "ram_guard_last.json"
JSONL_PATH: Path = OPS_DIR / "ram_guard.jsonl"
ALERT_STATE_PATH: Path = OPS_DIR / "ram_alert_state.json"

DEFAULT_POLICY: dict[str, Any] = {
    "enabled": True,
    "compulsory": True,
    # Unhealthy when available RAM below either absolute or percent of total.
    "trigger_available_gb": 1.25,
    "trigger_available_percent": 12.0,
    # Target after remediation (soft — we stop when healthy again).
    "target_available_gb": 2.0,
    "target_available_percent": 20.0,
    # Email only after remediation still unhealthy + cooldown.
    "alert_cooldown_hours": 6.0,
    "allow_drop_caches": True,
    "tmp_globs": [
        "output/jobs/**/_xfade_tmp",
        "output/jobs/**/_compose_tmp",
        "output/tmp",
        "output/cache/tmp",
        "/tmp/yt_farm_*",
        "/tmp/ffmpeg_*",
    ],
}

# Cmdline markers that mean "do not kill this ffmpeg".
_LIVE_PROTECT_MARKERS: tuple[str, ...] = (
    "rtmp://",
    "rtmps://",
    "a.rtmp.youtube.com",
    "b.rtmp.youtube.com",
    "playlist_napstorian.txt",
    "playlist_napping_historian.txt",
    "vod_loop_napstorian.progress",
    "vod_loop_napping_historian.progress",
)

_FARM_PROTECT_MARKERS: tuple[str, ...] = (
    "_xfade_tmp",
    "scenes/scene_",
    "video_final_production",
    "filter_complex",
    "acrossfade",
    "xfade=",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso() -> str:
    return _utc_now().isoformat()


def _ram_guard_env_disabled() -> bool:
    return (os.getenv("RAM_GUARD") or "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }


def load_policy() -> dict[str, Any]:
    policy = dict(DEFAULT_POLICY)
    if POLICY_PATH.is_file():
        try:
            data = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                policy.update(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ram_guard policy load failed: %s", exc)
    else:
        try:
            OPS_DIR.mkdir(parents=True, exist_ok=True)
            POLICY_PATH.write_text(
                json.dumps(DEFAULT_POLICY, indent=2) + "\n", encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ram_guard policy seed failed: %s", exc)
    return policy


def read_meminfo() -> dict[str, Any]:
    """Parse /proc/meminfo → bytes + derived GB/percents (no secrets)."""
    raw: dict[str, int] = {}
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return {"ok": False, "error": str(exc)[:200]}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        parts = rest.strip().split()
        if not parts:
            continue
        try:
            kib = int(parts[0])
        except ValueError:
            continue
        raw[key.strip()] = kib * 1024
    total = int(raw.get("MemTotal") or 0)
    available = int(raw.get("MemAvailable") or raw.get("MemFree") or 0)
    free = int(raw.get("MemFree") or 0)
    buffers = int(raw.get("Buffers") or 0)
    cached = int(raw.get("Cached") or 0)
    swap_total = int(raw.get("SwapTotal") or 0)
    swap_free = int(raw.get("SwapFree") or 0)
    avail_pct = (100.0 * available / total) if total > 0 else 0.0
    return {
        "ok": True,
        "total_bytes": total,
        "available_bytes": available,
        "free_bytes": free,
        "buffers_bytes": buffers,
        "cached_bytes": cached,
        "swap_total_bytes": swap_total,
        "swap_free_bytes": swap_free,
        "total_gb": round(total / 1e9, 3),
        "available_gb": round(available / 1e9, 3),
        "free_gb": round(free / 1e9, 3),
        "available_percent": round(avail_pct, 2),
        "used_percent": round(100.0 - avail_pct, 2) if total > 0 else 0.0,
    }


def is_ram_unhealthy(
    mem: dict[str, Any],
    policy: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """True when MemAvailable is below absolute GB or percent threshold."""
    pol = policy or load_policy()
    if not mem.get("ok"):
        return False, "meminfo_unavailable"
    avail_gb = float(mem.get("available_gb") or 0)
    avail_pct = float(mem.get("available_percent") or 0)
    trig_gb = float(pol.get("trigger_available_gb") or DEFAULT_POLICY["trigger_available_gb"])
    trig_pct = float(
        pol.get("trigger_available_percent")
        or DEFAULT_POLICY["trigger_available_percent"]
    )
    reasons: list[str] = []
    if avail_gb < trig_gb:
        reasons.append(f"available_gb={avail_gb}<{trig_gb}")
    if avail_pct < trig_pct:
        reasons.append(f"available_pct={avail_pct}<{trig_pct}")
    if reasons:
        return True, "+".join(reasons)
    return False, f"ok_avail_gb={avail_gb}_pct={avail_pct}"


def is_ram_healthy_target(
    mem: dict[str, Any],
    policy: dict[str, Any] | None = None,
) -> bool:
    pol = policy or load_policy()
    if not mem.get("ok"):
        return False
    avail_gb = float(mem.get("available_gb") or 0)
    avail_pct = float(mem.get("available_percent") or 0)
    tgt_gb = float(pol.get("target_available_gb") or DEFAULT_POLICY["target_available_gb"])
    tgt_pct = float(
        pol.get("target_available_percent") or DEFAULT_POLICY["target_available_percent"]
    )
    return avail_gb >= tgt_gb or avail_pct >= tgt_pct


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "ignore")
    except Exception:
        return ""


def _is_zombie(pid: int) -> bool:
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    for line in status.splitlines():
        if line.startswith("State:"):
            # Z = zombie
            return " Z" in f" {line}" or line.split(":", 1)[1].strip().startswith("Z")
    return False


def _ffmpeg_protected(flat_cmd: str) -> tuple[bool, str]:
    low = flat_cmd.lower()
    for m in _LIVE_PROTECT_MARKERS:
        if m.lower() in low:
            return True, f"live:{m}"
    for m in _FARM_PROTECT_MARKERS:
        if m.lower() in low:
            return True, f"farm:{m}"
    # Progress files for Live
    if "vod_loop_" in low and ".progress" in low:
        return True, "live:progress"
    return False, ""


def list_orphan_ffmpeg_candidates() -> list[dict[str, Any]]:
    """FFmpeg PIDs safe to consider for kill under RAM pressure."""
    out: list[dict[str, Any]] = []
    try:
        entries = list(Path("/proc").iterdir())
    except Exception:
        return out
    for ent in entries:
        if not ent.name.isdigit():
            continue
        pid = int(ent.name)
        cmd = _cmdline(pid)
        if not cmd:
            continue
        argv0 = cmd.split("\0", 1)[0]
        if Path(argv0).name != "ffmpeg":
            continue
        flat = cmd.replace("\0", " ")
        zombie = _is_zombie(pid)
        protected, why = _ffmpeg_protected(flat)
        out.append(
            {
                "pid": pid,
                "zombie": zombie,
                "protected": protected,
                "protect_reason": why,
                "cmd_preview": flat[:180],
            }
        )
    return out


def kill_orphan_ffmpeg(*, dry_run: bool = False) -> dict[str, Any]:
    killed: list[int] = []
    skipped: list[dict[str, Any]] = []
    for row in list_orphan_ffmpeg_candidates():
        if row.get("protected") and not row.get("zombie"):
            skipped.append(
                {"pid": row["pid"], "reason": row.get("protect_reason") or "protected"}
            )
            continue
        # Kill zombies always; kill non-protected orphans under pressure.
        pid = int(row["pid"])
        if dry_run:
            killed.append(pid)
            continue
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            continue
        except Exception as exc:  # noqa: BLE001
            skipped.append({"pid": pid, "reason": type(exc).__name__})
            continue
        # Escalate if still alive briefly
        try:
            import time

            time.sleep(0.15)
            os.kill(pid, 0)
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        except Exception:
            pass
        killed.append(pid)
    return {
        "killed": killed,
        "skipped_protected": skipped,
        "n_killed": len(killed),
        "n_skipped": len(skipped),
    }


def _safe_rmtree(path: Path, *, dry_run: bool = False) -> int:
    """Delete a tmp dir; return bytes approx freed (0 if dry_run/missing)."""
    if not path.exists():
        return 0
    # Never leave ROOT / factory
    try:
        resolved = path.resolve()
    except Exception:
        return 0
    root = ROOT.resolve()
    if resolved == root or root in resolved.parents and any(
        part in {"src", "config", "secrets", ".venv", ".git"}
        for part in resolved.relative_to(root).parts[:1]
    ):
        return 0
    size = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    size += p.stat().st_size
            except OSError:
                pass
    except Exception:
        size = 0
    if dry_run:
        return size
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file():
            path.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ram_guard trim failed %s: %s", path, exc)
        return 0
    return size


def trim_tmp_dirs(policy: dict[str, Any] | None = None, *, dry_run: bool = False) -> dict[str, Any]:
    """Trim known tmp trees; honor disk_guard protected job dirs when available."""
    pol = policy or load_policy()
    protected: set[Path] = set()
    try:
        from src.runpod.disk_guard import protected_paths

        protected = protected_paths()
    except Exception:  # noqa: BLE001
        protected = set()
    freed = 0
    trimmed: list[str] = []
    skipped: list[str] = []
    import glob as _glob

    for pattern in pol.get("tmp_globs") or DEFAULT_POLICY["tmp_globs"]:
        pat = pattern if pattern.startswith("/") else str(ROOT / pattern)
        for match in _glob.glob(pat, recursive=True):
            p = Path(match)
            try:
                resolved = p.resolve()
            except Exception:
                continue
            under_protect = False
            for pd in protected:
                try:
                    if resolved == pd or pd in resolved.parents or resolved in pd.parents:
                        # Only skip if this tmp is inside an active protected job
                        if pd in resolved.parents or resolved == pd:
                            under_protect = True
                            break
                except Exception:
                    continue
            # Always allow _xfade_tmp / _compose_tmp under jobs — those are the
            # intended RAM relief; disk_guard already protects active farm dirs
            # via protect_statuses. If parent job is protected, skip.
            if under_protect and resolved.name not in {"_xfade_tmp", "_compose_tmp"}:
                skipped.append(str(resolved))
                continue
            if under_protect and any(pd in resolved.parents for pd in protected):
                # Active farm job — do not delete its in-flight xfade.
                skipped.append(str(resolved))
                continue
            bytes_freed = _safe_rmtree(resolved, dry_run=dry_run)
            if bytes_freed or dry_run:
                trimmed.append(str(resolved))
                freed += int(bytes_freed)
    return {
        "trimmed": trimmed[:80],
        "skipped_protected": skipped[:40],
        "bytes_freed": freed,
        "gb_freed": round(freed / 1e9, 4),
    }


def try_drop_caches(*, dry_run: bool = False) -> dict[str, Any]:
    """Echo 3 > /proc/sys/vm/drop_caches via sudo -n when permitted."""
    out: dict[str, Any] = {
        "attempted": False,
        "ok": False,
        "needs_approval": False,
        "detail": "",
    }
    if dry_run:
        out["attempted"] = True
        out["ok"] = True
        out["detail"] = "dry_run_would_drop_caches"
        return out
    # Prefer non-interactive sudo
    cmd = [
        "sudo",
        "-n",
        "sh",
        "-c",
        "sync; echo 3 > /proc/sys/vm/drop_caches",
    ]
    out["attempted"] = True
    try:
        proc = subprocess.run(
            cmd, check=False, capture_output=True, text=True, timeout=30
        )
    except Exception as exc:  # noqa: BLE001
        out["detail"] = type(exc).__name__
        out["needs_approval"] = True
        return out
    if proc.returncode == 0:
        out["ok"] = True
        out["detail"] = "dropped"
        return out
    err = (proc.stderr or proc.stdout or "").strip()[:240]
    out["detail"] = err or f"rc={proc.returncode}"
    out["needs_approval"] = True
    return out


def _append_jsonl(row: dict[str, Any]) -> None:
    try:
        OPS_DIR.mkdir(parents=True, exist_ok=True)
        with JSONL_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("ram_guard jsonl write failed: %s", exc)


def _write_last(payload: dict[str, Any]) -> None:
    try:
        OPS_DIR.mkdir(parents=True, exist_ok=True)
        LAST_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("ram_guard last write failed: %s", exc)


def _load_alert_state() -> dict[str, Any]:
    if not ALERT_STATE_PATH.is_file():
        return {}
    try:
        return json.loads(ALERT_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_alert_state(state: dict[str, Any]) -> None:
    OPS_DIR.mkdir(parents=True, exist_ok=True)
    ALERT_STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def maybe_email_ram_unresolved(
    *,
    mem: dict[str, Any],
    remediation: dict[str, Any],
    policy: dict[str, Any],
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Email only if still unhealthy after remediation + cooldown."""
    out: dict[str, Any] = {"sent_email": False, "skipped": False}
    unhealthy, why = is_ram_unhealthy(mem, policy)
    if not unhealthy and not force:
        out["skipped"] = True
        out["reason"] = "healthy_after_remediation"
        return out
    state = _load_alert_state()
    last_at = str(state.get("last_email_at") or "")
    cooldown_h = float(policy.get("alert_cooldown_hours") or 6.0)
    if last_at and not force:
        try:
            prev = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
            age_h = (_utc_now() - prev).total_seconds() / 3600.0
            if age_h < cooldown_h:
                out["skipped"] = True
                out["reason"] = f"cooldown_{age_h:.1f}h"
                return out
        except ValueError:
            pass
    subject = (
        f"[YT RAM] VPS memory still low — avail "
        f"{mem.get('available_gb')}GB ({mem.get('available_percent')}%)"
    )
    body = (
        f"# RAM unresolved after remediation\n\n"
        f"- reason: `{why}`\n"
        f"- available_gb: `{mem.get('available_gb')}` / total `{mem.get('total_gb')}`\n"
        f"- available_percent: `{mem.get('available_percent')}`\n"
        f"- actions: `{json.dumps(remediation.get('actions') or [], default=str)[:500]}`\n"
        f"- drop_caches: `{remediation.get('drop_caches')}`\n"
        f"- orphan_ffmpeg_killed: `{(remediation.get('orphan_ffmpeg') or {}).get('n_killed')}`\n"
        f"\nNever kills Live RTMP or active farm compose encodes.\n"
    )
    if dry_run:
        out["dry_run"] = True
        out["subject"] = subject
        return out
    try:
        from src.agents.ledger import OpsLedger

        mail = OpsLedger().alert_now(subject=subject, body=body)
        out["sent_email"] = bool(mail.get("sent_email"))
        out["mail"] = {
            k: mail.get(k) for k in ("ok", "sent_email", "send_detail", "path")
        }
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
        return out
    _save_alert_state(
        {
            "last_email_at": _utc_iso(),
            "last_reason": why,
            "last_available_gb": mem.get("available_gb"),
        }
    )
    return out


def maybe_run_ram_guard(
    *,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """One tick: measure → if unhealthy remediate → remeasure → maybe email."""
    policy = load_policy()
    result: dict[str, Any] = {
        "ok": True,
        "module": "ram_guard",
        "ts": _utc_iso(),
        "dry_run": dry_run,
        "forced": force,
        "actions": [],
    }
    if _ram_guard_env_disabled():
        result["skipped"] = True
        result["message"] = "ram_guard disabled via RAM_GUARD=0"
        _write_last(result)
        return result
    if not policy.get("enabled", True) and not policy.get("compulsory", True) and not force:
        result["skipped"] = True
        result["message"] = "ram_guard disabled via policy.enabled=false"
        _write_last(result)
        return result

    before = read_meminfo()
    result["before"] = before
    unhealthy, reason = is_ram_unhealthy(before, policy)
    result["unhealthy"] = unhealthy
    result["unhealthy_reason"] = reason
    if not unhealthy and not force:
        result["message"] = f"ram_ok {reason}"
        result["after"] = before
        _append_jsonl(
            {
                "ts": result["ts"],
                "unhealthy": False,
                "available_gb": before.get("available_gb"),
                "available_percent": before.get("available_percent"),
            }
        )
        _write_last(result)
        return result

    actions: list[str] = []
    # 1) Trim tmp
    trim = trim_tmp_dirs(policy, dry_run=dry_run)
    result["trim_tmp"] = trim
    if trim.get("trimmed"):
        actions.append(f"trim_tmp_n={len(trim['trimmed'])}")
    # 2) Kill orphan/zombie ffmpeg (never Live / active farm)
    orphans = kill_orphan_ffmpeg(dry_run=dry_run)
    result["orphan_ffmpeg"] = orphans
    if orphans.get("n_killed"):
        actions.append(f"kill_orphan_ffmpeg_n={orphans['n_killed']}")

    mid = read_meminfo()
    still_bad, mid_reason = is_ram_unhealthy(mid, policy)
    result["after_soft"] = mid
    result["still_unhealthy_after_soft"] = still_bad

    drop: dict[str, Any] = {"skipped": True, "reason": "not_needed"}
    if (still_bad or force) and policy.get("allow_drop_caches", True):
        drop = try_drop_caches(dry_run=dry_run)
        actions.append("drop_caches_attempted" if drop.get("attempted") else "drop_caches_skip")
        if drop.get("needs_approval"):
            result["approval_needed"] = {
                "command": "sudo -n sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'",
                "why": "RAM still low after tmp trim + orphan ffmpeg cull; drop_caches needs root",
                "approval": "terminal | all",
            }
    result["drop_caches"] = drop
    result["actions"] = actions

    after = read_meminfo()
    result["after"] = after
    still_after, after_reason = is_ram_unhealthy(after, policy)
    result["still_unhealthy"] = still_after
    result["after_reason"] = after_reason
    result["message"] = (
        f"ram_remediated avail_gb={before.get('available_gb')}→{after.get('available_gb')} "
        f"still_unhealthy={still_after}"
    )

    alert = {"skipped": True, "reason": "healthy"}
    if still_after or force:
        alert = maybe_email_ram_unresolved(
            mem=after,
            remediation=result,
            policy=policy,
            force=force,
            dry_run=dry_run,
        )
    result["alert"] = alert

    _append_jsonl(
        {
            "ts": result["ts"],
            "unhealthy": True,
            "before_gb": before.get("available_gb"),
            "after_gb": after.get("available_gb"),
            "still_unhealthy": still_after,
            "actions": actions,
            "emailed": bool(alert.get("sent_email")),
        }
    )
    _write_last(result)
    logger.info("ram_guard: %s", result["message"])
    return result


run_ram_guard = maybe_run_ram_guard
