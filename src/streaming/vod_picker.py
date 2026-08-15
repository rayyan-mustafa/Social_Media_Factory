"""Auto best-VOD picker for vod_loop playlists (own content only).

Ranks local ``final.mp4`` under ``output/live_vods`` (preferred) and
``output/jobs`` **per channel** by ``views × AVD%`` (scorecard / analytics /
algo_insights), falling back to views-only when AVD is missing. Soft
tie-breakers: duration / size / recency / channel brand hints /
sibling-head diversity.

Hard rules:
- Never cross-channel pick (sibling Brand VODs excluded).
- Prefer **public** YouTube uploads for that channel only (OpsStore
  ``status=public`` and/or ``vod_views_cache.channel_by_video_id`` listing).
  Private / scheduled / hold locals are excluded unless the public pool is
  empty (documented fallback so Live encode never blanks).
- Live library: before playlist refresh, top-N publics are downloaded into
  ``output/live_vods/<channel>/<video_id>/final.mp4`` so encode does not
  depend on farm job finals surviving post-public purge.
- Encode needs a local ``final.mp4`` mapped to that public ``video_id``;
  public uploads without local media are noted (and downloaded when the
  Live library hook runs).

Default pool size is ``VOD_PICKER_LIMIT`` (8) so Live featured mode still
loops the head while tails supply more SMM soft-swap candidates.

Writes FFmpeg concat playlists under ``config/streaming/playlist_<channel>.txt``.
Never invents RTMP keys. Never pulls third-party (non-owned) URLs.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
STREAM_CFG = ROOT / "config" / "streaming"
OPS = ROOT / "output" / "ops"
JOBS = ROOT / "output" / "jobs"
LIVE_VODS = ROOT / "output" / "live_vods"
PICKER_STATUS = OPS / "vod_picker_status.json"
PLAYLIST_FORCE_LOCK = OPS / "live_playlist_force_lock.json"

CHANNELS = ("napstorian", "napping_historian")

# Brand-fit tokens matched against path + publish title (underscores OK).
# napstorian = what-if / alternate history winners; historian = mystery / Tudor /
# empire documentary packaging (not the same Anne-Boleyn what-if stack).
_CHANNEL_HINTS: dict[str, tuple[str, ...]] = {
    "napstorian": (
        "what_if",
        "what if",
        "anne",
        "boleyn",
        "henry",
        "cromwell",
        "jane",
        "outlived",
        "survived",
        "regent",
        "alternate",
        "counterfactual",
    ),
    "napping_historian": (
        "mystery",
        "dark_history",
        "forgotten",
        "empire",
        "roman",
        "mongol",
        "ottoman",
        "atlantic",
        "dynasty",
        "tudor",
        "secret",
        "haunt",
        "documentary",
        "catherine",
        "aragon",
        "mary",
        "fall_of",
        "revenge",
        "court",
    ),
}

# Soft style markers beyond simple substring hits.
_NAPSTORIAN_WHAT_IF = re.compile(r"(?:^|[_\s])what[_\s]?if(?:[_\s]|$)", re.I)
_HISTORIAN_DOC = re.compile(
    r"(dark[_\s]?history|forgotten|secret|haunt|revenge|fall[_\s]?of|"
    r"documentary|mystery|empire|dynasty)",
    re.I,
)

DEFAULT_PICK_LIMIT = 8


def default_pick_limit() -> int:
    """Pool size for ranked Live playlist tails (env ``VOD_PICKER_LIMIT``)."""
    raw = (os.getenv("VOD_PICKER_LIMIT") or str(DEFAULT_PICK_LIMIT)).strip()
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = DEFAULT_PICK_LIMIT
    return max(1, min(20, n))


@dataclass
class VodCandidate:
    path: str
    duration_sec: float
    size_bytes: int
    mtime: float
    score: float
    reasons: list[str]
    views: int = 0
    views_src: str = "none"
    avd_pct: float | None = None
    avd_src: str = "none"
    # Primary Live rank key: views * (avd_pct/100) when AVD known, else views.
    rank_metric: float = 0.0
    video_id: str = ""
    public_proof: str = "none"  # ops_public | yt_channel_listing | none
    is_public: bool = False


def live_rank_metric(views: int, avd_pct: float | None) -> float:
    """Views weighted by AVD%; plain views when AVD missing."""
    v = max(0.0, float(views or 0))
    if avd_pct is None:
        return v
    try:
        avd = float(avd_pct)
    except (TypeError, ValueError):
        return v
    if avd <= 0:
        return v
    # avd_pct is 0–100 (YouTube averageViewPercentage).
    return v * (avd / 100.0)


def _load_avd_by_video_id() -> dict[str, dict[str, Any]]:
    """Map video_id → {avd_pct, source} from scorecard / analytics / insights.

    Preference: scorecard > retention_dogs > analytics artifacts > algo_insights.
    """
    out: dict[str, dict[str, Any]] = {}
    # Higher wins when replacing an existing entry.
    _src_rank = {
        "algo_insights": 1,
        "analytics_artifact": 2,
        "retention_dogs": 3,
        "scorecard": 4,
    }

    def _rank_of(src: str) -> int:
        s = str(src or "")
        for prefix, rank in _src_rank.items():
            if s.startswith(prefix):
                return rank
        return 0

    def _put(vid: str, avd: Any, src: str, *, prefer: bool = False) -> None:
        vid = str(vid or "").strip()
        if not vid:
            return
        try:
            val = float(avd)
        except (TypeError, ValueError):
            return
        if val <= 0:
            return
        prev = out.get(vid)
        if prev and not prefer:
            if _rank_of(str(prev.get("source") or "")) >= _rank_of(src):
                return
        out[vid] = {"avd_pct": round(val, 3), "source": src}

    # algo_insights.json — historical retention snapshots (seed)
    insights_path = OPS / "algo_insights.json"
    if insights_path.is_file():
        try:
            rows = json.loads(insights_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rows = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                metrics = (
                    row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
                )
                vid = str(row.get("video_id") or metrics.get("video_id") or "").strip()
                avd = metrics.get("avd_pct")
                if avd is None:
                    avd = row.get("avd_pct")
                if vid and avd is not None:
                    _put(vid, avd, "algo_insights")

    # Analytics probe/sample artifacts (averageViewPercentage per video id).
    for path in sorted(OPS.glob("_analytics_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for ch, block in data.items():
            if not isinstance(block, dict):
                continue
            analytics = block.get("analytics")
            if isinstance(analytics, dict):
                for vid, row in analytics.items():
                    if str(vid).startswith("channel_") or not isinstance(row, dict):
                        continue
                    headers = row.get("headers") if isinstance(row.get("headers"), list) else []
                    rows = row.get("rows") if isinstance(row.get("rows"), list) else []
                    if not headers or not rows:
                        continue
                    try:
                        idx = headers.index("averageViewPercentage")
                        avd = (rows[0] or [None])[idx]
                    except (ValueError, IndexError, TypeError):
                        continue
                    _put(str(vid), avd, f"analytics_artifact:{path.name}:{ch}")
            # Probe tries with parsed.avd_pct
            for try_row in block.get("tries") or []:
                if not isinstance(try_row, dict):
                    continue
                parsed = try_row.get("parsed") if isinstance(try_row.get("parsed"), dict) else {}
                vid = str(try_row.get("video") or "").strip()
                avd = parsed.get("avd_pct")
                if vid and avd is not None:
                    _put(vid, avd, f"analytics_artifact:{path.name}:probe")

    # smm_retention_dogs.json — red AVD list (still usable for ranking).
    dogs_path = OPS / "smm_retention_dogs.json"
    if dogs_path.is_file():
        try:
            dogs = json.loads(dogs_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            dogs = {}
        channels = dogs.get("channels") if isinstance(dogs, dict) else {}
        if isinstance(channels, dict):
            for ch, rows in channels.items():
                if not isinstance(rows, list):
                    continue
                for v in rows:
                    if not isinstance(v, dict) or v.get("avd_pct") is None:
                        continue
                    _put(
                        str(v.get("video_id") or ""),
                        v.get("avd_pct"),
                        f"retention_dogs:{ch}",
                    )

    # smm_yt_scorecard.jsonl — newest day last; prefer over insights/artifacts
    score_path = OPS / "smm_yt_scorecard.jsonl"
    if score_path.is_file():
        try:
            lines = score_path.read_text(encoding="utf-8").strip().splitlines()
        except OSError:
            lines = []
        for line in lines[-30:]:
            try:
                day = json.loads(line)
            except json.JSONDecodeError:
                continue
            day_utc = str(day.get("day_utc") or "")
            channels = day.get("channels") if isinstance(day.get("channels"), dict) else {}
            for ch, block in channels.items():
                videos = (block or {}).get("videos") if isinstance(block, dict) else None
                if not isinstance(videos, list):
                    continue
                for v in videos:
                    if not isinstance(v, dict) or v.get("avd_pct") is None:
                        continue
                    _put(
                        str(v.get("video_id") or ""),
                        v.get("avd_pct"),
                        f"scorecard:{day_utc}:{ch}",
                        prefer=True,
                    )
    return out


def resolve_avd_for_path(
    path: Path | str,
    *,
    publish_index: dict[str, dict[str, Any]] | None = None,
    avd_by_id: dict[str, dict[str, Any]] | None = None,
) -> tuple[float | None, str]:
    """Return (avd_pct, source) for a local final via its published video_id."""
    pub_idx = publish_index if publish_index is not None else _load_publish_index()
    by_id = avd_by_id if avd_by_id is not None else _load_avd_by_video_id()
    resolved = str(Path(path).resolve())
    pub = pub_idx.get(resolved) or {}
    vid = str(pub.get("video_id") or "").strip()
    if not vid:
        # Fallback: scan publish_manifest beside the media.
        try:
            parts = Path(resolved).parts
            if "live_vods" in parts:
                lidx = parts.index("live_vods")
                if lidx + 2 < len(parts):
                    vid = str(parts[lidx + 2]).strip()
            if not vid and "jobs" in parts:
                jidx = parts.index("jobs")
                job_dir = Path(*parts[: jidx + 2])
                for man_name in ("publish_manifest.json", "pipeline_manifest.json"):
                    man_path = job_dir / man_name
                    if not man_path.is_file():
                        continue
                    try:
                        data = json.loads(man_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    vid = str(data.get("video_id") or "").strip()
                    if vid:
                        break
        except Exception:  # noqa: BLE001
            vid = ""
    if not vid:
        return None, "none"
    row = by_id.get(vid) or {}
    avd = row.get("avd_pct")
    if avd is None:
        return None, "none"
    try:
        return float(avd), str(row.get("source") or "avd_cache")
    except (TypeError, ValueError):
        return None, "none"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def playlist_force_lock_path() -> Path:
    return OPS / "live_playlist_force_lock.json"


def read_playlist_force_lock() -> dict[str, Any] | None:
    """If locked, Live featured heads are pinned for a new-format / ops test."""
    lock_path = playlist_force_lock_path()
    if not lock_path.is_file():
        return None
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("locked"):
        return None
    return data


def write_playlist_force_lock(
    *,
    heads: dict[str, str],
    reason: str = "new_format_live_test",
    exclude_paths: list[str] | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Pin playlist heads so vod_picker / SMM soft-swap cannot undo a Live test."""
    OPS.mkdir(parents=True, exist_ok=True)
    payload = {
        "locked": True,
        "reason": reason,
        "updated_at": _utc_now(),
        "heads": {str(k): str(Path(v).resolve()) for k, v in heads.items()},
        "exclude_paths": [str(Path(p).resolve()) for p in (exclude_paths or [])],
        "note": note
        or (
            "stream_beat / vod_picker / SMM underperform-swap honor this lock. "
            "Clear by deleting live_playlist_force_lock.json or set locked=false."
        ),
    }
    playlist_force_lock_path().write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def clear_playlist_force_lock() -> bool:
    lock_path = playlist_force_lock_path()
    if lock_path.is_file():
        lock_path.unlink(missing_ok=True)
        return True
    return False


