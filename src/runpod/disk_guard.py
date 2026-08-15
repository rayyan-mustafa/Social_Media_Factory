"""Disk guard — free space when usage crosses stored industry-ish thresholds.

Safe cleanup only under ``output/`` (jobs media / old terminal job dirs / ops logs)
plus optional system hygiene (journal vacuum, apt clean, idle docker prune).

NEVER touches factory surfaces: ``src/``, ``.env``, ``secrets/``, ``config/``,
``.venv``, systemd units, or active farming job directories.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    from src.services.settings import ROOT
except Exception:  # noqa: BLE001
    ROOT = Path(__file__).resolve().parents[2]

OPS_DIR: Path = ROOT / "output" / "ops"
JOBS_DIR: Path = ROOT / "output" / "jobs"
LIVE_VODS_DIR: Path = ROOT / "output" / "live_vods"
JOBS_JSON: Path = OPS_DIR / "jobs.json"
POLICY_PATH: Path = OPS_DIR / "disk_guard_policy.json"
LAST_PATH: Path = OPS_DIR / "disk_guard_last.json"

# Absolute factory deny-list (path prefixes relative to ROOT).
FACTORY_PREFIXES: tuple[str, ...] = (
    "src",
    "config",
    "secrets",
    ".venv",
    "venv",
    ".git",
    "alembic",
    "docs",
    "tests",
    "node_modules",
)

FACTORY_FILES: tuple[str, ...] = (
    ".env",
    ".env.local",
    ".env.production",
    "credentials.json",
    "service_account.json",
)

DEFAULT_POLICY: dict[str, Any] = {
    # Compulsory under FACTORY_ALWAYS_ON / */10 watchdog. Soft-disable is NOT
    # supported via this flag — use env DISK_GUARD=0 only as an emergency brake.
    "enabled": True,
    "compulsory": True,
    "mount": "/",
    "trigger_used_percent": 85.0,
    "trigger_free_gb": 8.0,
    "target_used_percent": 75.0,
    "target_free_gb": 15.0,
    "terminal_statuses": ["public", "private", "failed"],
    "protect_statuses": [
        "farming",
        "running",
        "prep",
        "composing",
        "uploading",
        "hold",
        "queued",
        "scheduled",
        "gpu",
        "visuals",
        "in_progress",
    ],
    "media_extensions": [
        ".mp4",
        ".mov",
        ".webm",
        ".mkv",
        ".avi",
        ".wav",
        ".mp3",
        ".m4a",
        ".flac",
        ".ogg",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".bmp",
        ".tiff",
    ],
    "keep_filenames": [
        "script.json",
        "meta.json",
        "job.json",
        "manifest.json",
        "pipeline.json",
        "status.json",
        "title.txt",
        "description.txt",
        "tags.txt",
        "final.mp4",
        "publish_manifest.json",
        "voice_manifest.json",
        "visual_manifest.json",
        "edit_manifest.json",
    ],
    # Thin metadata kept after status=public (heavy media purged separately).
    "public_keep_filenames": [
        "script.json",
        "meta.json",
        "job.json",
        "manifest.json",
        "pipeline.json",
        "status.json",
        "title.txt",
        "description.txt",
        "tags.txt",
        "publish_manifest.json",
        "voice_manifest.json",
        "visual_manifest.json",
        "edit_manifest.json",
        "sop_compliance.json",
    ],
    "public_keep_dirnames": [
        "script",
        "ops",
        "youtube_meta",
    ],
    "public_purge_dirnames": [
        "_xfade_tmp",
        "_xfade_parts",
        "scenes",
        "images",
        "audio",
        "derivatives",
    ],
    # Priority #1 after the 2026-08-08 94% incident: compose intermediates.
    "xfade_tmp_dirname": "_xfade_tmp",
    "xfade_require_final_mp4": True,
    # Always wipe _xfade_tmp when final.mp4 exists — ignore 85%/8G gate.
    "xfade_always_when_final": True,
    "xfade_min_age_hours": 0,
    "media_min_age_hours": 12,
    "whole_job_min_age_days": 3,
    "orphan_job_dir_min_age_days": 2,
    "log_min_age_days": 3,
    "log_truncate_bytes": 2_000_000,
    "max_delete_actions": 400,
    "allow_journal_vacuum": True,
    "journal_vacuum_max_size": "200M",
    "allow_apt_clean": True,
    "allow_docker_prune": True,
    "docker_prune_only_if_idle": True,
    # After status=public: drop heavy local media; keep thin manifests.
    # Live encode streams from output/live_vods (downloaded top publics), so
    # farm job finals are NOT kept forever. Playlist / force-lock paths and
    # the entire live_vods tree remain protected.
    "public_purge_enabled": True,
    "public_purge_final_mp4": True,
    "public_purge_keep_live_pool_finals": True,
    "live_pool_keep_top_n": 8,
    "public_purge_keep_all_public_finals": False,
    "protect_live_vods": True,
}


def _disk_guard_env_disabled() -> bool:
    """Emergency brake only. Default is ON (compulsory)."""
    raw = (os.getenv("DISK_GUARD") or "1").strip().lower()
    return raw in {"0", "false", "no", "off"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def load_policy(path: Path | None = None) -> dict[str, Any]:
    """Load stored policy, merging over defaults. Creates file if missing."""
    p = path or POLICY_PATH
    policy = dict(DEFAULT_POLICY)
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                policy.update(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("disk_guard policy load failed: %s", exc)
    else:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(policy, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("disk_guard policy seed failed: %s", exc)
    return policy


def disk_snapshot(mount: str = "/") -> dict[str, Any]:
    usage = shutil.disk_usage(mount)
    total = int(usage.total)
    used = int(usage.used)
    free = int(usage.free)
    used_pct = (used / total * 100.0) if total else 0.0
    free_gb = free / (1024**3)
    return {
        "mount": mount,
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "used_percent": round(used_pct, 2),
        "free_gb": round(free_gb, 2),
    }


def threshold_crossed(snap: dict[str, Any], policy: dict[str, Any]) -> bool:
    used_pct = float(snap.get("used_percent") or 0)
    free_gb = float(snap.get("free_gb") or 0)
    return used_pct >= float(policy["trigger_used_percent"]) or free_gb <= float(
        policy["trigger_free_gb"]
    )


def target_reached(snap: dict[str, Any], policy: dict[str, Any]) -> bool:
    used_pct = float(snap.get("used_percent") or 0)
    free_gb = float(snap.get("free_gb") or 0)
    return used_pct <= float(policy["target_used_percent"]) and free_gb >= float(
        policy["target_free_gb"]
    )


def _resolve_under_root(path: Path) -> Path | None:
    try:
        resolved = path.resolve()
        root = ROOT.resolve()
        resolved.relative_to(root)
        return resolved
    except Exception:  # noqa: BLE001
        return None


def _is_live_vods_path(path: Path) -> bool:
    """True when path is under output/live_vods — never wipe Live library."""
    resolved = _resolve_under_root(path)
    if resolved is None:
        return False
    try:
        live_root = LIVE_VODS_DIR.resolve()
    except Exception:  # noqa: BLE001
        live_root = LIVE_VODS_DIR
    try:
        resolved.relative_to(live_root)
        return True
    except Exception:  # noqa: BLE001
        return False


def _is_factory_path(path: Path) -> bool:
    resolved = _resolve_under_root(path)
    if resolved is None:
        # Outside ROOT — refuse
        return True
    root = ROOT.resolve()
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return True
    parts = rel.parts
    if not parts:
        return True
    if parts[0] in FACTORY_PREFIXES:
        return True
    if parts[0] in FACTORY_FILES:
        return True
    if resolved.name in FACTORY_FILES:
        return True
    # Never delete anything outside output/
    if parts[0] != "output":
        return True
    # Live library is purge-exempt (dedicated copies of top publics).
    if len(parts) >= 2 and parts[1] == "live_vods":
        return True
    return False


def _live_vods_final_paths() -> set[Path]:
    out: set[Path] = set()
    if not LIVE_VODS_DIR.is_dir():
        return out
    try:
        for fp in LIVE_VODS_DIR.rglob("final.mp4"):
            if fp.is_file():
                out.add(fp.resolve())
    except Exception:  # noqa: BLE001
        return out
    return out


def _load_jobs() -> list[dict[str, Any]]:
    if not JOBS_JSON.exists():
        return []
    try:
        data = json.loads(JOBS_JSON.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if isinstance(jobs, dict):
        return [j for j in jobs.values() if isinstance(j, dict)]
    if isinstance(jobs, list):
        return [j for j in jobs if isinstance(j, dict)]
    return []


def _job_dir_path(raw: Any) -> Path | None:
    if not raw:
        return None
    try:
        p = Path(str(raw))
        if not p.is_absolute():
            p = ROOT / p
        return _resolve_under_root(p)
    except Exception:  # noqa: BLE001
        return None


def _parse_ts(raw: Any) -> datetime | None:
    if raw is None:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _active_farm_out_dirs() -> set[Path]:
    """Protect any ``--out-dir`` currently used by a live farm process."""
    protected: set[Path] = set()
    try:
        proc = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        blob = proc.stdout or ""
    except Exception:  # noqa: BLE001
        return protected
    for line in blob.splitlines():
        if "run_farm_job" not in line and "farm_job" not in line:
            continue
        if "--out-dir" not in line:
            continue
        try:
            after = line.split("--out-dir", 1)[1].strip()
            token = after.split()[0].strip().strip("'\"")
            p = _job_dir_path(token)
            if p is not None:
                protected.add(p)
        except Exception:  # noqa: BLE001
            continue
    return protected


def protected_paths(policy: dict[str, Any] | None = None) -> set[Path]:
    policy = policy or load_policy()
    protect_statuses = {
        str(s).strip().lower() for s in (policy.get("protect_statuses") or [])
    }
    out: set[Path] = set(_active_farm_out_dirs())
    for job in _load_jobs():
        status = str(job.get("status") or job.get("state") or "").strip().lower()
        jdir = _job_dir_path(job.get("job_dir") or job.get("dir"))
        if jdir is None:
            continue
        if status in protect_statuses or status == "farming":
            out.add(jdir)
    return out


def _dir_age(path: Path, now: datetime) -> timedelta | None:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None
    return now - mtime


def _bytes_of(path: Path) -> int:
    try:
        if path.is_file():
            return int(path.stat().st_size)
        total = 0
        for root, _dirs, files in os.walk(path, followlinks=False):
            for name in files:
                fp = Path(root) / name
                try:
                    total += int(fp.stat().st_size)
                except OSError:
                    continue
        return total
    except OSError:
        return 0


def _safe_unlink(path: Path, *, dry_run: bool) -> int:
    if _is_factory_path(path) or _is_live_vods_path(path):
        return 0
    size = _bytes_of(path)
    if dry_run:
        return size
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=False)
        elif path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)
        else:
            return 0
        return size
    except OSError as exc:
        logger.warning("disk_guard delete failed %s: %s", path, exc)
        return 0


def _has_final_mp4(jdir: Path) -> bool:
    for name in ("final.mp4", "video/final.mp4"):
        if (jdir / name).is_file():
            return True
    # Common compose layout
    video = jdir / "video"
    if video.is_dir():
        for fp in video.glob("*.mp4"):
            if fp.name.lower() == "final.mp4" or "final" in fp.name.lower():
                return True
    return False


def _sibling_final_mp4(xfade_dir: Path) -> Path | None:
    """``…/video/_xfade_tmp`` → ``…/video/final.mp4`` when present."""
    sibling = xfade_dir.parent / "final.mp4"
    if sibling.is_file():
        return sibling
    return None


def _live_force_lock_paths() -> set[Path]:
    """Pinned Live head finals (files) — never purge these paths."""
    lock_path = OPS_DIR / "live_playlist_force_lock.json"
    out: set[Path] = set()
    if not lock_path.is_file():
        return out
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return out
    if not isinstance(data, dict) or not data.get("locked"):
        return out
    heads = data.get("heads") or {}
    if not isinstance(heads, dict):
        return out
    for raw in list(heads.values()) + list(data.get("exclude_paths") or []):
        p = _job_dir_path(raw)
        if p is not None:
            out.add(p)
    return out


def _playlist_final_paths() -> set[Path]:
    """Current Live playlist entries (heads + tails) — never purge these finals."""
    out: set[Path] = set()
    stream_cfg = ROOT / "config" / "streaming"
    if not stream_cfg.is_dir():
        return out
    for pl in stream_cfg.glob("playlist_*.txt"):
        try:
            text = pl.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        for line in text.splitlines():
            s = line.strip()
            if not s.lower().startswith("file "):
                continue
            raw = s[5:].strip().strip("'\"")
            if not raw:
                continue
            p = Path(raw)
            try:
                if p.is_file():
                    out.add(p.resolve())
            except Exception:  # noqa: BLE001
                continue
    # Featured tracker may point at a head not yet rewritten into playlist.
    featured = OPS_DIR / "smm_live_featured.json"
    if featured.is_file():
        try:
            data = json.loads(featured.read_text(encoding="utf-8"))
            channels = data.get("channels") if isinstance(data, dict) else None
            if isinstance(channels, dict):
                for row in channels.values():
                    if not isinstance(row, dict):
                        continue
                    for key in ("featured_path", "next_path"):
                        raw = str(row.get(key) or "").strip()
                        if not raw:
                            continue
                        p = Path(raw)
                        try:
                            if p.is_file():
                                out.add(p.resolve())
                        except Exception:  # noqa: BLE001
                            continue
        except Exception:  # noqa: BLE001
            pass
    return out


def _top_public_final_paths(*, top_n: int = 8) -> set[Path]:
    """Top-N public video finals per channel (views) that still exist locally."""
    out: set[Path] = set()
    top_n = max(0, int(top_n or 0))
    if top_n <= 0:
        return out
    views_by_id: dict[str, int] = {}
    channel_by_id: dict[str, str] = {}
    cache_path = OPS_DIR / "vod_views_cache.json"
    if cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cache, dict):
                for vid, v in dict(cache.get("by_video_id") or {}).items():
                    try:
                        views_by_id[str(vid)] = int(v or 0)
                    except (TypeError, ValueError):
                        views_by_id[str(vid)] = 0
                for vid, ch in dict(cache.get("channel_by_video_id") or {}).items():
                    channel_by_id[str(vid)] = str(ch or "").strip().lower()
        except Exception:  # noqa: BLE001
            pass

    # video_id → local final paths
    finals_by_vid: dict[str, list[Path]] = {}
    if JOBS_DIR.is_dir():
        for man in JOBS_DIR.glob("*/publish_manifest.json"):
            try:
                data = json.loads(man.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(data, dict):
                continue
            vid = str(data.get("video_id") or "").strip()
            if not vid:
                continue
            job_dir = man.parent
            candidates = [
                Path(str(data.get("final_path") or "")),
                job_dir / "video" / "final.mp4",
                job_dir / "video_live_head" / "final.mp4",
                job_dir / "video_final_production" / "final.mp4",
            ]
            for p in candidates:
                try:
                    if p.is_file():
                        finals_by_vid.setdefault(vid, []).append(p.resolve())
                except Exception:  # noqa: BLE001
                    continue

    # OpsStore public rows (may lack channel — still protect local final).
    public_vids: list[tuple[str, str, int]] = []
    for job in _load_jobs():
        status = str(job.get("status") or job.get("state") or "").strip().lower()
        if status != "public":
            continue
        vid = str(job.get("video_id") or "").strip()
        if not vid:
            continue
        ch = str(job.get("channel") or channel_by_id.get(vid) or "").strip().lower()
        public_vids.append((ch or "_", vid, int(views_by_id.get(vid) or 0)))
    for vid, ch in channel_by_id.items():
        if vid not in {v for _, v, _ in public_vids}:
            # Listed public uploads — protect if local final exists.
            public_vids.append((ch or "_", vid, int(views_by_id.get(vid) or 0)))

    by_channel: dict[str, list[tuple[int, str]]] = {}
    for ch, vid, views in public_vids:
        by_channel.setdefault(ch or "_", []).append((views, vid))
    for ch, rows in by_channel.items():
        rows.sort(key=lambda t: t[0], reverse=True)
        for _views, vid in rows[:top_n]:
            for fp in finals_by_vid.get(vid) or []:
                out.add(fp)
    return out


def _public_job_final_paths() -> set[Path]:
    """All local finals under jobs.json rows with status=public + video_id."""
    out: set[Path] = set()
    for job in _load_jobs():
        status = str(job.get("status") or job.get("state") or "").strip().lower()
        if status != "public":
            continue
        if not str(job.get("video_id") or "").strip():
            continue
        jdir = _job_dir_path(job.get("job_dir") or job.get("dir"))
        if jdir is None:
            continue
        for rel in (
            "video/final.mp4",
            "video_live_head/final.mp4",
            "video_final_production/final.mp4",
            "final.mp4",
        ):
            fp = jdir / rel
            try:
                if fp.is_file():
                    out.add(fp.resolve())
            except Exception:  # noqa: BLE001
                continue
    return out


def _live_protect_final_paths(policy: dict[str, Any] | None = None) -> set[Path]:
    """Finals that Live may feature — never delete under public purge."""
    policy = policy or load_policy()
    out: set[Path] = set()
    out |= _live_force_lock_paths()
    out |= _playlist_final_paths()
    if bool(policy.get("protect_live_vods", True)):
        out |= _live_vods_final_paths()
    if bool(policy.get("public_purge_keep_live_pool_finals", True)):
        top_n = int(policy.get("live_pool_keep_top_n") or 8)
        out |= _top_public_final_paths(top_n=top_n)
        if bool(policy.get("public_purge_keep_all_public_finals", False)):
            out |= _public_job_final_paths()
    return out


def _path_under_any(path: Path, roots: set[Path]) -> bool:
    try:
        resolved = path.resolve()
    except Exception:  # noqa: BLE001
        return False
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _touches_live_lock(path: Path, locked: set[Path]) -> bool:
    """True if path is a locked final, or a directory containing one."""
    try:
        resolved = path.resolve()
    except Exception:  # noqa: BLE001
        return False
    for lf in locked:
        try:
            lr = lf.resolve()
        except Exception:  # noqa: BLE001
            continue
        if resolved == lr:
            return True
        # Directory that contains a locked file — refuse whole-dir delete
        try:
            if resolved.is_dir() or path.is_dir():
                lr.relative_to(resolved)
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def always_prune_xfade_tmps(
    *,
    dry_run: bool = False,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wipe ``_xfade_tmp`` whenever sibling/job ``final.mp4`` exists.

    Ignores the 85%/8G threshold. Still skips factory paths, protect statuses,
    and active farm ``--out-dir`` trees.
    """
    policy = policy or load_policy()
    dirname = str(policy.get("xfade_tmp_dirname") or "_xfade_tmp")
    now = _utcnow()
    protected = protected_paths(policy)
    deleted: list[dict[str, Any]] = []
    freed = 0

    if not JOBS_DIR.exists() or not bool(policy.get("xfade_always_when_final", True)):
        return {
            "ok": True,
            "bytes_freed": 0,
            "deleted": [],
            "deleted_count": 0,
            "message": "xfade always-prune skipped or no jobs dir",
        }

    status_by_dir: dict[Path, str] = {}
    for job in _load_jobs():
        jdir = _job_dir_path(job.get("job_dir") or job.get("dir"))
        if jdir is None:
            continue
        status_by_dir[jdir] = str(
            job.get("status") or job.get("state") or ""
        ).strip().lower()

    protect_statuses = {
        str(s).strip().lower() for s in (policy.get("protect_statuses") or [])
    }

    for jdir in JOBS_DIR.iterdir():
        if not jdir.is_dir():
            continue
        resolved = _resolve_under_root(jdir)
        if resolved is None or _is_factory_path(resolved):
            continue
        if resolved in protected:
            continue
        status = status_by_dir.get(resolved, "")
        if status in protect_statuses or status == "farming":
            continue
        # Require a finished final somewhere on the job (deterministic + safe).
        if not _has_final_mp4(resolved):
            continue
        for tmp in resolved.rglob(dirname):
            if not tmp.is_dir() or tmp.name != dirname:
                continue
            if _is_factory_path(tmp):
                continue
            if _path_under_any(tmp, protected):
                continue
            # Live force-lock pins final.mp4 for streaming — never blocks xfade wipe.
            # Prefer sibling final; fall back to job-level final already checked.
            sibling = _sibling_final_mp4(tmp)
            if sibling is None and not _has_final_mp4(resolved):
                continue
            age = _dir_age(tmp, now)
            size = _bytes_of(tmp)
            got = _safe_unlink(tmp, dry_run=dry_run)
            if got <= 0 and not dry_run:
                continue
            freed += got if got else size
            deleted.append(
                {
                    "kind": "xfade_tmp_always",
                    "path": str(tmp),
                    "bytes": size,
                    "bytes_freed": got if got else size,
                    "status": status or "untracked_with_final",
                    "sibling_final": str(sibling) if sibling else None,
                    "age_hours": (
                        round(age.total_seconds() / 3600.0, 2) if age else None
                    ),
                    "dry_run": dry_run,
                }
            )
    msg = (
        f"{'DRY-RUN ' if dry_run else ''}"
        f"xfade always-prune actions={len(deleted)} bytes={freed}"
    )
    logger.info("disk_guard: %s", msg)
    return {
        "ok": True,
        "bytes_freed": freed,
        "deleted": deleted[:80],
        "deleted_count": len(deleted),
        "message": msg,
    }


