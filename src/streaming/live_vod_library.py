"""Dedicated Live VOD library — downloaded public uploads, independent of farm jobs.

Layout::

    output/live_vods/<channel>/<video_id>/final.mp4
    output/live_vods/<channel>/<video_id>/meta.json

Live ffmpeg playlists should prefer these paths so post-public farm purge can
delete ``output/jobs/.../final.mp4`` without blanking encode.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "output" / "ops"
LIVE_VODS_ROOT = ROOT / "output" / "live_vods"
LIBRARY_STATUS = OPS / "live_vod_library_status.json"

CHANNELS = ("napstorian", "napping_historian")

DEFAULT_TOP_N = 3  # 1 featured + up to 2 backups
DEFAULT_MIN_BYTES = 2_000_000
DEFAULT_MIN_DURATION_SEC = 180.0
DEFAULT_SLEEP_BETWEEN_SEC = 3.0
DEFAULT_CANDIDATE_SCAN = 24  # rank more publics than top_n to skip shorts


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_smm_settings() -> dict[str, Any]:
    path = ROOT / "config" / "agents_settings.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    smm = data.get("smm") if isinstance(data, dict) else None
    return smm if isinstance(smm, dict) else {}


def library_enabled() -> bool:
    raw = (os.getenv("LIVE_VOD_LIBRARY") or "1").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    smm = _load_smm_settings()
    if "live_vod_library_enabled" in smm:
        return bool(smm.get("live_vod_library_enabled"))
    return True


def default_top_n() -> int:
    raw = (os.getenv("LIVE_VOD_LIBRARY_TOP_N") or "").strip()
    if raw:
        try:
            return max(1, min(8, int(raw)))
        except (TypeError, ValueError):
            pass
    smm = _load_smm_settings()
    if smm.get("live_vod_library_top_n") is not None:
        try:
            return max(1, min(8, int(smm["live_vod_library_top_n"])))
        except (TypeError, ValueError):
            pass
    return DEFAULT_TOP_N


def min_library_bytes() -> int:
    raw = (os.getenv("LIVE_VOD_LIBRARY_MIN_BYTES") or "").strip()
    if raw:
        try:
            return max(100_000, int(raw))
        except (TypeError, ValueError):
            pass
    return DEFAULT_MIN_BYTES


def min_library_duration_sec() -> float:
    raw = (
        os.getenv("LIVE_VOD_LIBRARY_MIN_DURATION_SEC")
        or os.getenv("VOD_PICKER_MIN_DURATION_SEC")
        or ""
    ).strip()
    if raw:
        try:
            return max(30.0, float(raw))
        except (TypeError, ValueError):
            pass
    return DEFAULT_MIN_DURATION_SEC


def live_vods_root() -> Path:
    return LIVE_VODS_ROOT


def live_vod_dir(channel: str, video_id: str) -> Path:
    ch = (channel or "").strip().lower()
    vid = (video_id or "").strip()
    return LIVE_VODS_ROOT / ch / vid


def live_vod_final_path(channel: str, video_id: str) -> Path:
    return live_vod_dir(channel, video_id) / "final.mp4"


def live_vod_meta_path(channel: str, video_id: str) -> Path:
    return live_vod_dir(channel, video_id) / "meta.json"


def parse_live_vod_path(path: Path | str) -> tuple[str, str] | None:
    """Return ``(channel, video_id)`` when path is under live_vods, else None."""
    try:
        parts = Path(path).resolve().parts
    except OSError:
        parts = Path(str(path)).parts
    if "live_vods" not in parts:
        return None
    idx = parts.index("live_vods")
    if idx + 2 >= len(parts):
        return None
    ch = str(parts[idx + 1]).strip().lower()
    vid = str(parts[idx + 2]).strip()
    if ch not in CHANNELS or not vid:
        return None
    return ch, vid


def is_live_vods_path(path: Path | str) -> bool:
    return parse_live_vod_path(path) is not None


def library_final_ok(
    path: Path | str,
    *,
    min_bytes: int | None = None,
) -> bool:
    p = Path(path)
    if not p.is_file():
        return False
    floor = min_library_bytes() if min_bytes is None else int(min_bytes)
    try:
        return p.stat().st_size >= floor
    except OSError:
        return False


def _ffprobe_duration(path: Path) -> float:
    try:
        from src.streaming.vod_picker import _ffprobe_duration as probe

        return float(probe(path) or 0.0)
    except Exception:  # noqa: BLE001
        pass
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return float((r.stdout or "").strip() or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_library_meta(
    channel: str,
    video_id: str,
    *,
    title: str = "",
    views: int = 0,
    avd_pct: float | None = None,
    rank_metric: float = 0.0,
    duration_sec: float | None = None,
    source_url: str = "",
    extra: dict[str, Any] | None = None,
) -> Path:
    dest = live_vod_meta_path(channel, video_id)
    payload: dict[str, Any] = {
        "module": "live_vod_library",
        "channel": channel,
        "video_id": video_id,
        "title": title,
        "description": "",
        "views": int(views or 0),
        "avd_pct": avd_pct,
        "rank_metric": float(rank_metric or 0.0),
        "duration_sec": duration_sec,
        "source_url": source_url
        or f"https://www.youtube.com/watch?v={video_id}",
        "updated_at": _utc_now(),
        "final_path": str(live_vod_final_path(channel, video_id)),
    }
    if extra:
        payload.update(extra)
    # Preserve prior description/title if richer.
    if dest.is_file():
        try:
            prev = json.loads(dest.read_text(encoding="utf-8"))
            if isinstance(prev, dict):
                if not payload.get("title") and prev.get("title"):
                    payload["title"] = prev["title"]
                if prev.get("description") and not payload.get("description"):
                    payload["description"] = prev["description"]
        except (OSError, json.JSONDecodeError):
            pass
    _atomic_write_json(dest, payload)
    # publish_manifest shim so live_broadcast_meta / vod_picker index work.
    pub = live_vod_dir(channel, video_id) / "publish_manifest.json"
    _atomic_write_json(
        pub,
        {
            "video_id": video_id,
            "channel": channel,
            "title": payload.get("title") or "",
            "description": payload.get("description") or "",
            "final_path": payload.get("final_path"),
            "source": "live_vod_library",
        },
    )
    return dest


def discover_library_finals(
    *,
    channel: str | None = None,
    min_bytes: int | None = None,
) -> list[Path]:
    """List OK ``final.mp4`` files under the Live library."""
    root = LIVE_VODS_ROOT
    if not root.is_dir():
        return []
    floor = min_library_bytes() if min_bytes is None else int(min_bytes)
    out: list[Path] = []
    ch_filter = (channel or "").strip().lower() or None
    for ch_dir in sorted(root.iterdir()):
        if not ch_dir.is_dir():
            continue
        ch = ch_dir.name.strip().lower()
        if ch not in CHANNELS:
            continue
        if ch_filter and ch != ch_filter:
            continue
        for vid_dir in sorted(ch_dir.iterdir()):
            if not vid_dir.is_dir():
                continue
            final = vid_dir / "final.mp4"
            if library_final_ok(final, min_bytes=floor):
                out.append(final.resolve())
    return out


def rank_top_publics_for_library(
    channel: str,
    *,
    limit: int | None = None,
    candidate_scan: int | None = None,
) -> list[dict[str, Any]]:
    """Rank channel public video_ids by views×AVD% (views fallback) for download.

    Does not require a local farm final — used to decide what to fetch into
    ``output/live_vods``.
    """
    from src.streaming.vod_picker import (
        _is_live_stream_title,
        _load_avd_by_video_id,
        _load_public_video_ids,
        _load_views_cache,
        live_rank_metric,
    )

    ch = (channel or "").strip().lower()
    if ch not in CHANNELS:
        return []
    lim = default_top_n() if limit is None else max(1, int(limit))
    scan = (
        DEFAULT_CANDIDATE_SCAN
        if candidate_scan is None
        else max(lim, int(candidate_scan))
    )
    cache = _load_views_cache()
    avd_by = _load_avd_by_video_id()
    pubs = _load_public_video_ids(views_cache=cache)
    by_id = dict(cache.get("by_video_id") or {})
    titles = dict(cache.get("titles_by_id") or {})
    rows: list[dict[str, Any]] = []
    for vid in pubs.get(ch) or set():
        title = str(titles.get(vid) or "")
        if _is_live_stream_title(title):
            continue
        try:
            views = int(by_id.get(vid) or 0)
        except (TypeError, ValueError):
            views = 0
        avd_row = avd_by.get(vid) or {}
        avd = avd_row.get("avd_pct")
        try:
            avd_f = float(avd) if avd is not None else None
        except (TypeError, ValueError):
            avd_f = None
        metric = live_rank_metric(views, avd_f)
        rows.append(
            {
                "video_id": vid,
                "channel": ch,
                "views": views,
                "avd_pct": avd_f,
                "avd_src": str(avd_row.get("source") or "none"),
                "rank_metric": metric,
                "title": title,
                "library_path": str(live_vod_final_path(ch, vid)),
                "library_ok": library_final_ok(live_vod_final_path(ch, vid)),
            }
        )
    rows.sort(
        key=lambda r: (float(r.get("rank_metric") or 0.0), int(r.get("views") or 0)),
        reverse=True,
    )
    return rows[:scan] if scan else rows[:lim]


def _cookies_arg() -> list[str]:
    """Optional Netscape cookies for yt-dlp (bot checks)."""
    for key in ("LIVE_VOD_YT_COOKIES", "YT_DLP_COOKIES", "YOUTUBE_COOKIES"):
        raw = (os.getenv(key) or "").strip()
        if not raw:
            continue
        p = Path(raw)
        if not p.is_absolute():
            p = ROOT / p
        if p.is_file():
            return ["--cookies", str(p)]
    return []


def find_farm_final_for_video_id(video_id: str) -> Path | None:
    """Best existing farm ``final.mp4`` for a published video_id (if any)."""
    vid = (video_id or "").strip()
    if not vid:
        return None
    try:
        from src.streaming.vod_picker import _load_publish_index

        idx = _load_publish_index()
    except Exception:  # noqa: BLE001
        return None
    hits: list[Path] = []
    for path, meta in idx.items():
        if str(meta.get("video_id") or "").strip() != vid:
            continue
        if "/live_vods/" in str(path).replace("\\", "/"):
            continue
        p = Path(path)
        if library_final_ok(p):
            hits.append(p)
    if not hits:
        return None
    # Prefer video/final.mp4 over live_head variants when sizes similar.
    hits.sort(
        key=lambda p: (
            1 if "video_live_head" in p.as_posix() else 0,
            -p.stat().st_size,
        )
    )
    return hits[0]


def copy_farm_final_into_library(
    channel: str,
    video_id: str,
    source: Path,
    *,
    title: str = "",
    views: int = 0,
    avd_pct: float | None = None,
    rank_metric: float = 0.0,
) -> dict[str, Any]:
    """Copy/hardlink a farm final into ``output/live_vods`` (purge-safe)."""
    import shutil

    ch = (channel or "").strip().lower()
    vid = (video_id or "").strip()
    dest = live_vod_final_path(ch, vid)
    result: dict[str, Any] = {
        "ok": False,
        "channel": ch,
        "video_id": vid,
        "action": None,
        "path": str(dest),
        "source": str(source),
    }
    if ch not in CHANNELS or not vid:
        result["error"] = "bad_channel_or_video_id"
        return result
    if not library_final_ok(source):
        result["error"] = "source_missing_or_small"
        return result
    if library_final_ok(dest):
        write_library_meta(
            ch,
            vid,
            title=title,
            views=views,
            avd_pct=avd_pct,
            rank_metric=rank_metric,
            duration_sec=_ffprobe_duration(dest) or None,
            extra={"seed_src": "farm_copy_existing"},
        )
        result["ok"] = True
        result["action"] = "skipped_exists"
        result["size_bytes"] = dest.stat().st_size
        return result

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{vid}.copy.mp4"
    try:
        try:
            if tmp.exists():
                tmp.unlink()
            os.link(source, tmp)
        except OSError:
            shutil.copy2(source, tmp)
        tmp.replace(dest)
    except OSError as exc:
        result["error"] = f"copy_failed:{exc}"
        tmp.unlink(missing_ok=True)
        return result

    # Copy thin packaging beside media when present.
    src_dir = source.parent
    for rel in (
        "publish_manifest.json",
        "../publish_manifest.json",
        "../../publish_manifest.json",
    ):
        cand = (src_dir / rel).resolve() if ".." in rel else src_dir / rel
        # Walk up to job dir for publish_manifest.
        break
    job_dir = None
    for parent in source.resolve().parents:
        if (parent / "publish_manifest.json").is_file() or parent.name.startswith(
            "20"
        ):
            if (parent / "publish_manifest.json").is_file():
                job_dir = parent
                break
    if job_dir is not None:
        pub = job_dir / "publish_manifest.json"
        if pub.is_file() and not title:
            try:
                data = json.loads(pub.read_text(encoding="utf-8"))
                title = str(data.get("title") or title)
                if not views:
                    pass
            except (OSError, json.JSONDecodeError):
                pass
        # Best-effort thumb copy into library dir.
        for thumb_rel in (
            "youtube_meta/thumbnail.jpg",
            "youtube_meta/thumbnail.png",
            "thumbnails/selected.jpg",
            "thumbnails/thumbnail.jpg",
        ):
            tp = job_dir / thumb_rel
            if tp.is_file() and tp.stat().st_size >= 1024:
                try:
                    shutil.copy2(tp, dest.parent / tp.name)
                except OSError:
                    pass
                break

    dur = _ffprobe_duration(dest)
    write_library_meta(
        ch,
        vid,
        title=title,
        views=views,
        avd_pct=avd_pct,
        rank_metric=rank_metric,
        duration_sec=dur if dur > 0 else None,
        extra={"seed_src": "farm_copy", "farm_source": str(source)},
    )
    result["ok"] = True
    result["action"] = "copied_from_farm"
    result["duration_sec"] = dur if dur > 0 else None
    result["size_bytes"] = dest.stat().st_size
    result["title"] = title
    return result


def _is_bot_block_error(err: str) -> bool:
    low = (err or "").lower()
    return (
        "sign in to confirm" in low
        or "not a bot" in low
        or "cookies-from-browser" in low
        or "confirm you're not a bot" in low
        or "confirm you’re not a bot" in low
    )


def _yt_dlp_bin() -> str:
    venv = ROOT / ".venv" / "bin" / "yt-dlp"
    if venv.is_file():
        return str(venv)
    return "yt-dlp"


def probe_youtube_duration(
    video_id: str,
    *,
    timeout_sec: float = 90.0,
) -> dict[str, Any]:
    """Lightweight yt-dlp metadata probe (no media download)."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        _yt_dlp_bin(),
        *_cookies_arg(),
        "--skip-download",
        "--print",
        "%(duration)s\t%(title)s\t%(description)s",
        "--no-warnings",
        url,
    ]
    out: dict[str, Any] = {
        "ok": False,
        "video_id": video_id,
        "duration_sec": None,
        "title": "",
        "description": "",
    }
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_sec
        )
    except FileNotFoundError:
        out["error"] = "yt-dlp_not_found"
        return out
    except subprocess.TimeoutExpired:
        out["error"] = "yt-dlp_probe_timeout"
        return out
    if r.returncode != 0:
        out["error"] = (r.stderr or r.stdout or "probe_failed")[-400:]
        return out
    line = (r.stdout or "").splitlines()[0] if (r.stdout or "").strip() else ""
    parts = line.split("\t", 2)
    try:
        dur = float(parts[0]) if parts and parts[0] not in {"NA", "None", ""} else None
    except (TypeError, ValueError):
        dur = None
    out["duration_sec"] = dur
    out["title"] = parts[1] if len(parts) > 1 else ""
    out["description"] = parts[2] if len(parts) > 2 else ""
    out["ok"] = True
    return out