def is_new_format_final(path: Path | str) -> bool:
    """True when edit_manifest records selected-pack / premium overlays.

    Old finals without overlays are NOT new-format — do not use them as Live
    test heads unless recomposed.
    """
    p = Path(path)
    if not p.is_file():
        return False
    # final.mp4 lives in …/video/final.mp4 next to edit_manifest.json
    manifest = p.parent / "edit_manifest.json"
    if not manifest.is_file():
        return False
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    meta = data.get("meta") if isinstance(data, dict) else None
    if not isinstance(meta, dict):
        return False
    if meta.get("premium_overlays_v1"):
        return True
    ovs = meta.get("infographic_overlays") or []
    if ovs:
        return True
    return bool(meta.get("compose_infographic_overlays_enabled")) and bool(
        meta.get("selected_pack") or meta.get("new_format")
    )


def write_forced_playlist(
    channel: str,
    head: Path | str,
    *,
    tails: list[Path | str] | None = None,
    dry_run: bool = False,
    reason: str = "force_lock",
) -> dict[str, Any]:
    """Write playlist with an explicit head (Live featured VOD)."""
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel: {channel}")
    head_p = Path(head).resolve()
    if not head_p.is_file():
        return {
            "ok": False,
            "channel": channel,
            "error": f"head_missing:{head_p}",
            "written": False,
        }
    paths = [head_p.as_posix()]
    for t in tails or []:
        tp = Path(t).resolve()
        if tp.is_file() and tp.as_posix() not in paths:
            paths.append(tp.as_posix())
    pl = playlist_path(channel)
    lines = [
        f"# FORCE-LOCKED by live_playlist_force_lock {_utc_now()} channel={channel}",
        f"# reason={reason}. Head = featured Live VOD (infinite -stream_loop -1).",
        "# Do not auto-rank until lock cleared (output/ops/live_playlist_force_lock.json).",
    ]
    for abs_p in paths:
        lines.append(f"file '{abs_p}'")
    body = "\n".join(lines) + "\n"
    if not dry_run:
        STREAM_CFG.mkdir(parents=True, exist_ok=True)
        pl.write_text(body, encoding="utf-8")
    return {
        "ok": True,
        "channel": channel,
        "playlist": str(pl),
        "written": not dry_run,
        "entries": paths,
        "forced": True,
        "reason": reason,
    }


def _ffprobe_duration(path: Path) -> float:
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            text=True,
            timeout=30,
        ).strip()
        return float(out or 0)
    except Exception:
        return 0.0


VIEWS_CACHE = OPS / "vod_views_cache.json"


def _load_benchmark_titles(channel: str | None = None) -> list[str]:
    p = OPS / "benchmarks.json"
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    titles: list[str] = []
    top = _winners_top_for_channel(data, channel)
    for row in top:
        t = str(row.get("title") or "").strip()
        if t:
            titles.append(t.lower())
    return titles


def _winners_top_for_channel(
    data: dict[str, Any], channel: str | None = None
) -> list[dict[str, Any]]:
    """Prefer ``channel_winners_by_channel[ch]``; napstorian falls back to legacy."""
    ch = (channel or "").strip().lower()
    by_ch = data.get("channel_winners_by_channel") or {}
    if ch and isinstance(by_ch, dict):
        row = by_ch.get(ch)
        if isinstance(row, dict) and row.get("top"):
            return list(row.get("top") or [])
    if ch in {"", "napstorian", "default"}:
        return list(((data.get("channel_winners") or {}).get("top")) or [])
    return []