# Thin metadata / ops surfaces retained after public purge.
_PUBLIC_KEEP_SUFFIXES = {".json", ".md", ".txt", ".csv"}


def cleanup_public_job_media(
    job_dir: Path | str,
    *,
    dry_run: bool = False,
    policy: dict[str, Any] | None = None,
    require_public_status: bool = True,
    job_status: str | None = None,
) -> dict[str, Any]:
    """After status=public: delete heavy local media; keep thin manifests.

    Keeps publish/voice/visual/edit manifests, script.json, SOP notes, youtube_meta.
    Deletes ``_xfade_tmp``, scene trees, image banks, voice wavs.
    Keeps ``final.mp4`` when it is a Live pool candidate (playlist head/tail,
    force-lock, top-N public-by-views, or any public+video_id job when policy
    says so). Idempotent; non-blocking on errors. Never touches factory surfaces.
    """
    policy = policy or load_policy()
    result: dict[str, Any] = {
        "ok": True,
        "skipped": False,
        "job_dir": str(job_dir),
        "bytes_freed": 0,
        "deleted": [],
        "kept_live_finals": [],
        "errors": [],
        "message": "",
    }
    if not bool(policy.get("public_purge_enabled", True)):
        result["skipped"] = True
        result["message"] = "public_purge_enabled=false"
        return result

    status = str(job_status or "").strip().lower()
    if require_public_status and status and status != "public":
        result["skipped"] = True
        result["message"] = f"status={status or 'unknown'} (need public)"
        return result
    if require_public_status and not status:
        # Look up jobs.json
        resolved_probe = _job_dir_path(job_dir)
        for job in _load_jobs():
            jdir = _job_dir_path(job.get("job_dir") or job.get("dir"))
            if jdir is None or resolved_probe is None:
                continue
            if jdir == resolved_probe:
                status = str(job.get("status") or job.get("state") or "").strip().lower()
                break
        if status != "public":
            result["skipped"] = True
            result["message"] = f"status={status or 'unknown'} (need public)"
            return result

    jdir = _job_dir_path(job_dir)
    if jdir is None or not jdir.is_dir():
        result["ok"] = False
        result["message"] = f"job_dir missing: {job_dir}"
        return result
    if _is_factory_path(jdir):
        result["ok"] = False
        result["message"] = "refused factory path"
        return result

    protected = protected_paths(policy)
    if jdir in protected:
        result["skipped"] = True
        result["message"] = "job_dir protected (active/farming)"
        return result

    live_protect = _live_protect_final_paths(policy)
    keep_names = {
        str(n).lower()
        for n in (
            policy.get("public_keep_filenames") or policy.get("keep_filenames") or []
        )
    }
    # Default: drop final.mp4 after public — unless Live pool exemption applies.
    if bool(policy.get("public_purge_final_mp4", True)):
        keep_names.discard("final.mp4")
    # Broad Live-pool keep: any public job final (vod_picker needs local media).
    keep_all_public_finals = bool(
        policy.get("public_purge_keep_live_pool_finals", True)
        and policy.get("public_purge_keep_all_public_finals", True)
    )
    if keep_all_public_finals:
        keep_names.add("final.mp4")
    keep_dirs = {
        str(n).lower() for n in (policy.get("public_keep_dirnames") or [])
    }
    purge_dirs = {
        str(n).lower() for n in (policy.get("public_purge_dirnames") or [])
    }
    media_ext = {str(e).lower() for e in (policy.get("media_extensions") or [])}

    deleted: list[dict[str, Any]] = []
    kept_live: list[str] = []
    freed = 0

    def _record(path: Path, kind: str, got: int, planned: int) -> None:
        nonlocal freed
        if got <= 0 and not dry_run:
            return
        entry = {
            "kind": kind,
            "path": str(path),
            "bytes_freed": got if got else planned,
            "dry_run": dry_run,
        }
        deleted.append(entry)
        freed += int(entry["bytes_freed"])

    # 1) Named heavy dirs first (_xfade_tmp, scenes, images, audio, …)
    for child in list(jdir.rglob("*")):
        if not child.is_dir():
            continue
        if child.name.lower() not in purge_dirs:
            continue
        if _is_factory_path(child):
            continue
        if _touches_live_lock(child, live_protect):
            continue
        # Keep voice/visual manifests living inside audio/images — move is hard;
        # delete media files inside instead when dir is audio/images.
        if child.name.lower() in {"audio", "images"}:
            continue
        size = _bytes_of(child)
        try:
            got = _safe_unlink(child, dry_run=dry_run)
            _record(child, "public_purge_dir", got, size)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"{child}: {exc}")

    # 2) Media files (wav/mp4/jpg/…) — skip keep names + keep dir trees + live protect
    for fp in list(jdir.rglob("*")):
        if not fp.is_file():
            continue
        if _is_factory_path(fp):
            continue
        if _touches_live_lock(fp, live_protect):
            if fp.name.lower() == "final.mp4":
                kept_live.append(str(fp))
            continue
        # Preserve thin metadata anywhere
        if fp.name.lower() in keep_names:
            if fp.name.lower() == "final.mp4":
                kept_live.append(str(fp))
            continue
        if fp.suffix.lower() in _PUBLIC_KEEP_SUFFIXES and fp.suffix.lower() not in {
            ".mp4",
            ".mov",
            ".webm",
            ".mkv",
            ".wav",
            ".mp3",
            ".m4a",
        }:
            # Keep .json/.md/.txt even if not in keep_names (SOP notes, etc.)
            if fp.suffix.lower() in {".json", ".md", ".txt"}:
                # Still delete huge non-manifest dumps? Prefer keep all json/md/txt
                continue
        # Skip files under preserved dirnames (script/ops/youtube_meta)
        try:
            rel_parts = fp.relative_to(jdir).parts
        except ValueError:
            continue
        if rel_parts and rel_parts[0].lower() in keep_dirs:
            continue
        if fp.suffix.lower() not in media_ext and fp.name.lower() != "final.mp4":
            continue
        # Live-pool path protect even when keep_names dropped final.mp4
        if fp.name.lower() == "final.mp4" and _touches_live_lock(fp, live_protect):
            kept_live.append(str(fp))
            continue
        size = _bytes_of(fp)
        try:
            got = _safe_unlink(fp, dry_run=dry_run)
            _record(fp, "public_purge_media", got, size)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"{fp}: {exc}")

    # 3) Empty leftover dirs under purge targets (best-effort)
    for name in purge_dirs:
        for d in list(jdir.rglob(name)):
            if not d.is_dir() or _is_factory_path(d):
                continue
            if _touches_live_lock(d, live_protect):
                continue
            try:
                if not any(d.iterdir()):
                    got = _safe_unlink(d, dry_run=dry_run)
                    _record(d, "public_purge_empty_dir", got, 0)
            except Exception:  # noqa: BLE001
                continue

    result["bytes_freed"] = freed
    result["deleted"] = deleted[:120]
    result["deleted_count"] = len(deleted)
    result["kept_live_finals"] = sorted(set(kept_live))[:40]
    result["message"] = (
        f"{'DRY-RUN ' if dry_run else ''}"
        f"public purge {jdir.name} actions={len(deleted)} bytes={freed}"
        + (
            f" kept_live_finals={len(result['kept_live_finals'])}"
            if result["kept_live_finals"]
            else ""
        )
    )
    if result["errors"]:
        result["ok"] = False
    logger.info("disk_guard: %s", result["message"])
    return result