def download_public_vod(
    channel: str,
    video_id: str,
    *,
    title: str = "",
    views: int = 0,
    avd_pct: float | None = None,
    rank_metric: float = 0.0,
    min_duration_sec: float | None = None,
    min_bytes: int | None = None,
    force: bool = False,
    sleep_after_sec: float = 0.0,
) -> dict[str, Any]:
    """Download one public watch URL into the Live library (idempotent)."""
    ch = (channel or "").strip().lower()
    vid = (video_id or "").strip()
    result: dict[str, Any] = {
        "ok": False,
        "channel": ch,
        "video_id": vid,
        "action": None,
        "path": str(live_vod_final_path(ch, vid)),
    }
    if ch not in CHANNELS or not vid:
        result["error"] = "bad_channel_or_video_id"
        return result

    dest = live_vod_final_path(ch, vid)
    dest_dir = dest.parent
    floor = min_library_bytes() if min_bytes is None else int(min_bytes)
    min_d = (
        min_library_duration_sec()
        if min_duration_sec is None
        else float(min_duration_sec)
    )

    if not force and library_final_ok(dest, min_bytes=floor):
        dur = _ffprobe_duration(dest)
        if dur >= min_d or dur <= 0:
            # dur<=0: trust size gate (probe flaky); keep file.
            write_library_meta(
                ch,
                vid,
                title=title,
                views=views,
                avd_pct=avd_pct,
                rank_metric=rank_metric,
                duration_sec=dur if dur > 0 else None,
            )
            result["ok"] = True
            result["action"] = "skipped_exists"
            result["duration_sec"] = dur if dur > 0 else None
            result["size_bytes"] = dest.stat().st_size
            return result
        result["action"] = "replace_too_short"
        result["prior_duration_sec"] = dur

    # Prefer copying an existing farm final (no YouTube bot check).
    farm = find_farm_final_for_video_id(vid)
    if farm is not None:
        dur = _ffprobe_duration(farm)
        if dur > 0 and dur < min_d:
            result["action"] = "skipped_short"
            result["duration_sec"] = dur
            result["error"] = f"farm_duration_sec={dur} < min={min_d}"
            return result
        copied = copy_farm_final_into_library(
            ch,
            vid,
            farm,
            title=title,
            views=views,
            avd_pct=avd_pct,
            rank_metric=rank_metric,
        )
        if copied.get("ok"):
            if sleep_after_sec > 0:
                time.sleep(float(sleep_after_sec))
            return copied

    # Probe duration before full download to skip Shorts cheaply.
    probe = probe_youtube_duration(vid)
    if probe.get("ok") and probe.get("duration_sec") is not None:
        try:
            pd = float(probe["duration_sec"])
        except (TypeError, ValueError):
            pd = None
        if pd is not None and pd < min_d:
            result["action"] = "skipped_short"
            result["duration_sec"] = pd
            result["title"] = probe.get("title") or title
            result["error"] = f"duration_sec={pd} < min={min_d}"
            return result
        if not title and probe.get("title"):
            title = str(probe["title"])
    elif not probe.get("ok"):
        err = str(probe.get("error") or "")
        if _is_bot_block_error(err):
            result["action"] = "bot_blocked"
            result["error"] = err[-600:]
            return result
        logger.info(
            "live_vod probe failed %s: %s — attempting download anyway",
            vid,
            err,
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp_out = dest_dir / f".{vid}.download.mp4"
    if tmp_out.exists():
        tmp_out.unlink(missing_ok=True)

    url = f"https://www.youtube.com/watch?v={vid}"
    cmd = [
        _yt_dlp_bin(),
        *_cookies_arg(),
        "-f",
        "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "--merge-output-format",
        "mp4",
        "--no-playlist",
        "--retries",
        "3",
        "--fragment-retries",
        "3",
        "--sleep-requests",
        "1",
        "--sleep-interval",
        "1",
        "--max-sleep-interval",
        "4",
        "-o",
        str(tmp_out),
        url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except FileNotFoundError:
        result["error"] = "yt-dlp_not_found"
        result["action"] = "failed"
        return result
    except subprocess.TimeoutExpired:
        result["error"] = "yt-dlp_timeout"
        result["action"] = "failed"
        tmp_out.unlink(missing_ok=True)
        return result

    if r.returncode != 0 or not tmp_out.is_file():
        err = (r.stderr or r.stdout or "download_failed")[-600:]
        result["action"] = "bot_blocked" if _is_bot_block_error(err) else "failed"
        result["error"] = err
        tmp_out.unlink(missing_ok=True)
        return result

    try:
        size = tmp_out.stat().st_size
    except OSError:
        size = 0
    if size < floor:
        result["action"] = "failed_too_small"
        result["error"] = f"size={size} < min={floor}"
        tmp_out.unlink(missing_ok=True)
        return result

    dur = _ffprobe_duration(tmp_out)
    if dur > 0 and dur < min_d:
        result["action"] = "skipped_short"
        result["duration_sec"] = dur
        result["error"] = f"downloaded_duration_sec={dur} < min={min_d}"
        tmp_out.unlink(missing_ok=True)
        return result

    tmp_out.replace(dest)
    desc = str(probe.get("description") or "") if probe.get("ok") else ""
    write_library_meta(
        ch,
        vid,
        title=title or str(probe.get("title") or ""),
        views=views,
        avd_pct=avd_pct,
        rank_metric=rank_metric,
        duration_sec=dur if dur > 0 else None,
        extra={"description": desc[:5000]} if desc else None,
    )
    # Also stash description into publish_manifest via rewrite.
    if desc:
        write_library_meta(
            ch,
            vid,
            title=title or str(probe.get("title") or ""),
            views=views,
            avd_pct=avd_pct,
            rank_metric=rank_metric,
            duration_sec=dur if dur > 0 else None,
            extra={"description": desc[:5000]},
        )

    result["ok"] = True
    result["action"] = "downloaded"
    result["duration_sec"] = dur if dur > 0 else None
    result["size_bytes"] = dest.stat().st_size
    result["title"] = title or str(probe.get("title") or "")
    if sleep_after_sec > 0:
        time.sleep(float(sleep_after_sec))
    return result


def ensure_channel_library(
    channel: str,
    *,
    top_n: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    download_fn: Callable[..., dict[str, Any]] | None = None,
    sleep_between_sec: float | None = None,
) -> dict[str, Any]:
    """Ensure top-N ranked publics exist under ``output/live_vods/<channel>/``."""
    ch = (channel or "").strip().lower()
    n = default_top_n() if top_n is None else max(1, int(top_n))
    sleep_s = (
        DEFAULT_SLEEP_BETWEEN_SEC
        if sleep_between_sec is None
        else max(0.0, float(sleep_between_sec))
    )
    ranked = rank_top_publics_for_library(ch, limit=n)
    result: dict[str, Any] = {
        "ok": True,
        "channel": ch,
        "top_n": n,
        "dry_run": dry_run,
        "ranked_candidates": ranked[: max(n * 4, n)],
        "ensured": [],
        "skipped_short": [],
        "failed": [],
        "featured": None,
    }
    if ch not in CHANNELS:
        result["ok"] = False
        result["error"] = "unknown_channel"
        return result

    dl = download_fn or download_public_vod
    filled = 0
    bot_blocked = False
    for row in ranked:
        if filled >= n:
            break
        vid = str(row.get("video_id") or "")
        if dry_run:
            entry = {
                "video_id": vid,
                "action": "dry_run",
                "library_ok": bool(row.get("library_ok")),
                "path": row.get("library_path"),
                "rank_metric": row.get("rank_metric"),
                "views": row.get("views"),
                "title": row.get("title"),
            }
            result["ensured"].append(entry)
            if not result["featured"]:
                result["featured"] = entry
            filled += 1
            continue

        one = dl(
            ch,
            vid,
            title=str(row.get("title") or ""),
            views=int(row.get("views") or 0),
            avd_pct=row.get("avd_pct"),
            rank_metric=float(row.get("rank_metric") or 0.0),
            force=force,
            sleep_after_sec=sleep_s if filled + 1 < n else 0.0,
        )
        action = str(one.get("action") or "")
        if action == "skipped_short":
            result["skipped_short"].append(one)
            continue
        if action == "bot_blocked":
            result["failed"].append(one)
            bot_blocked = True
            result["bot_blocked"] = True
            break
        if not one.get("ok"):
            result["failed"].append(one)
            continue
        entry = {
            "video_id": vid,
            "action": one.get("action"),
            "path": one.get("path"),
            "duration_sec": one.get("duration_sec"),
            "size_bytes": one.get("size_bytes"),
            "rank_metric": row.get("rank_metric"),
            "views": row.get("views"),
            "title": one.get("title") or row.get("title"),
        }
        result["ensured"].append(entry)
        if not result["featured"]:
            result["featured"] = entry
        filled += 1

    # If YouTube bot-blocked (or zero fills), seed from best local ranked finals
    # so Live still gets purge-safe library copies.
    if not dry_run and filled < n and download_fn is None:
        seeded = seed_library_from_local_rank(
            ch, top_n=n - filled, force=force
        )
        result["local_seed"] = seeded
        for entry in seeded.get("ensured") or []:
            # Avoid duplicate video_ids already filled.
            if any(e.get("video_id") == entry.get("video_id") for e in result["ensured"]):
                continue
            result["ensured"].append(entry)
            if not result["featured"]:
                result["featured"] = entry
            filled += 1
            if filled >= n:
                break
        if bot_blocked:
            result["note"] = (
                "yt-dlp bot-blocked (set LIVE_VOD_YT_COOKIES to a Netscape "
                "cookies.txt); seeded remaining slots from local farm finals."
            )

    result["ok"] = bool(result["ensured"]) or dry_run
    result["filled"] = filled
    return result


def seed_library_from_local_rank(
    channel: str,
    *,
    top_n: int = 1,
    force: bool = False,
) -> dict[str, Any]:
    """Copy top local channel-owned longform finals into the Live library.

    Used when public YouTube downloads are blocked / missing so Live still has
    dedicated purge-safe files under ``output/live_vods``.
    """
    from src.streaming.vod_picker import rank_vods

    ch = (channel or "").strip().lower()
    out: dict[str, Any] = {
        "ok": False,
        "channel": ch,
        "ensured": [],
        "source": "local_rank",
    }
    if ch not in CHANNELS:
        out["error"] = "unknown_channel"
        return out
    ranked = rank_vods(
        ch,
        limit=max(top_n * 3, top_n),
        prefer_public=True,
        allow_nonpublic_fallback=True,
    )
    filled = 0
    for c in ranked:
        if filled >= top_n:
            break
        vid = str(c.video_id or "").strip()
        if not vid:
            # Synthetic id from path hash so library path is stable.
            vid = f"local_{Path(c.path).parent.parent.name[:40]}"
        src = Path(c.path)
        if not library_final_ok(src):
            continue
        if "/live_vods/" in src.as_posix():
            # Already a library file.
            entry = {
                "video_id": vid,
                "action": "already_library",
                "path": str(src),
                "views": c.views,
                "rank_metric": c.rank_metric,
                "title": "",
            }
            out["ensured"].append(entry)
            filled += 1
            continue
        one = copy_farm_final_into_library(
            ch,
            vid,
            src,
            title="",
            views=int(c.views or 0),
            avd_pct=c.avd_pct,
            rank_metric=float(c.rank_metric or 0.0),
        )
        if not one.get("ok"):
            continue
        # Prefer not re-copying when force false and exists — already handled.
        out["ensured"].append(
            {
                "video_id": vid,
                "action": one.get("action"),
                "path": one.get("path"),
                "duration_sec": one.get("duration_sec"),
                "size_bytes": one.get("size_bytes"),
                "views": c.views,
                "rank_metric": c.rank_metric,
                "title": one.get("title") or "",
                "is_public": bool(c.is_public),
            }
        )
        filled += 1
    out["ok"] = bool(out["ensured"])
    out["filled"] = filled
    return out


def ensure_all_channel_libraries(
    *,
    channels: tuple[str, ...] | list[str] = CHANNELS,
    top_n: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    download_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ensure Live libraries for all Brand channels; write ops status."""
    if not library_enabled() and not dry_run:
        return {
            "ok": True,
            "skipped": True,
            "reason": "live_vod_library_disabled",
            "ts": _utc_now(),
        }
    payload: dict[str, Any] = {
        "ok": True,
        "ts": _utc_now(),
        "module": "live_vod_library",
        "dry_run": dry_run,
        "top_n": default_top_n() if top_n is None else int(top_n),
        "root": str(LIVE_VODS_ROOT),
        "channels": {},
    }
    for ch in channels:
        row = ensure_channel_library(
            ch,
            top_n=top_n,
            dry_run=dry_run,
            force=force,
            download_fn=download_fn,
        )
        payload["channels"][ch] = row
        if not row.get("ok"):
            payload["ok"] = False
    if not dry_run:
        OPS.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(LIBRARY_STATUS, payload)
    return payload