def _load_winner_views_by_title(
    channel: str | None = None,
) -> list[tuple[str, int]]:
    """(title_lower, views) from SMM channel winners — highest views first."""
    p = OPS / "benchmarks.json"
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[tuple[str, int]] = []
    top = _winners_top_for_channel(data, channel)
    for row in top:
        t = str(row.get("title") or "").strip().lower()
        try:
            views = int(row.get("views") or 0)
        except (TypeError, ValueError):
            views = 0
        if t and views > 0:
            out.append((t, views))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def _extract_manifest_channel(data: dict[str, Any]) -> str:
    """Best-effort channel tag from publish / pipeline manifests."""
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    for key in (
        "channel",
        "youtube_channel",
        "target_channel",
        "publish_channel",
        "brand_channel",
    ):
        for bag in (data, meta):
            raw = str((bag or {}).get(key) or "").strip().lower()
            if raw in CHANNELS:
                return raw
    return ""


def _load_ops_job_channels() -> dict[str, str]:
    """Map job folder name / resolved path prefix → channel from OpsStore."""
    out: dict[str, str] = {}
    try:
        from src.agents.store import OpsStore

        for job in OpsStore().list_jobs():
            ch = str((job.meta or {}).get("channel") or "").strip().lower()
            if ch not in CHANNELS:
                continue
            meta = job.meta or {}
            for key in ("job_dir", "output_dir", "workdir"):
                raw = meta.get(key)
                if not raw:
                    continue
                p = Path(str(raw))
                out[p.name] = ch
                try:
                    out[str(p.resolve())] = ch
                except Exception:
                    out[str(p)] = ch
            jd = getattr(job, "job_dir", None) or meta.get("job_dir")
            if jd:
                p = Path(str(jd))
                out[p.name] = ch
                try:
                    out[str(p.resolve())] = ch
                except Exception:
                    out[str(p)] = ch
            sop = meta.get("sop_compliance_path")
            if sop:
                # …/output/jobs/<job_folder>/ops/SOP_COMPLIANCE.md
                try:
                    job_dir = Path(str(sop)).resolve().parent.parent
                    out[job_dir.name] = ch
                    out[str(job_dir)] = ch
                except Exception:
                    pass
            title = str(job.title or "").strip().lower()
            if title:
                out[f"title:{title}"] = ch
    except Exception:
        return out
    return out


def _is_live_stream_title(title: str) -> bool:
    t = (title or "").strip().lower()
    return "live stream" in t or t.endswith(" live") or t.startswith("live:")


def _load_ops_job_privacy() -> dict[str, dict[str, Any]]:
    """Map video_id → {status, channel, title, job_dir} from OpsStore."""
    out: dict[str, dict[str, Any]] = {}
    try:
        from src.agents.store import OpsStore

        for job in OpsStore().list_jobs():
            vid = str(job.video_id or "").strip()
            if not vid:
                continue
            meta = job.meta or {}
            ch = str(meta.get("channel") or "").strip().lower()
            job_dir = str(job.job_dir or meta.get("job_dir") or meta.get("output_dir") or "")
            out[vid] = {
                "status": str(job.status or "").strip().lower(),
                "channel": ch if ch in CHANNELS else "",
                "title": str(job.title or ""),
                "job_dir": job_dir,
            }
    except Exception:
        return out
    return out


def _load_public_video_ids(
    *,
    views_cache: dict[str, Any] | None = None,
    ops_privacy: dict[str, dict[str, Any]] | None = None,
) -> dict[str, set[str]]:
    """Per-channel set of YouTube **public** video_ids.

    Sources (union):
    1. ``vod_views_cache.channel_by_video_id`` — SMM listing of public uploads
    2. OpsStore jobs with ``status=public`` tagged to that channel
    """
    by_ch: dict[str, set[str]] = {ch: set() for ch in CHANNELS}
    cache = views_cache if views_cache is not None else _load_views_cache()
    titles = dict(cache.get("titles_by_id") or {})
    listed = cache.get("channel_by_video_id") or {}
    if isinstance(listed, dict):
        for vid, ch in listed.items():
            vid_s = str(vid or "").strip()
            ch_s = str(ch or "").strip().lower()
            if not vid_s or ch_s not in CHANNELS:
                continue
            if _is_live_stream_title(str(titles.get(vid_s) or "")):
                continue
            by_ch[ch_s].add(vid_s)
    privacy = ops_privacy if ops_privacy is not None else _load_ops_job_privacy()
    for vid, row in privacy.items():
        if str(row.get("status") or "") != "public":
            continue
        ch = str(row.get("channel") or "").strip().lower()
        if ch not in CHANNELS:
            # Infer channel from views-cache listing when OpsStore channel blank.
            listed_ch = str((listed or {}).get(vid) or "").strip().lower()
            ch = listed_ch if listed_ch in CHANNELS else ""
        if ch not in CHANNELS:
            continue
        if _is_live_stream_title(str(row.get("title") or titles.get(vid) or "")):
            continue
        by_ch[ch].add(vid)
    return by_ch