def cleanup_all_public_jobs(
    *,
    dry_run: bool = False,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run ``cleanup_public_job_media`` for every jobs.json row with status=public."""
    policy = policy or load_policy()
    rows: list[dict[str, Any]] = []
    total = 0
    for job in _load_jobs():
        status = str(job.get("status") or job.get("state") or "").strip().lower()
        if status != "public":
            continue
        jdir = _job_dir_path(job.get("job_dir") or job.get("dir"))
        if jdir is None:
            continue
        # Idempotent skip if already purged and no heavy media left
        meta = job.get("meta") if isinstance(job.get("meta"), dict) else {}
        marker = jdir / "ops" / "local_media_purged.json"
        if marker.is_file() and not (jdir / "video" / "final.mp4").exists():
            # Still sweep if _xfade_tmp or wavs remain
            heavy = False
            for pat in ("**/_xfade_tmp", "**/*.wav", "**/scenes", "**/final.mp4"):
                if any(jdir.glob(pat)):
                    heavy = True
                    break
            if not heavy:
                rows.append(
                    {
                        "job_id": job.get("id") or job.get("job_id"),
                        "skipped": True,
                        "message": "already purged",
                    }
                )
                continue
        one = cleanup_public_job_media(
            jdir,
            dry_run=dry_run,
            policy=policy,
            require_public_status=True,
            job_status="public",
        )
        total += int(one.get("bytes_freed") or 0)
        if not dry_run and not one.get("skipped") and one.get("ok", True):
            try:
                ops = jdir / "ops"
                ops.mkdir(parents=True, exist_ok=True)
                (ops / "local_media_purged.json").write_text(
                    json.dumps(
                        {
                            "at": _utcnow_iso(),
                            "bytes_freed": one.get("bytes_freed"),
                            "deleted_count": one.get("deleted_count"),
                            "video_id": job.get("video_id"),
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            except Exception as exc:  # noqa: BLE001
                one.setdefault("errors", []).append(f"marker write: {exc}")
        rows.append(
            {
                "job_id": job.get("id") or job.get("job_id"),
                **{k: one.get(k) for k in ("ok", "skipped", "bytes_freed", "message", "deleted_count")},
            }
        )
    msg = f"public jobs purge n={len(rows)} bytes={total}"
    logger.info("disk_guard: %s", msg)
    return {
        "ok": True,
        "bytes_freed": total,
        "jobs": rows,
        "message": msg,
    }


def _xfade_tmp_candidates(
    policy: dict[str, Any],
    protected: set[Path],
    now: datetime,
) -> list[dict[str, Any]]:
    """Delete ``_xfade_tmp`` on finished (non-farming) jobs — largest historical leak."""
    dirname = str(policy.get("xfade_tmp_dirname") or "_xfade_tmp")
    require_final = bool(policy.get("xfade_require_final_mp4", True))
    min_age = timedelta(hours=float(policy.get("xfade_min_age_hours") or 0))
    terminal = {
        str(s).strip().lower() for s in (policy.get("terminal_statuses") or [])
    }
    protect_statuses = {
        str(s).strip().lower() for s in (policy.get("protect_statuses") or [])
    }

    # Map job_dir → status from jobs.json
    status_by_dir: dict[Path, str] = {}
    for job in _load_jobs():
        jdir = _job_dir_path(job.get("job_dir") or job.get("dir"))
        if jdir is None:
            continue
        status_by_dir[jdir] = str(
            job.get("status") or job.get("state") or ""
        ).strip().lower()

    cands: list[dict[str, Any]] = []
    if not JOBS_DIR.exists():
        return cands
    for jdir in JOBS_DIR.iterdir():
        if not jdir.is_dir():
            continue
        resolved = _resolve_under_root(jdir)
        if resolved is None or _is_factory_path(resolved):
            continue
        if resolved in protected:
            continue
        status = status_by_dir.get(resolved, "")
        if status in protect_statuses or status == "farming":
            continue
        # Prefer known terminal; also allow dirs with final.mp4 even if untracked
        if status and status not in terminal and not _has_final_mp4(resolved):
            continue
        if require_final and not _has_final_mp4(resolved) and status not in terminal:
            continue
        if require_final and status in terminal and not _has_final_mp4(resolved):
            # Still allow xfade wipe on failed/private/public even without final
            pass
        for tmp in resolved.rglob(dirname):
            if not tmp.is_dir():
                continue
            if tmp.name != dirname:
                continue
            if _is_factory_path(tmp):
                continue
            # Never touch xfade under a protected job dir
            skip = False
            for pdir in protected:
                try:
                    tmp.resolve().relative_to(pdir)
                    skip = True
                    break
                except Exception:  # noqa: BLE001
                    continue
            if skip:
                continue
            age = _dir_age(tmp, now)
            if age is None or age < min_age:
                continue
            cands.append(
                {
                    "kind": "xfade_tmp",
                    "path": str(tmp),
                    "bytes": _bytes_of(tmp),
                    "status": status or "untracked_with_final",
                    "age_hours": round(age.total_seconds() / 3600.0, 2),
                }
            )
    return cands


def _terminal_job_candidates(
    policy: dict[str, Any],
    protected: set[Path],
    now: datetime,
) -> list[dict[str, Any]]:
    terminal = {
        str(s).strip().lower() for s in (policy.get("terminal_statuses") or [])
    }
    media_min = timedelta(hours=float(policy.get("media_min_age_hours") or 12))
    whole_min = timedelta(days=float(policy.get("whole_job_min_age_days") or 3))
    keep_names = {str(n).lower() for n in (policy.get("keep_filenames") or [])}
    media_ext = {str(e).lower() for e in (policy.get("media_extensions") or [])}
    xfade_name = str(policy.get("xfade_tmp_dirname") or "_xfade_tmp")

    cands: list[dict[str, Any]] = []
    for job in _load_jobs():
        status = str(job.get("status") or job.get("state") or "").strip().lower()
        if status not in terminal:
            continue
        jdir = _job_dir_path(job.get("job_dir") or job.get("dir"))
        if jdir is None or not jdir.exists() or not jdir.is_dir():
            continue
        if jdir in protected:
            continue
        if _is_factory_path(jdir):
            continue
        age = _dir_age(jdir, now)
        if age is None:
            continue
        updated = _parse_ts(job.get("updated_at")) or (
            now - age
        )
        job_age = now - updated

        # Whole-folder deletion for old terminal jobs.
        if job_age >= whole_min and age >= whole_min:
            cands.append(
                {
                    "kind": "job_dir",
                    "path": str(jdir),
                    "bytes": _bytes_of(jdir),
                    "job_id": job.get("id") or job.get("job_id"),
                    "status": status,
                    "age_hours": round(job_age.total_seconds() / 3600.0, 2),
                }
            )
            continue

        # Media-only inside younger-but-old-enough terminal dirs.
        if job_age < media_min:
            continue
        for fp in jdir.rglob("*"):
            if not fp.is_file():
                continue
            if _is_factory_path(fp):
                continue
            # Skip files inside _xfade_tmp (handled as whole-dir deletes)
            if any(part == xfade_name for part in fp.parts):
                continue
            if fp.name.lower() in keep_names:
                continue
            if fp.suffix.lower() not in media_ext:
                continue
            try:
                f_age = now - datetime.fromtimestamp(
                    fp.stat().st_mtime, tz=timezone.utc
                )
            except OSError:
                continue
            if f_age < media_min:
                continue
            cands.append(
                {
                    "kind": "media",
                    "path": str(fp),
                    "bytes": _bytes_of(fp),
                    "job_id": job.get("id") or job.get("job_id"),
                    "status": status,
                    "age_hours": round(f_age.total_seconds() / 3600.0, 2),
                }
            )
    return cands


def _orphan_job_dir_candidates(
    policy: dict[str, Any],
    protected: set[Path],
    known_dirs: set[Path],
    now: datetime,
) -> list[dict[str, Any]]:
    if not JOBS_DIR.exists():
        return []
    min_age = timedelta(days=float(policy.get("orphan_job_dir_min_age_days") or 2))
    cands: list[dict[str, Any]] = []
    for child in JOBS_DIR.iterdir():
        if not child.is_dir():
            # stray logs under jobs/
            if child.is_file() and child.suffix.lower() == ".log":
                age = _dir_age(child, now)
                if age is not None and age >= min_age and not _is_factory_path(child):
                    cands.append(
                        {
                            "kind": "orphan_file",
                            "path": str(child),
                            "bytes": _bytes_of(child),
                            "age_hours": round(age.total_seconds() / 3600.0, 2),
                        }
                    )
            continue
        resolved = _resolve_under_root(child)
        if resolved is None or _is_factory_path(resolved):
            continue
        if resolved in protected or resolved in known_dirs:
            continue
        age = _dir_age(resolved, now)
        if age is None or age < min_age:
            continue
        cands.append(
            {
                "kind": "orphan_job_dir",
                "path": str(resolved),
                "bytes": _bytes_of(resolved),
                "age_hours": round(age.total_seconds() / 3600.0, 2),
            }
        )
    return cands


def _log_candidates(policy: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    if not OPS_DIR.exists():
        return []
    min_age = timedelta(days=float(policy.get("log_min_age_days") or 3))
    trunc_at = int(policy.get("log_truncate_bytes") or 2_000_000)
    cands: list[dict[str, Any]] = []
    for fp in OPS_DIR.glob("*.log"):
        if _is_factory_path(fp):
            continue
        try:
            st = fp.stat()
        except OSError:
            continue
        age = now - datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        if age < min_age:
            continue
        if st.st_size <= trunc_at:
            # old small logs → delete
            cands.append(
                {
                    "kind": "log_delete",
                    "path": str(fp),
                    "bytes": int(st.st_size),
                    "age_hours": round(age.total_seconds() / 3600.0, 2),
                }
            )
        else:
            # large → truncate (keep last trunc_at bytes)
            cands.append(
                {
                    "kind": "log_truncate",
                    "path": str(fp),
                    "bytes": max(0, int(st.st_size) - trunc_at),
                    "keep_bytes": trunc_at,
                    "age_hours": round(age.total_seconds() / 3600.0, 2),
                }
            )
    return cands


def _truncate_log(path: Path, keep_bytes: int, *, dry_run: bool) -> int:
    if _is_factory_path(path):
        return 0
    try:
        size = int(path.stat().st_size)
    except OSError:
        return 0
    freeable = max(0, size - keep_bytes)
    if freeable <= 0:
        return 0
    if dry_run:
        return freeable
    try:
        with path.open("rb") as fh:
            fh.seek(-keep_bytes, os.SEEK_END)
            tail = fh.read()
        path.write_bytes(tail)
        return freeable
    except OSError as exc:
        logger.warning("disk_guard truncate failed %s: %s", path, exc)
        return 0


def _run_cmd(cmd: list[str], *, timeout: int = 120) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        ok = proc.returncode == 0
        msg = ((proc.stdout or "") + (proc.stderr or "")).strip()[:400]
        return ok, msg or f"rc={proc.returncode}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:300]


def _sudo_available() -> bool:
    ok, _ = _run_cmd(["sudo", "-n", "true"], timeout=10)
    return ok


def _docker_idle() -> bool:
    ok, out = _run_cmd(["docker", "ps", "-q"], timeout=20)
    if not ok:
        return False
    return not bool(out.strip())


def _system_hygiene(
    policy: dict[str, Any],
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if policy.get("allow_journal_vacuum", True) and _sudo_available():
        max_size = str(policy.get("journal_vacuum_max_size") or "200M")
        entry = {
            "kind": "journal_vacuum",
            "cmd": f"sudo journalctl --vacuum-size={max_size}",
            "dry_run": dry_run,
        }
        if dry_run:
            entry["ok"] = True
            entry["skipped"] = "dry_run"
        else:
            ok, msg = _run_cmd(
                ["sudo", "-n", "journalctl", f"--vacuum-size={max_size}"],
                timeout=180,
            )
            entry["ok"] = ok
            entry["message"] = msg
        actions.append(entry)

    if policy.get("allow_apt_clean", True) and _sudo_available():
        entry = {
            "kind": "apt_clean",
            "cmd": "sudo apt-get clean",
            "dry_run": dry_run,
        }
        if dry_run:
            entry["ok"] = True
            entry["skipped"] = "dry_run"
        else:
            ok, msg = _run_cmd(["sudo", "-n", "apt-get", "clean"], timeout=180)
            entry["ok"] = ok
            entry["message"] = msg
        actions.append(entry)

    if policy.get("allow_docker_prune", True):
        idle_only = bool(policy.get("docker_prune_only_if_idle", True))
        idle = _docker_idle()
        entry: dict[str, Any] = {
            "kind": "docker_prune",
            "cmd": "docker system prune -f",
            "docker_idle": idle,
            "dry_run": dry_run,
        }
        if idle_only and not idle:
            entry["ok"] = False
            entry["skipped"] = "docker_not_idle"
        elif dry_run:
            entry["ok"] = True
            entry["skipped"] = "dry_run"
        else:
            ok, msg = _run_cmd(["docker", "system", "prune", "-f"], timeout=180)
            entry["ok"] = ok
            entry["message"] = msg
        actions.append(entry)
    return actions


def _write_last(payload: dict[str, Any]) -> None:
    try:
        LAST_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_PATH.write_text(
            json.dumps(payload, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("disk_guard last write failed: %s", exc)


def maybe_run_disk_guard(
    *,
    dry_run: bool = False,
    force: bool = False,
    policy_path: Path | None = None,
) -> dict[str, Any]:
    """Compulsory disk cleanup when threshold crossed (or ``force``).

    Called every */10 ``run_watchdog_tick`` and from sleep_factory backup.
    Emergency brake: env ``DISK_GUARD=0`` only (policy ``enabled`` is ignored
    when ``compulsory`` is true — default).
    """
    policy = load_policy(policy_path)
    mount = str(policy.get("mount") or "/")
    before = disk_snapshot(mount)
    compulsory = bool(policy.get("compulsory", True))
    result: dict[str, Any] = {
        "at": _utcnow_iso(),
        "dry_run": dry_run,
        "force": force,
        "policy_path": str(policy_path or POLICY_PATH),
        "compulsory": compulsory,
        "enabled": True,
        "triggered": False,
        "before": before,
        "after": before,
        "bytes_freed": 0,
        "deleted": [],
        "hygiene": [],
        "protected_dirs": [],
        "message": "",
    }

    if _disk_guard_env_disabled():
        result["enabled"] = False
        result["message"] = "disk_guard disabled via DISK_GUARD=0"
        _write_last(result)
        return result

    # Policy enabled=false is ignored when compulsory (FACTORY_ALWAYS_ON path).
    if not compulsory and not policy.get("enabled", True):
        result["enabled"] = False
        result["message"] = "disk_guard disabled via policy.enabled=false"
        _write_last(result)
        return result

    crossed = threshold_crossed(before, policy)
    result["triggered"] = bool(crossed or force)

    # Always-on safe wipes (ignore 85%/8G gate): leftover _xfade_tmp next to
    # final.mp4, plus heavy media on jobs already status=public.
    always_xfade = always_prune_xfade_tmps(dry_run=dry_run, policy=policy)
    always_public = cleanup_all_public_jobs(dry_run=dry_run, policy=policy)
    always_freed = int(always_xfade.get("bytes_freed") or 0) + int(
        always_public.get("bytes_freed") or 0
    )
    always_deleted = list(always_xfade.get("deleted") or []) + [
        {**j, "kind": "public_job_summary"}
        for j in (always_public.get("jobs") or [])
        if not j.get("skipped")
    ]
    result["xfade_always"] = {
        "bytes_freed": always_xfade.get("bytes_freed"),
        "deleted_count": always_xfade.get("deleted_count"),
        "message": always_xfade.get("message"),
    }
    result["public_purge"] = {
        "bytes_freed": always_public.get("bytes_freed"),
        "jobs": always_public.get("jobs"),
        "message": always_public.get("message"),
    }

    if not crossed and not force:
        after = disk_snapshot(mount)
        measured = max(0, int(before["used_bytes"]) - int(after["used_bytes"]))
        result.update(
            {
                "after": after,
                "bytes_freed": measured if not dry_run else always_freed,
                "bytes_freed_planned": always_freed,
                "deleted": always_deleted[:80],
                "deleted_count": len(always_deleted),
                "message": (
                    f"{'DRY-RUN ' if dry_run else ''}"
                    f"ok used={before['used_percent']}%→{after['used_percent']}% "
                    f"free={before['free_gb']}G→{after['free_gb']}G "
                    f"(below trigger; always-on xfade+public "
                    f"freed_planned={always_freed})"
                ),
            }
        )
        _write_last(result)
        return result

    now = _utcnow()
    protected = protected_paths(policy)
    result["protected_dirs"] = [str(p) for p in sorted(protected, key=str)]

    known_dirs: set[Path] = set()
    for job in _load_jobs():
        jdir = _job_dir_path(job.get("job_dir") or job.get("dir"))
        if jdir is not None:
            known_dirs.add(jdir)

    # xfade first (historically multi-GB), then media/orphans/logs.
    # always_prune already wiped finals-present xfade; candidates may be empty.
    xfade = _xfade_tmp_candidates(policy, protected, now)
    rest = (
        _terminal_job_candidates(policy, protected, now)
        + _orphan_job_dir_candidates(policy, protected, known_dirs, now)
        + _log_candidates(policy, now)
    )
    xfade.sort(key=lambda c: int(c.get("bytes") or 0), reverse=True)
    rest.sort(key=lambda c: int(c.get("bytes") or 0), reverse=True)
    candidates = xfade + rest

    max_actions = int(policy.get("max_delete_actions") or 400)
    deleted: list[dict[str, Any]] = list(always_deleted)
    freed = always_freed

    for cand in candidates:
        snap = disk_snapshot(mount)
        if target_reached(snap, policy) and not force:
            break
        if len(deleted) - len(always_deleted) >= max_actions:
            break
        kind = str(cand.get("kind") or "")
        path = Path(str(cand["path"]))
        if _is_factory_path(path):
            continue
        # Protect if path is under a protected dir
        skip = False
        for pdir in protected:
            try:
                path.resolve().relative_to(pdir)
                skip = True
                break
            except Exception:  # noqa: BLE001
                continue
        if skip:
            continue

        got = 0
        if kind == "log_truncate":
            got = _truncate_log(
                path,
                int(cand.get("keep_bytes") or policy.get("log_truncate_bytes") or 0),
                dry_run=dry_run,
            )
        else:
            got = _safe_unlink(path, dry_run=dry_run)
        if got <= 0 and not dry_run:
            continue
        entry = dict(cand)
        entry["bytes_freed"] = got if got else int(cand.get("bytes") or 0)
        entry["dry_run"] = dry_run
        deleted.append(entry)
        freed += int(entry["bytes_freed"])

    hygiene = _system_hygiene(policy, dry_run=dry_run)
    after = disk_snapshot(mount)
    measured = max(0, int(before["used_bytes"]) - int(after["used_bytes"]))
    result.update(
        {
            "after": after,
            "bytes_freed": measured if not dry_run else freed,
            "bytes_freed_planned": freed,
            "deleted": deleted[:80],
            "deleted_count": len(deleted),
            "hygiene": hygiene,
            "message": (
                f"{'DRY-RUN ' if dry_run else ''}"
                f"disk_guard used {before['used_percent']}%→{after['used_percent']}% "
                f"free {before['free_gb']}G→{after['free_gb']}G "
                f"actions={len(deleted)} planned_free_bytes={freed}"
            ),
        }
    )
    _write_last(result)
    logger.info("disk_guard: %s", result["message"])
    return result


# Alias used by watchdog / sleep_factory
run_disk_guard = maybe_run_disk_guard