def _video_id_for_path(
    path: Path | str,
    *,
    publish_index: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Resolve published YouTube video_id for a local final path."""
    resolved = str(Path(path).resolve())
    pub_idx = publish_index if publish_index is not None else _load_publish_index()
    pub = pub_idx.get(resolved) or {}
    vid = str(pub.get("video_id") or "").strip()
    if vid:
        return vid
    try:
        parts = Path(resolved).parts
        if "live_vods" in parts:
            lidx = parts.index("live_vods")
            if lidx + 2 < len(parts):
                return str(parts[lidx + 2]).strip()
        if "jobs" in parts:
            jidx = parts.index("jobs")
            job_dir = Path(*parts[: jidx + 2])
            for man_name in ("publish_manifest.json", "pipeline_manifest.json"):
                man_path = job_dir / man_name
                if not man_path.is_file():
                    continue
                try:
                    data = json.loads(man_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                vid = str(data.get("video_id") or "").strip()
                if vid:
                    return vid
    except Exception:  # noqa: BLE001
        return ""
    return ""


def public_proof_for_path(
    path: Path | str,
    channel: str,
    *,
    publish_index: dict[str, dict[str, Any]] | None = None,
    public_ids_by_channel: dict[str, set[str]] | None = None,
    ops_privacy: dict[str, dict[str, Any]] | None = None,
    views_cache: dict[str, Any] | None = None,
) -> tuple[bool, str, str]:
    """Return (is_public_for_channel, video_id, proof_src).

    Proof sources:
    - ``ops_public`` — OpsStore job status public for this channel
    - ``yt_channel_listing`` — video_id listed under channel in views cache
    - ``none`` — not public (or wrong channel / missing video_id)
    """
    ch = (channel or "").strip().lower()
    if ch not in CHANNELS:
        return False, "", "none"
    pub_idx = publish_index if publish_index is not None else _load_publish_index()
    privacy = ops_privacy if ops_privacy is not None else _load_ops_job_privacy()
    by_ch = (
        public_ids_by_channel
        if public_ids_by_channel is not None
        else _load_public_video_ids(views_cache=views_cache, ops_privacy=privacy)
    )
    vid = _video_id_for_path(path, publish_index=pub_idx)
    if not vid:
        return False, "", "none"
    ops = privacy.get(vid) or {}
    ops_status = str(ops.get("status") or "").strip().lower()
    ops_ch = str(ops.get("channel") or "").strip().lower()
    if ops_status == "public" and (ops_ch == ch or vid in (by_ch.get(ch) or set())):
        return True, vid, "ops_public"
    if vid in (by_ch.get(ch) or set()):
        return True, vid, "yt_channel_listing"
    return False, vid, "none"


def public_videos_missing_local_final(
    channel: str,
    *,
    publish_index: dict[str, dict[str, Any]] | None = None,
    public_ids_by_channel: dict[str, set[str]] | None = None,
    views_cache: dict[str, Any] | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Public video_ids for channel with views but no local final.mp4 (skipped)."""
    ch = (channel or "").strip().lower()
    cache = views_cache if views_cache is not None else _load_views_cache()
    by_ch = (
        public_ids_by_channel
        if public_ids_by_channel is not None
        else _load_public_video_ids(views_cache=cache)
    )
    pub_idx = publish_index if publish_index is not None else _load_publish_index()
    paths_by_vid: dict[str, list[str]] = {}
    for path, meta in pub_idx.items():
        vid = str(meta.get("video_id") or "").strip()
        if not vid:
            continue
        if Path(path).is_file():
            paths_by_vid.setdefault(vid, []).append(path)
    by_id = dict(cache.get("by_video_id") or {})
    titles = dict(cache.get("titles_by_id") or {})
    rows: list[dict[str, Any]] = []
    for vid in by_ch.get(ch) or set():
        if paths_by_vid.get(vid):
            continue
        try:
            views = int(by_id.get(vid) or 0)
        except (TypeError, ValueError):
            views = 0
        rows.append(
            {
                "video_id": vid,
                "views": views,
                "title": str(titles.get(vid) or ""),
                "note": "public_on_yt_but_no_local_final",
            }
        )
    rows.sort(key=lambda r: int(r.get("views") or 0), reverse=True)
    return rows[: max(0, limit)]


def _channel_for_final_path(
    path: Path,
    *,
    publish_index: dict[str, dict[str, Any]] | None = None,
    ops_channels: dict[str, str] | None = None,
    views_cache: dict[str, Any] | None = None,
) -> str:
    """Best-effort Brand channel for a local final path."""
    resolved = path.resolve()
    pub = (publish_index or {}).get(str(resolved)) or {}
    tagged = str(pub.get("channel") or "").strip().lower()
    if tagged in CHANNELS:
        return tagged
    vid = str(pub.get("video_id") or "").strip()
    listed = _channel_for_video_id(vid, views_cache) if vid else ""
    if listed in CHANNELS:
        return listed
    parts = resolved.parts
    if "live_vods" in parts:
        lidx = parts.index("live_vods")
        if lidx + 1 < len(parts):
            ch = str(parts[lidx + 1]).strip().lower()
            if ch in CHANNELS:
                return ch
    ops = ops_channels if ops_channels is not None else _load_ops_job_channels()
    # Walk up to find jobs/<folder>
    if "jobs" in parts:
        jidx = parts.index("jobs")
        if jidx + 1 < len(parts):
            folder = parts[jidx + 1]
            if folder in ops:
                return ops[folder]
            job_dir = Path(*parts[: jidx + 2])
            key = str(job_dir)
            if key in ops:
                return ops[key]
            try:
                key2 = str(job_dir.resolve())
                if key2 in ops:
                    return ops[key2]
            except Exception:
                pass
            from_dir = _job_channel_from_dir(job_dir)
            if from_dir:
                return from_dir
    return ""


def _job_channel_from_dir(job_dir: Path) -> str:
    """Resolve Brand channel for a job folder via publish/pipeline manifests."""
    for name in ("publish_manifest.json", "pipeline_manifest.json", "job.json"):
        man = job_dir / name
        if not man.is_file():
            continue
        try:
            data = json.loads(man.read_text(encoding="utf-8"))
        except Exception:
            continue
        ch = _extract_manifest_channel(data if isinstance(data, dict) else {})
        if ch:
            return ch
    return ""


def _index_final_variants(
    idx: dict[str, dict[str, Any]],
    *,
    job_dir: Path,
    channel: str,
    video_id: str,
    title: str,
) -> None:
    """Index all Live-eligible finals under a job (incl. live_head / production)."""
    candidates = [
        job_dir / "video" / "final.mp4",
        job_dir / "video_live_head" / "final.mp4",
        job_dir / "video_final_production" / "final.mp4",
    ]
    for p in candidates:
        if not p.is_file():
            continue
        key = str(p.resolve())
        prev = idx.get(key) or {}
        # Prefer richer prior metadata; never overwrite a known channel with empty.
        prev_ch = str(prev.get("channel") or "").strip().lower()
        use_ch = prev_ch if prev_ch in CHANNELS else channel
        idx[key] = {
            "video_id": str(prev.get("video_id") or video_id or "").strip(),
            "title": str(prev.get("title") or title or "").strip(),
            "channel": use_ch,
            "path": key,
        }


def _index_live_vod_library(idx: dict[str, dict[str, Any]]) -> None:
    """Index downloaded Live-library finals (preferred over farm job paths)."""
    if not LIVE_VODS.is_dir():
        return
    for ch_dir in LIVE_VODS.iterdir():
        if not ch_dir.is_dir():
            continue
        ch = ch_dir.name.strip().lower()
        if ch not in CHANNELS:
            continue
        for vid_dir in ch_dir.iterdir():
            if not vid_dir.is_dir():
                continue
            vid = vid_dir.name.strip()
            final = vid_dir / "final.mp4"
            if not final.is_file():
                continue
            title = ""
            meta = vid_dir / "meta.json"
            pub = vid_dir / "publish_manifest.json"
            for man_path in (meta, pub):
                if not man_path.is_file():
                    continue
                try:
                    data = json.loads(man_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(data, dict):
                    continue
                if not vid:
                    vid = str(data.get("video_id") or "").strip()
                if not title:
                    title = str(data.get("title") or "").strip()
                man_ch = str(data.get("channel") or "").strip().lower()
                if man_ch in CHANNELS:
                    ch = man_ch
            key = str(final.resolve())
            idx[key] = {
                "video_id": vid,
                "title": title,
                "channel": ch,
                "path": key,
                "source": "live_vod_library",
            }


def _load_publish_index() -> dict[str, dict[str, Any]]:
    """Map resolved final.mp4 path → {video_id, title, channel?} from manifests.

    Indexes farm jobs **and** ``output/live_vods`` (Live library preferred when
    both exist for the same ``video_id`` at rank time).
    """
    idx: dict[str, dict[str, Any]] = {}
    if JOBS.is_dir():
        for man in JOBS.glob("*/publish_manifest.json"):
            try:
                data = json.loads(man.read_text(encoding="utf-8"))
            except Exception:
                continue
            job_dir = man.parent
            final = data.get("final_path") or str(job_dir / "video" / "final.mp4")
            p = Path(str(final))
            if not p.is_file():
                p = job_dir / "video" / "final.mp4"
            channel = _extract_manifest_channel(data)
            if not channel:
                channel = _job_channel_from_dir(job_dir)
            video_id = str(data.get("video_id") or "").strip()
            title = str(data.get("title") or "").strip()
            if p.is_file():
                key = str(p.resolve())
                idx[key] = {
                    "video_id": video_id,
                    "title": title,
                    "channel": channel,
                    "path": key,
                }
            _index_final_variants(
                idx,
                job_dir=job_dir,
                channel=channel,
                video_id=video_id,
                title=title,
            )
        # Jobs without publish_manifest still may have Live finals + pipeline channel.
        for job_dir in JOBS.iterdir():
            if not job_dir.is_dir():
                continue
            channel = _job_channel_from_dir(job_dir)
            if not channel:
                continue
            _index_final_variants(
                idx, job_dir=job_dir, channel=channel, video_id="", title=""
            )
    _index_live_vod_library(idx)
    return idx


def _load_views_cache() -> dict[str, Any]:
    if not VIEWS_CACHE.is_file():
        return {}
    try:
        return json.loads(VIEWS_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _channel_for_video_id(video_id: str, cache: dict[str, Any] | None = None) -> str:
    """Channel that listed this upload in views cache (when known)."""
    vid = (video_id or "").strip()
    if not vid:
        return ""
    data = cache if cache is not None else _load_views_cache()
    by_ch = data.get("channel_by_video_id") or {}
    if isinstance(by_ch, dict):
        ch = str(by_ch.get(vid) or "").strip().lower()
        if ch in CHANNELS:
            return ch
    return ""


def resolve_views_for_path(
    path: Path,
    *,
    publish_index: dict[str, dict[str, Any]] | None = None,
    winner_titles: list[tuple[str, int]] | None = None,
    channel: str | None = None,
    exact_only: bool = False,
) -> tuple[int, str]:
    """Return (views, source) for a local final — prefer exact video_id cache.

    When ``exact_only`` is True (public-pool candidates), skip soft
    winner-title overlap so another public upload's views cannot leak onto
    an unrelated local final.
    """
    resolved = str(path.resolve())
    cache = _load_views_cache()
    by_path = dict(cache.get("by_path") or {})
    by_id = dict(cache.get("by_video_id") or {})
    if resolved in by_path:
        try:
            return int(by_path[resolved] or 0), "views_cache_path"
        except (TypeError, ValueError):
            pass
    pub = (publish_index or _load_publish_index()).get(resolved) or {}
    vid = str(pub.get("video_id") or "").strip()
    if not vid:
        # Also resolve via sibling manifests when publish_index row lacks video_id.
        try:
            from_path = _video_id_for_path(path, publish_index=publish_index)
            vid = str(from_path or "").strip()
        except Exception:
            vid = ""
    if vid and vid in by_id:
        try:
            return int(by_id[vid] or 0), "views_cache_video_id"
        except (TypeError, ValueError):
            pass
    # Known video_id but missing from cache → do not invent views via title overlap.
    if exact_only or vid:
        return 0, "none"
    # Title overlap with public channel winners (SMM) — soft signal only when
    # the local final has no published video_id (fallback / untagged pool).
    title = str(pub.get("title") or path.parent.parent.name).lower().replace("_", " ")
    best = 0
    for wtitle, views in winner_titles or _load_winner_views_by_title(channel):
        toks = re.findall(r"[a-z0-9]{4,}", title)
        hits = sum(1 for t in toks if t in wtitle)
        if hits >= 2 and views > best:
            best = views
    if best > 0:
        return best, "winner_title_overlap"
    return 0, "none"


def _load_smm_boost_tokens() -> set[str]:
    """Cheap token boost from SMM feedback files if present."""
    tokens: set[str] = set()
    for name in ("smm_harvest_feedback.json", "smm_quality_alerts.md"):
        p = OPS / name
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore").lower()
        except Exception:
            continue
        for tok in re.findall(r"[a-z]{4,}", text):
            if tok in {"true", "false", "null", "video", "views", "watch"}:
                continue
            tokens.add(tok)
        if len(tokens) > 400:
            break
    return tokens


def discover_finals(*, min_duration_sec: float = 180.0) -> list[Path]:
    found: list[Path] = []
    # Live library first (dedicated copies of top publics — purge-safe).
    if LIVE_VODS.is_dir():
        for p in LIVE_VODS.rglob("final.mp4"):
            found.append(p)
    if JOBS.is_dir():
        for p in JOBS.rglob("final.mp4"):
            # Skip obvious smokes / tiny tests by name.
            low = str(p).lower()
            if "pipeline_smoke" in low or "smoke_test" in low or "/sop_test_" in low:
                continue
            if "sop_test_" in Path(p).parent.parent.name.lower():
                continue
            found.append(p)
    # Optional curated edits
    smoke = ROOT / "output" / "video" / "smoke_ann_bolyn_edit" / "final.mp4"
    if smoke.is_file():
        found.append(smoke)
    # Dedupe
    uniq: dict[str, Path] = {str(p.resolve()): p.resolve() for p in found}
    out: list[Path] = []
    for p in uniq.values():
        if not p.is_file():
            continue
        # Size gate: < 2MB almost certainly placeholder/bad
        try:
            if p.stat().st_size < 2_000_000:
                continue
        except OSError:
            continue
        out.append(p)
    return out


def _prefer_live_library_paths(cands: list[VodCandidate]) -> list[VodCandidate]:
    """When the same video_id has both live_vods + farm paths, keep live_vods."""
    best: dict[str, VodCandidate] = {}
    no_vid: list[VodCandidate] = []
    for c in cands:
        vid = str(c.video_id or "").strip()
        if not vid:
            no_vid.append(c)
            continue
        prev = best.get(vid)
        if prev is None:
            best[vid] = c
            continue
        prev_live = "/live_vods/" in (prev.path or "").replace("\\", "/")
        cur_live = "/live_vods/" in (c.path or "").replace("\\", "/")
        if cur_live and not prev_live:
            best[vid] = c
        elif cur_live == prev_live:
            # Keep higher rank_metric / score.
            if (float(c.rank_metric or 0.0), float(c.score)) > (
                float(prev.rank_metric or 0.0),
                float(prev.score),
            ):
                best[vid] = c
    merged = list(best.values()) + no_vid
    merged.sort(
        key=lambda x: (float(x.rank_metric or 0.0), float(x.score)),
        reverse=True,
    )
    return merged


def score_candidate(
    path: Path,
    *,
    channel: str,
    bench_titles: list[str],
    smm_tokens: set[str],
    min_duration_sec: float = 180.0,
    publish_index: dict[str, dict[str, Any]] | None = None,
    winner_titles: list[tuple[str, int]] | None = None,
    views_cache: dict[str, Any] | None = None,
    avd_by_id: dict[str, dict[str, Any]] | None = None,
    public_ids_by_channel: dict[str, set[str]] | None = None,
    ops_privacy: dict[str, dict[str, Any]] | None = None,
    require_public: bool | None = None,
) -> VodCandidate | None:
    try:
        st = path.stat()
    except OSError:
        return None
    dur = _ffprobe_duration(path)
    if dur < min_duration_sec:
        return None
    reasons: list[str] = []
    score = 0.0
    pub_idx = publish_index if publish_index is not None else _load_publish_index()
    cache = views_cache if views_cache is not None else _load_views_cache()
    privacy = ops_privacy if ops_privacy is not None else _load_ops_job_privacy()
    public_ids = (
        public_ids_by_channel
        if public_ids_by_channel is not None
        else _load_public_video_ids(views_cache=cache, ops_privacy=privacy)
    )

    is_public, video_id, public_proof = public_proof_for_path(
        path,
        channel,
        publish_index=pub_idx,
        public_ids_by_channel=public_ids,
        ops_privacy=privacy,
        views_cache=cache,
    )
    if require_public is True and not is_public:
        return None

    # Views × AVD drive playlist order (rank_vods); composite score is tie-break.
    # Public pool: exact video_id / path cache only (no title-overlap leakage).
    views, view_src = resolve_views_for_path(
        path,
        publish_index=pub_idx,
        winner_titles=winner_titles,
        channel=channel,
        exact_only=bool(is_public or require_public),
    )
    avd_pct, avd_src = resolve_avd_for_path(
        path, publish_index=pub_idx, avd_by_id=avd_by_id
    )
    rank_m = live_rank_metric(int(views or 0), avd_pct)
    if views > 0:
        score += math.log1p(views) * 40.0
        reasons.append(f"yt_views={views}:{view_src}")
    if avd_pct is not None:
        # Soft boost for known watch-time so tie-breaks favor stickier VODs.
        score += min(25.0, float(avd_pct) * 0.25)
        reasons.append(f"avd_pct={avd_pct}:{avd_src}")
    else:
        reasons.append("avd_pct=missing:views_fallback")
    reasons.append(f"rank_metric={rank_m:.3f}")
    if is_public:
        reasons.append(f"public={public_proof}:{video_id}")
        score += 20
    elif video_id:
        ops_st = str((privacy.get(video_id) or {}).get("status") or "") or "unknown"
        reasons.append(f"privacy={ops_st}:{video_id}")

    # Duration: prefer 10–40 min retention longform (tie-break)
    if 600 <= dur <= 2400:
        score += 40
        reasons.append("duration_sweet")
    elif dur >= 300:
        score += 25
        reasons.append("duration_ok")
    else:
        score += 10
        reasons.append("duration_short")

    # Size as crude quality proxy
    mb = st.st_size / 1e6
    score += min(20.0, mb / 5.0)
    reasons.append(f"size_mb={mb:.1f}")

    # Recency
    age_days = max(0.0, (datetime.now(timezone.utc).timestamp() - st.st_mtime) / 86400.0)
    score += max(0.0, 15.0 - age_days)
    reasons.append(f"age_days={age_days:.1f}")

    pub = pub_idx.get(str(path.resolve())) or {}
    title = str(pub.get("title") or "").strip()
    name = path.as_posix().lower()
    stem = path.parent.parent.name.lower().replace("_", " ")
    corpus = f"{name} {stem} {title.lower()}"

    hints = _CHANNEL_HINTS.get(channel) or ()
    hit = sum(1 for h in hints if h.replace("_", " ") in corpus or h in name)
    if hit:
        # Stronger brand pull so channels diverge when views are close.
        score += 14 * hit
        reasons.append(f"channel_hints={hit}")

    # Style fit: napstorian loves what-if packaging; historian prefers doc/mystery
    # and soft-penalizes pure what-if stems when empire/mystery alternatives exist.
    if channel == "napstorian":
        if _NAPSTORIAN_WHAT_IF.search(corpus.replace("-", " ")):
            score += 28
            reasons.append("style_what_if")
    elif channel == "napping_historian":
        doc_hits = len(_HISTORIAN_DOC.findall(corpus.replace("-", " ")))
        if doc_hits:
            score += 18 * min(doc_hits, 3)
            reasons.append(f"style_doc_mystery={min(doc_hits, 3)}")
        if _NAPSTORIAN_WHAT_IF.search(corpus.replace("-", " ")):
            score -= 22
            reasons.append("style_what_if_penalty")

    # Prefer finals tagged / published to this Brand Account when metadata exists.
    # Hard rule: never put a sibling Brand's owned VOD on this channel's Live playlist.
    owner = _channel_for_final_path(
        path,
        publish_index=pub_idx,
        views_cache=cache,
    )
    if owner == channel:
        score += 35
        reasons.append("channel_owned")
    elif owner and owner in CHANNELS and owner != channel:
        # Hard exclude — historian must never feature napstorian VODs (and vice versa).
        return None
    else:
        # Untagged pool: historian rejects pure what-if packaging; napstorian
        # soft-rejects doc/mystery-only stems when what-if alternatives exist.
        if channel == "napping_historian" and _NAPSTORIAN_WHAT_IF.search(
            corpus.replace("-", " ")
        ):
            doc_hits = len(_HISTORIAN_DOC.findall(corpus.replace("-", " ")))
            if doc_hits == 0:
                return None
        if channel == "napstorian" and not _NAPSTORIAN_WHAT_IF.search(
            corpus.replace("-", " ")
        ):
            # Soft only — already handled via hint scores; keep eligible.
            pass

    for bt in bench_titles:
        # Soft overlap on 3+ char tokens
        overlap = 0
        for tok in re.findall(r"[a-z0-9]{4,}", stem):
            if tok in bt:
                overlap += 1
        if overlap >= 2:
            score += 12
            reasons.append("benchmark_title_overlap")
            break

    if smm_tokens:
        tok_hits = sum(1 for t in re.findall(r"[a-z]{4,}", stem) if t in smm_tokens)
        if tok_hits:
            score += min(10, tok_hits)
            reasons.append(f"smm_tokens={tok_hits}")

    return VodCandidate(
        path=str(path.resolve()),
        duration_sec=dur,
        size_bytes=int(st.st_size),
        mtime=float(st.st_mtime),
        score=round(score, 3),
        reasons=reasons,
        views=int(views or 0),
        views_src=str(view_src or "none"),
        avd_pct=float(avd_pct) if avd_pct is not None else None,
        avd_src=str(avd_src or "none"),
        rank_metric=round(float(rank_m), 3),
        video_id=str(video_id or ""),
        public_proof=str(public_proof or "none"),
        is_public=bool(is_public),
    )


def _diversify_away_from(
    ranked: list[VodCandidate],
    avoid_paths: set[str] | list[str] | None,
) -> list[VodCandidate]:
    """Move sibling-channel heads later when alternatives exist (keep rank order)."""
    if not ranked or not avoid_paths:
        return ranked
    avoid = {str(Path(p).resolve()) for p in avoid_paths if str(p).strip()}
    if not avoid:
        return ranked
    preferred = [c for c in ranked if c.path not in avoid]
    demoted = [c for c in ranked if c.path in avoid]
    if not preferred:
        return ranked
    for c in demoted:
        if "sibling_head_deprioritized" not in c.reasons:
            c.reasons = list(c.reasons) + ["sibling_head_deprioritized"]
    return preferred + demoted


def rank_vods(
    channel: str,
    *,
    limit: int | None = None,
    min_duration_sec: float | None = None,
    exclude_paths: set[str] | list[str] | None = None,
    deprioritize_paths: set[str] | list[str] | None = None,
    prefer_public: bool = True,
    allow_nonpublic_fallback: bool = True,
) -> list[VodCandidate]:
    """Rank local finals for ``channel``.

    Pool rule (when ``prefer_public``):
      1. Channel-matched locals whose ``video_id`` is **public** on that
         channel (OpsStore public and/or YT channel listing in views cache).
      2. If that pool is empty and ``allow_nonpublic_fallback``, fall back to
         private/scheduled/hold channel-owned locals so Live never blanks.
    """
    lim = default_pick_limit() if limit is None else int(limit)
    min_d = float(
        min_duration_sec
        if min_duration_sec is not None
        else float(os.getenv("VOD_PICKER_MIN_DURATION_SEC") or 180)
    )
    excluded = {
        str(Path(p).resolve())
        for p in (exclude_paths or [])
        if str(p).strip()
    }
    bench = _load_benchmark_titles(channel)
    smm = _load_smm_boost_tokens()
    publish_index = _load_publish_index()
    winner_titles = _load_winner_views_by_title(channel)
    views_cache = _load_views_cache()
    avd_by_id = _load_avd_by_video_id()
    ops_channels = _load_ops_job_channels()
    ops_privacy = _load_ops_job_privacy()
    public_ids = _load_public_video_ids(
        views_cache=views_cache, ops_privacy=ops_privacy
    )
    # Enrich publish_index channel from OpsStore when missing.
    if ops_channels:
        for key, meta in list(publish_index.items()):
            if meta.get("channel"):
                continue
            ch = _channel_for_final_path(
                Path(key), publish_index=publish_index, ops_channels=ops_channels, views_cache=views_cache
            )
            if ch:
                meta = dict(meta)
                meta["channel"] = ch
                publish_index[key] = meta

    def _score_all(*, require_public: bool | None) -> list[VodCandidate]:
        cands: list[VodCandidate] = []
        for p in discover_finals(min_duration_sec=min_d):
            if str(p.resolve()) in excluded:
                continue
            c = score_candidate(
                p,
                channel=channel,
                bench_titles=bench,
                smm_tokens=smm,
                min_duration_sec=min_d,
                publish_index=publish_index,
                winner_titles=winner_titles,
                views_cache=views_cache,
                avd_by_id=avd_by_id,
                public_ids_by_channel=public_ids,
                ops_privacy=ops_privacy,
                require_public=require_public,
            )
            if c:
                cands.append(c)
        cands = _prefer_live_library_paths(cands)
        cands.sort(
            key=lambda x: (float(x.rank_metric or 0.0), float(x.score)),
            reverse=True,
        )
        return _diversify_away_from(cands, deprioritize_paths)

    used_fallback = False
    if prefer_public:
        cands = _score_all(require_public=True)
        if not cands and allow_nonpublic_fallback:
            used_fallback = True
            cands = _score_all(require_public=None)
            for c in cands:
                if "public_pool_empty_fallback" not in c.reasons:
                    c.reasons = list(c.reasons) + ["public_pool_empty_fallback"]
            # Avoid Live thrash onto near-zero-view private/scheduled locals when
            # no public+local finals exist — prefer composite brand/quality score,
            # but never put near-zero **scheduled** junk in the Live head when
            # better private/hold/unknown locals exist.
            max_views = max((int(c.views or 0) for c in cands), default=0)
            if max_views < 50:

                def _near_zero_junk_privacy(c: VodCandidate) -> bool:
                    """Non-public pipeline states with near-zero views — bad Live heads."""
                    if int(c.views or 0) >= 50:
                        return False
                    junk_states = (
                        "privacy=scheduled:",
                        "privacy=hold:",
                        "privacy=farming:",
                        "privacy=queued:",
                        "privacy=composing:",
                        "privacy=uploading:",
                    )
                    for r in c.reasons or []:
                        s = str(r)
                        if s.startswith(junk_states):
                            return True
                    return False

                preferred = [c for c in cands if not _near_zero_junk_privacy(c)]
                demoted = [c for c in cands if _near_zero_junk_privacy(c)]
                pool = preferred if preferred else cands
                # Prefer prior featured Live head (smm_live_featured) when still
                # in the pool — restores proven heads after a bad fallback.
                featured_path = ""
                try:
                    feat = OPS / "smm_live_featured.json"
                    if feat.is_file():
                        raw = json.loads(feat.read_text(encoding="utf-8"))
                        ch_row = ((raw or {}).get("channels") or {}).get(channel) or {}
                        raw_feat = str(ch_row.get("featured_path") or "").strip()
                        if raw_feat:
                            fp = Path(raw_feat)
                            if fp.is_file():
                                featured_path = str(fp.resolve())
                except Exception:  # noqa: BLE001
                    featured_path = ""

                def _fallback_key(c: VodCandidate) -> tuple[float, float, float]:
                    feat_boost = (
                        1.0
                        if featured_path and c.path == featured_path
                        else 0.0
                    )
                    return (
                        feat_boost,
                        float(c.score),
                        float(c.rank_metric or 0.0),
                    )

                pool.sort(key=_fallback_key, reverse=True)
                for c in demoted:
                    if "fallback_demote_near_zero_scheduled" not in c.reasons:
                        c.reasons = list(c.reasons) + [
                            "fallback_demote_near_zero_scheduled"
                        ]
                cands = pool + (
                    [c for c in demoted if c not in pool] if preferred else []
                )
                cands = _diversify_away_from(cands, deprioritize_paths)
                for c in cands:
                    if "fallback_score_rank_low_views" not in c.reasons:
                        c.reasons = list(c.reasons) + [
                            "fallback_score_rank_low_views"
                        ]
                    if (
                        featured_path
                        and c.path == featured_path
                        and "fallback_prefer_prior_featured" not in c.reasons
                    ):
                        c.reasons = list(c.reasons) + [
                            "fallback_prefer_prior_featured"
                        ]
    else:
        cands = _score_all(require_public=None)

    # Attach pool metadata on the first candidate via reasons already; callers
    # that need pool stats can call public_videos_missing_local_final.
    out = cands[: max(1, lim)] if cands else []
    if out and used_fallback:
        # Mark on head so refresh_playlists / status can surface the fallback.
        head = out[0]
        if "public_fallback_used" not in head.reasons:
            head.reasons = list(head.reasons) + ["public_fallback_used"]
    return out


def rank_vods_with_meta(
    channel: str,
    *,
    limit: int | None = None,
    min_duration_sec: float | None = None,
    exclude_paths: set[str] | list[str] | None = None,
    deprioritize_paths: set[str] | list[str] | None = None,
    prefer_public: bool = True,
    allow_nonpublic_fallback: bool = True,
) -> dict[str, Any]:
    """Like ``rank_vods`` plus pool diagnostics for status / reports."""
    views_cache = _load_views_cache()
    ops_privacy = _load_ops_job_privacy()
    public_ids = _load_public_video_ids(
        views_cache=views_cache, ops_privacy=ops_privacy
    )
    publish_index = _load_publish_index()
    ranked = rank_vods(
        channel,
        limit=limit,
        min_duration_sec=min_duration_sec,
        exclude_paths=exclude_paths,
        deprioritize_paths=deprioritize_paths,
        prefer_public=prefer_public,
        allow_nonpublic_fallback=allow_nonpublic_fallback,
    )
    public_n = sum(1 for c in ranked if c.is_public)
    fallback_used = bool(ranked) and public_n == 0 and prefer_public
    missing = public_videos_missing_local_final(
        channel,
        publish_index=publish_index,
        public_ids_by_channel=public_ids,
        views_cache=views_cache,
    )
    return {
        "channel": channel,
        "candidates": ranked,
        "public_in_playlist": public_n,
        "public_pool_size": len(public_ids.get(channel) or set()),
        "public_fallback_used": fallback_used,
        "public_without_local_final": missing,
        "prefer_public": prefer_public,
        "allow_nonpublic_fallback": allow_nonpublic_fallback,
    }


def playlist_path(channel: str) -> Path:
    return STREAM_CFG / f"playlist_{channel}.txt"


def read_playlist_entries(channel: str) -> list[str]:
    """Absolute media paths currently listed in the channel concat playlist."""
    pl = playlist_path(channel)
    if not pl.is_file():
        return []
    out: list[str] = []
    for ln in pl.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if not s.lower().startswith("file "):
            continue
        raw = s[5:].strip().strip("'").strip('"')
        p = Path(raw)
        if not p.is_absolute():
            p = (pl.parent / p).resolve()
        else:
            p = p.resolve()
        if p.is_file():
            out.append(str(p))
    return out


def write_playlist(
    channel: str,
    candidates: list[VodCandidate],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel: {channel}")
    pl = playlist_path(channel)
    lines = [
        f"# Auto-written by vod_picker {_utc_now()} channel={channel}",
        "# Own content only. Absolute paths. Head = featured Live VOD "
        "(infinite -stream_loop -1); tails = next views×AVD candidates.",
    ]
    paths: list[str] = []
    for c in candidates:
        p = Path(c.path)
        if not p.is_file():
            continue
        abs_p = p.resolve().as_posix()
        lines.append(f"file '{abs_p}'")
        paths.append(abs_p)
    if not paths:
        return {
            "ok": False,
            "channel": channel,
            "playlist": str(pl),
            "written": False,
            "error": "no_candidates",
        }
    body = "\n".join(lines) + "\n"
    if not dry_run:
        STREAM_CFG.mkdir(parents=True, exist_ok=True)
        pl.write_text(body, encoding="utf-8")
    path_set = set(paths)
    return {
        "ok": True,
        "channel": channel,
        "playlist": str(pl),
        "written": not dry_run,
        "entries": paths,
        "scores": [asdict(c) for c in candidates if c.path in path_set],
    }


def refresh_playlists(
    *,
    channels: tuple[str, ...] | list[str] = CHANNELS,
    limit: int | None = None,
    dry_run: bool = False,
    min_duration_sec: float | None = None,
    exclude_paths_by_channel: dict[str, list[str] | set[str]] | None = None,
    exclude_current: bool = False,
    diversify_channels: bool = True,
    prefer_public: bool = True,
    allow_nonpublic_fallback: bool = True,
) -> dict[str, Any]:
    """Rewrite playlists toward top views×AVD% **public** longform locals (per channel).

    ``prefer_public`` restricts the pool to channel-matched locals whose
    ``video_id`` is public on YouTube (OpsStore public and/or views-cache
    channel listing). Private/scheduled/hold are used only when that pool is
    empty and ``allow_nonpublic_fallback`` is True (Live must not blank).

    ``exclude_current`` drops the current playlist head so underperforming Live
    VOD can rotate to next-best without inventing third-party media.

    When multiple channels refresh together, later channels soft-deprioritize
    earlier channels' new heads / top stack so pools diverge when alternatives
    exist (still views×AVD first among remaining; views fallback if AVD missing).

    If excludes empty the candidate pool (or leave no writable files), clears the
    exclude set and re-ranks from scratch so Live never blanks — both channels.

    Honors ``output/ops/live_playlist_force_lock.json`` when ``locked=true``:
    rewrites each locked channel to the pinned head and skips auto-rank.
    """
    OPS.mkdir(parents=True, exist_ok=True)
    lim = default_pick_limit() if limit is None else int(limit)
    results: dict[str, Any] = {}
    exclude_map = dict(exclude_paths_by_channel or {})
    # Paths already featured (or near-head) on a sibling channel this refresh.
    sibling_stack: list[str] = []
    chan_list = [str(c) for c in channels]

    library_payload: dict[str, Any] | None = None
    if not dry_run:
        try:
            from src.streaming.live_vod_library import (
                ensure_all_channel_libraries,
                library_enabled,
            )

            if library_enabled():
                library_payload = ensure_all_channel_libraries(
                    channels=chan_list,
                    dry_run=False,
                )
        except Exception as exc:  # noqa: BLE001
            library_payload = {
                "ok": False,
                "error": str(exc)[:400],
                "module": "live_vod_library",
            }

    force_lock = read_playlist_force_lock()
    if force_lock:
        heads = force_lock.get("heads") or {}
        reason = str(force_lock.get("reason") or "force_lock")
        for ch in chan_list:
            head = heads.get(ch)
            if not head:
                continue
            # Keep a few non-excluded tails for soft-swap candidates after unlock,
            # but head stays forced for Live.
            tails: list[str] = []
            try:
                ranked_tail = rank_vods(
                    ch,
                    limit=lim,
                    min_duration_sec=min_duration_sec,
                    exclude_paths=[head, *(exclude_map.get(ch) or [])],
                    prefer_public=prefer_public,
                    allow_nonpublic_fallback=allow_nonpublic_fallback,
                )
                tails = [c.path for c in ranked_tail if c.path != str(Path(head).resolve())][
                    : max(0, lim - 1)
                ]
            except Exception:
                tails = []
            row = write_forced_playlist(
                ch, head, tails=tails, dry_run=dry_run, reason=reason
            )
            row["forced_lock"] = True
            results[ch] = row
        payload = {
            "ok": all(bool(v.get("ok")) for v in results.values()) if results else False,
            "ts": _utc_now(),
            "module": "vod_picker",
            "dry_run": dry_run,
            "limit": lim,
            "exclude_current": exclude_current,
            "diversify_channels": diversify_channels,
            "prefer_public": prefer_public,
            "allow_nonpublic_fallback": allow_nonpublic_fallback,
            "force_lock": True,
            "force_lock_reason": reason,
            "live_vod_library": library_payload,
            "channels": results,
            "policy": {
                "own_content_only": True,
                "prefer_public_uploads": prefer_public,
                "nonpublic_fallback": allow_nonpublic_fallback,
                "force_lock": True,
                "note": "live_playlist_force_lock active — auto most-viewed rank bypassed.",
            },
        }
        if not dry_run:
            PICKER_STATUS.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    for ch in chan_list:
        excluded: set[str] = set()
        for p in exclude_map.get(ch) or []:
            excluded.add(str(Path(p).resolve()))
        if exclude_current:
            cur = read_playlist_entries(ch)
            if cur:
                excluded.add(str(Path(cur[0]).resolve()))
        excluded_attempted = sorted(excluded)

        deprioritize: set[str] = set()
        if diversify_channels:
            deprioritize.update(sibling_stack)
            # Single-channel historian refresh: still dodge napstorian's Live head.
            if not sibling_stack and ch == "napping_historian":
                nap_entries = read_playlist_entries("napstorian")
                for p in nap_entries[:3]:
                    deprioritize.add(str(Path(p).resolve()))

        meta_ranked = rank_vods(
            ch,
            limit=lim,
            min_duration_sec=min_duration_sec,
            exclude_paths=excluded,
            deprioritize_paths=deprioritize,
            prefer_public=prefer_public,
            allow_nonpublic_fallback=allow_nonpublic_fallback,
        )
        ranked = list(meta_ranked)
        excludes_cleared = False
        # Exhaustion: no remaining candidates after excludes → clear set, re-rank #1.
        if not ranked and excluded:
            excluded = set()
            excludes_cleared = True
            ranked = rank_vods(
                ch,
                limit=lim,
                min_duration_sec=min_duration_sec,
                deprioritize_paths=deprioritize,
                prefer_public=prefer_public,
                allow_nonpublic_fallback=allow_nonpublic_fallback,
            )
        row = write_playlist(ch, ranked, dry_run=dry_run)
        # Also recover if ranked paths were unwritable while excludes were active.
        if not row.get("ok") and excluded_attempted and not excludes_cleared:
            excluded = set()
            excludes_cleared = True
            ranked = rank_vods(
                ch,
                limit=lim,
                min_duration_sec=min_duration_sec,
                deprioritize_paths=deprioritize,
                prefer_public=prefer_public,
                allow_nonpublic_fallback=allow_nonpublic_fallback,
            )
            row = write_playlist(ch, ranked, dry_run=dry_run)
        row["excluded"] = excluded_attempted
        row["excludes_cleared"] = excludes_cleared
        row["exclusion_exhausted"] = excludes_cleared
        row["deprioritized"] = sorted(deprioritize)
        public_n = sum(1 for c in ranked if getattr(c, "is_public", False))
        row["public_in_playlist"] = public_n
        row["public_fallback_used"] = bool(
            prefer_public and ranked and public_n == 0
        )
        try:
            pub_ids = _load_public_video_ids()
            row["public_pool_size"] = len(pub_ids.get(ch) or set())
            row["public_without_local_final"] = public_videos_missing_local_final(
                ch, public_ids_by_channel=pub_ids
            )[:8]
        except Exception:
            row["public_pool_size"] = 0
            row["public_without_local_final"] = []
        results[ch] = row

        # Grow sibling stack: head + next few so Anne-top-3 does not clone.
        entries = list(row.get("entries") or [])
        take = min(3, max(1, len(entries) // 2 + 1))
        for p in entries[:take]:
            if p not in sibling_stack:
                sibling_stack.append(p)

    payload = {
        "ok": all(bool(v.get("ok")) for v in results.values()) if results else False,
        "ts": _utc_now(),
        "module": "vod_picker",
        "dry_run": dry_run,
        "limit": lim,
        "exclude_current": exclude_current,
        "diversify_channels": diversify_channels,
        "prefer_public": prefer_public,
        "allow_nonpublic_fallback": allow_nonpublic_fallback,
        "live_vod_library": library_payload,
        "channels": results,
        "policy": {
            "own_content_only": True,
            "prefer_longform_over_shorts": True,
            "prefer_public_uploads": prefer_public,
            "nonpublic_fallback": allow_nonpublic_fallback,
            "rank_order": "views_times_avd_pct",
            "rank_fallback": "views_when_avd_missing",
            "channel_brand_hints": True,
            "sibling_head_diversity": diversify_channels,
            "live_vod_library": "output/live_vods/<channel>/<video_id>/final.mp4",
            "pick_limit_default": DEFAULT_PICK_LIMIT,
            "longform_min_duration_sec": float(
                min_duration_sec
                if min_duration_sec is not None
                else float(os.getenv("VOD_PICKER_MIN_DURATION_SEC") or 180)
            ),
            "note": (
                "Source pool prefers **public** YT uploads for that channel with a "
                "local final under output/live_vods (downloaded top-N) or "
                "output/jobs. Rank = views×AVD% (views fallback). Sibling Brand "
                "VODs hard-excluded. Live library downloads run before refresh so "
                "encode does not depend on farm finals surviving post-public purge. "
                "If the public+local pool is empty, fall back to private/scheduled/"
                "hold channel-owned locals so Live encode never blanks."
            ),
            "hard_channel_isolation": True,
        },
    }
    if not dry_run:
        PICKER_STATUS.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload
