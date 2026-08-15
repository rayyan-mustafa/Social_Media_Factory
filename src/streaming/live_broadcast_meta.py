"""Reuse featured VOD title/description/thumbnail on the bound Live broadcast.

When Live starts, the featured playlist head changes, or SMM soft-swaps VODs,
copy packaging from the source job (youtube_meta / publish_manifest) onto the
channel's active liveBroadcast via YouTube Data API.

Encode / RTMP health is never blocked: all API work is best-effort, idempotent,
and cooldown-gated. Thumbnail is attached only when a usable local image exists
(or one was downloaded from the source VOD).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "output" / "ops"
JOBS = ROOT / "output" / "jobs"
STATE_PATH = OPS / "live_broadcast_meta.json"
BROADCAST_IDS_PATH = OPS / "live_broadcast_ids.json"

# YouTube snippet limits
_TITLE_MAX = 100
_DESC_MAX = 5000

# Default: do not re-apply same featured packaging more often than this.
_DEFAULT_COOLDOWN_SEC = 600.0

_IMG_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cooldown_sec() -> float:
    raw = (os.getenv("LIVE_BROADCAST_META_COOLDOWN_SEC") or "").strip()
    if raw:
        try:
            return max(30.0, float(raw))
        except (TypeError, ValueError):
            pass
    return _DEFAULT_COOLDOWN_SEC


def _sha1_text(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def job_dir_from_media_path(media_path: Path | str | None) -> Path | None:
    """Resolve packaging dir from a featured final.mp4 path.

    Supports farm jobs (``…/output/jobs/<job>/…``) and the Live library
    (``…/output/live_vods/<channel>/<video_id>/final.mp4``).
    """
    if not media_path:
        return None
    try:
        parts = Path(media_path).resolve().parts
    except OSError:
        parts = Path(str(media_path)).parts
    if "live_vods" in parts:
        lidx = parts.index("live_vods")
        if lidx + 2 < len(parts):
            return Path(*parts[: lidx + 3])
        return None
    if "jobs" not in parts:
        return None
    jidx = parts.index("jobs")
    if jidx + 1 >= len(parts):
        return None
    return Path(*parts[: jidx + 2])


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_state() -> dict[str, Any]:
    data = _load_json(STATE_PATH)
    data.setdefault("channels", {})
    data.setdefault("module", "live_broadcast_meta")
    return data


def save_state(state: dict[str, Any]) -> None:
    state = dict(state)
    state["updated_at"] = _utc_now()
    state["module"] = "live_broadcast_meta"
    _atomic_write_json(STATE_PATH, state)


def load_broadcast_ids() -> dict[str, str]:
    """Map channel → known live broadcast / video id (ops cache)."""
    data = _load_json(BROADCAST_IDS_PATH)
    out: dict[str, str] = {}
    channels = data.get("channels") if isinstance(data.get("channels"), dict) else data
    if isinstance(channels, dict):
        for ch, row in channels.items():
            if isinstance(row, dict):
                bid = str(row.get("broadcast_id") or row.get("video_id") or "").strip()
            else:
                bid = str(row or "").strip()
            if bid:
                out[str(ch)] = bid
    # Seed from historian rotate note if present.
    rotate = _load_json(OPS / "historian_live_stream_rotate.json")
    hist = str(rotate.get("broadcast_id") or "").strip()
    if hist and "napping_historian" not in out:
        out["napping_historian"] = hist
    return out


def remember_broadcast_id(channel: str, broadcast_id: str) -> None:
    bid = (broadcast_id or "").strip()
    if not bid:
        return
    data = _load_json(BROADCAST_IDS_PATH)
    channels = dict(data.get("channels") or {})
    prev = channels.get(channel) if isinstance(channels.get(channel), dict) else {}
    row = dict(prev) if isinstance(prev, dict) else {}
    row["broadcast_id"] = bid
    row["updated_at"] = _utc_now()
    channels[channel] = row
    _atomic_write_json(
        BROADCAST_IDS_PATH,
        {
            "updated_at": _utc_now(),
            "module": "live_broadcast_ids",
            "channels": channels,
            "note": "Active Live broadcast ids per Brand Account (no secrets).",
        },
    )


def _usable_image(path: Path | None) -> Path | None:
    if path is None:
        return None
    try:
        p = Path(path)
        if not p.is_file():
            return None
        if p.suffix.lower() not in _IMG_SUFFIXES:
            return None
        if p.stat().st_size < 1024:
            return None
        return p.resolve()
    except OSError:
        return None


def _find_local_thumbnail(job_dir: Path) -> Path | None:
    """Prefer youtube_meta thumb, then publish path, then thumbnails/."""
    meta_dir = job_dir / "youtube_meta"
    for name in ("thumbnail.jpg", "thumbnail.jpeg", "thumbnail.png", "thumbnail.webp"):
        hit = _usable_image(meta_dir / name)
        if hit:
            return hit

    for man_name in ("publish_manifest.json", "pipeline_manifest.json"):
        man = _load_json(job_dir / man_name)
        for key in ("thumbnail_path", "thumb_path", "thumbnail"):
            raw = man.get(key)
            if raw:
                hit = _usable_image(Path(str(raw)))
                if hit:
                    return hit
        meta = man.get("meta") if isinstance(man.get("meta"), dict) else {}
        for key in ("thumbnail_path", "thumb_path", "thumbnail"):
            raw = meta.get(key)
            if raw:
                hit = _usable_image(Path(str(raw)))
                if hit:
                    return hit

    thumbs = job_dir / "thumbnails"
    if thumbs.is_dir():
        preferred = [
            "selected.jpg",
            "selected.jpeg",
            "selected.png",
            "winner.jpg",
            "thumbnail.jpg",
            "thumb.jpg",
            "final.jpg",
        ]
        for name in preferred:
            hit = _usable_image(thumbs / name)
            if hit:
                return hit
        # First usable image in thumbnails/ (not nested scene dumps).
        for p in sorted(thumbs.iterdir()):
            if p.is_file():
                hit = _usable_image(p)
                if hit:
                    return hit
    return None


def select_featured_vod_meta(
    featured_path: Path | str | None,
    *,
    youtube_fetch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pick title/description/thumbnail for a featured VOD path (no API side effects).

    Priority for title/description:
      1) youtube_meta/
      2) publish_manifest.json
      3) pipeline_manifest / script.json title (+ empty or topic desc)
      4) optional youtube_fetch dict from source video_id (title/description)
      5) job folder name heuristic

    Thumbnail only when a usable local image is found (caller may add downloaded).
    """
    out: dict[str, Any] = {
        "ok": False,
        "featured_path": str(featured_path) if featured_path else None,
        "job_dir": None,
        "source_video_id": None,
        "title": None,
        "description": None,
        "thumbnail_path": None,
        "title_src": None,
        "description_src": None,
        "thumbnail_src": None,
    }
    job_dir = job_dir_from_media_path(featured_path)
    if job_dir is None or not job_dir.is_dir():
        out["error"] = "job_dir_unresolved"
        return out
    out["job_dir"] = str(job_dir.resolve())

    # Source VOD id (uploaded longform), not the Live broadcast id.
    for man_name in ("publish_manifest.json", "pipeline_manifest.json"):
        man = _load_json(job_dir / man_name)
        vid = str(man.get("video_id") or "").strip()
        if vid and vid.lower() not in {"none", "null"}:
            out["source_video_id"] = vid
            break

    title = ""
    description = ""
    title_src = ""
    desc_src = ""

    meta_dir = job_dir / "youtube_meta"
    if meta_dir.is_dir():
        try:
            from src.services.youtube_meta import load_youtube_meta

            pack = load_youtube_meta(meta_dir)
            title = str(pack.get("title") or "").strip()
            description = str(pack.get("description") or "").strip()
            if title:
                title_src = "youtube_meta"
            if description:
                desc_src = "youtube_meta"
            thumb = pack.get("thumbnail_path")
            hit = _usable_image(Path(str(thumb)) if thumb else None)
            if hit:
                out["thumbnail_path"] = str(hit)
                out["thumbnail_src"] = "youtube_meta"
        except Exception as exc:  # noqa: BLE001
            logger.info("live meta youtube_meta load failed %s: %s", meta_dir, exc)

    pub = _load_json(job_dir / "publish_manifest.json")
    if not title:
        title = str(pub.get("title") or "").strip()
        if title:
            title_src = "publish_manifest"
    if not description:
        description = str(pub.get("description") or "").strip()
        if description:
            desc_src = "publish_manifest"

    pipe = _load_json(job_dir / "pipeline_manifest.json")
    if not title:
        title = str(pipe.get("title") or pipe.get("topic") or "").strip()
        if title:
            title_src = "pipeline_manifest"
    if not description:
        topic = str(pipe.get("topic") or "").strip()
        if topic:
            description = topic
            desc_src = "pipeline_topic"

    script = _load_json(job_dir / "script" / "script.json")
    if not title:
        title = str(script.get("title") or script.get("topic") or "").strip()
        if title:
            title_src = "script"
    if not description:
        hook = str(script.get("hook") or "").strip()
        topic = str(script.get("topic") or "").strip()
        if hook or topic:
            description = "\n\n".join(x for x in (hook, topic) if x)
            desc_src = "script"

    yf = youtube_fetch if isinstance(youtube_fetch, dict) else None
    if yf:
        if not title:
            title = str(yf.get("title") or "").strip()
            if title:
                title_src = "youtube_source_video"
        if not description:
            description = str(yf.get("description") or "").strip()
            if description:
                desc_src = "youtube_source_video"
        if not out.get("thumbnail_path"):
            # Caller may pass a downloaded local path under thumbnail_path.
            hit = _usable_image(
                Path(str(yf["thumbnail_path"])) if yf.get("thumbnail_path") else None
            )
            if hit:
                out["thumbnail_path"] = str(hit)
                out["thumbnail_src"] = str(yf.get("thumbnail_src") or "youtube_download")

    if not out.get("thumbnail_path"):
        hit = _find_local_thumbnail(job_dir)
        if hit:
            out["thumbnail_path"] = str(hit)
            out["thumbnail_src"] = out.get("thumbnail_src") or "local_thumbnails"

    if not title:
        # Folder heuristic: strip timestamp prefix.
        name = job_dir.name
        name = re.sub(r"^\d{8}T\d{6}Z_", "", name)
        title = name.replace("_", " ").strip()
        title_src = "job_folder"

    title = (title or "")[:_TITLE_MAX].strip()
    description = (description or "")[:_DESC_MAX].strip()
    out["title"] = title or None
    out["description"] = description or None
    out["title_src"] = title_src or None
    out["description_src"] = desc_src or None
    out["ok"] = bool(title)
    if not out["ok"]:
        out["error"] = "no_title"
    return out


def featured_path_for_channel(channel: str) -> str | None:
    """Playlist head for channel (featured Live VOD)."""
    try:
        from src.streaming.vod_loop import _playlist_featured_media

        feat = _playlist_featured_media(channel)
        return str(feat.resolve()) if feat is not None else None
    except Exception:  # noqa: BLE001
        pass
    try:
        from src.streaming.vod_picker import read_playlist_entries

        entries = read_playlist_entries(channel)
        if entries:
            return str(Path(entries[0]).resolve())
    except Exception:  # noqa: BLE001
        pass
    featured = _load_json(OPS / "smm_live_featured.json")
    row = (featured.get("channels") or {}).get(channel) or {}
    path = str(row.get("featured_path") or "").strip()
    return path or None


def _should_skip_idempotent(
    channel: str,
    *,
    featured_path: str | None,
    meta: dict[str, Any],
    force: bool,
    state: dict[str, Any],
) -> tuple[bool, str]:
    if force:
        return False, "force"
    row = (state.get("channels") or {}).get(channel) or {}
    last_feat = str(row.get("featured_path") or "")
    last_title = str(row.get("title") or "")
    last_desc_h = str(row.get("description_hash") or "")
    last_thumb = str(row.get("thumbnail_path") or "")
    last_at = str(row.get("applied_at") or row.get("last_attempt_at") or "")
    same_feat = bool(
        featured_path
        and last_feat
        and str(Path(last_feat)) == str(Path(featured_path))
    )
    same_pack = (
        same_feat
        and last_title == str(meta.get("title") or "")
        and last_desc_h == _sha1_text(str(meta.get("description") or ""))
        and last_thumb == str(meta.get("thumbnail_path") or "")
        and bool(row.get("ok"))
    )
    if same_pack and last_at:
        try:
            prev = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - prev).total_seconds()
            cool = _cooldown_sec()
            if age < cool:
                return True, f"cooldown_{int(cool - age)}s_same_pack"
            # Same pack past cooldown: still skip (already applied).
            return True, "already_applied_same_pack"
        except ValueError:
            return True, "already_applied_same_pack"
    if same_pack:
        return True, "already_applied_same_pack"
    # Different pack but very recent attempt → soft cooldown (anti spam).
    if last_at and not same_feat:
        try:
            prev = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - prev).total_seconds()
            # Allow featured swaps immediately; only gate identical re-tries.
            if age < 15.0 and same_feat:
                return True, "rapid_retry"
        except ValueError:
            pass
    return False, "apply"


def resolve_active_broadcast_id(
    youtube,
    channel: str,
    *,
    prefer_cached: bool = True,
) -> dict[str, Any]:
    """Find the channel's active/upcoming bound liveBroadcast id.

    Note: YouTube rejects ``mine`` + ``broadcastStatus`` together — use
    ``broadcastStatus`` alone (auth scopes the Brand Account), ``id``, or
    ``mine`` with a client-side lifeCycle filter. Never combine those filters.
    """
    cached = load_broadcast_ids().get(channel) if prefer_cached else None
    tried: list[str] = []
    items: list[dict[str, Any]] = []
    list_errors: list[str] = []

    def _get_by_id(bid: str) -> dict[str, Any] | None:
        tried.append(f"id={bid}")
        resp = (
            youtube.liveBroadcasts()
            .list(
                part="id,snippet,status,contentDetails",
                id=bid,
            )
            .execute()
        )
        items_local = list(resp.get("items") or [])
        return items_local[0] if items_local else None

    def _list_by_status(status: str) -> list[dict[str, Any]]:
        tried.append(f"broadcastStatus={status}")
        resp = (
            youtube.liveBroadcasts()
            .list(
                part="id,snippet,status,contentDetails",
                broadcastStatus=status,
                broadcastType="all",
                maxResults=25,
            )
            .execute()
        )
        return list(resp.get("items") or [])

    def _list_mine() -> list[dict[str, Any]]:
        tried.append("mine=true")
        resp = (
            youtube.liveBroadcasts()
            .list(
                part="id,snippet,status,contentDetails",
                mine=True,
                maxResults=50,
            )
            .execute()
        )
        return list(resp.get("items") or [])

    # Validate ops-cached broadcast id first (id filter — no status/mine combo).
    if cached:
        try:
            hit = _get_by_id(cached)
            if hit is not None:
                life = (hit.get("status") or {}).get("lifeCycleStatus")
                if str(life or "") not in {"complete", "revoked", "abandoned"}:
                    snip = hit.get("snippet") or {}
                    remember_broadcast_id(channel, cached)
                    return {
                        "ok": True,
                        "channel": channel,
                        "broadcast_id": cached,
                        "source": "ops_cache_validated",
                        "lifeCycleStatus": life,
                        "title": snip.get("title"),
                        "description": snip.get("description"),
                        "scheduledStartTime": snip.get("scheduledStartTime"),
                        "boundStreamId": (hit.get("contentDetails") or {}).get(
                            "boundStreamId"
                        ),
                        "tried": tried,
                    }
        except Exception as exc:  # noqa: BLE001
            list_errors.append(f"id:{exc}")

    for st in ("active", "upcoming"):
        try:
            items.extend(_list_by_status(st))
        except Exception as exc:  # noqa: BLE001
            list_errors.append(f"{st}:{exc}")

    if not items:
        try:
            items.extend(_list_mine())
        except Exception as exc:  # noqa: BLE001
            list_errors.append(f"mine:{exc}")

    # Prefer lifeCycleStatus live / ready / testing with bound stream.
    def _rank(it: dict[str, Any]) -> tuple[int, str]:
        status = (it.get("status") or {}).get("lifeCycleStatus") or ""
        bound = (it.get("contentDetails") or {}).get("boundStreamId") or ""
        rank = {
            "live": 0,
            "liveStarting": 1,
            "ready": 2,
            "testing": 3,
            "testStarting": 4,
            "created": 5,
        }.get(str(status), 9)
        if str(status) in {"complete", "revoked", "abandoned"}:
            rank += 50
        if not bound:
            rank += 10
        return (rank, str(it.get("id") or ""))

    # Drop completed/abandoned unless nothing else exists.
    liveish = [
        it
        for it in items
        if str((it.get("status") or {}).get("lifeCycleStatus") or "")
        not in {"complete", "revoked", "abandoned"}
    ]
    pool = liveish or items
    items_sorted = sorted(pool, key=_rank)
    chosen = None
    if cached:
        for it in items_sorted:
            if str(it.get("id") or "") == cached:
                chosen = it
                break
    if chosen is None and items_sorted:
        chosen = items_sorted[0]

    if chosen is None and cached:
        # Cache may still be valid even if list filtered oddly / API quirk.
        return {
            "ok": True,
            "channel": channel,
            "broadcast_id": cached,
            "source": "ops_cache",
            "lifeCycleStatus": None,
            "tried": tried,
            "list_errors": list_errors[:3] or None,
        }

    if chosen is None:
        # Last resort: public search for channel's live video id.
        search_id = _search_live_video_id(channel)
        if search_id:
            remember_broadcast_id(channel, search_id)
            return {
                "ok": True,
                "channel": channel,
                "broadcast_id": search_id,
                "source": "search_live",
                "lifeCycleStatus": "live",
                "tried": tried + ["search.list"],
            }
        return {
            "ok": False,
            "channel": channel,
            "error": "no_active_or_upcoming_broadcast",
            "tried": tried,
            "cached": cached,
            "list_errors": list_errors[:3] or None,
        }

    bid = str(chosen.get("id") or "").strip()
    if bid:
        remember_broadcast_id(channel, bid)
    snip = chosen.get("snippet") or {}
    return {
        "ok": True,
        "channel": channel,
        "broadcast_id": bid,
        "source": "liveBroadcasts.list",
        "lifeCycleStatus": (chosen.get("status") or {}).get("lifeCycleStatus"),
        "title": snip.get("title"),
        "description": snip.get("description"),
        "scheduledStartTime": snip.get("scheduledStartTime"),
        "boundStreamId": (chosen.get("contentDetails") or {}).get("boundStreamId"),
        "tried": tried,
    }


def _search_live_video_id(channel: str) -> str | None:
    """Resolve currently live video id via API key search (best-effort)."""
    try:
        from src.agents.smm_agent import SocialMediaManager

        smm = SocialMediaManager()
        channel_id = smm._youtube_channel_id_for(channel)
        api_key = getattr(smm.s, "youtube_api_key", None) or ""
        if not channel_id or not api_key:
            return None
        import httpx

        search = httpx.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "id",
                "channelId": channel_id,
                "eventType": "live",
                "type": "video",
                "maxResults": 3,
                "key": api_key,
            },
            timeout=20.0,
        )
        if search.status_code != 200:
            return None
        for it in search.json().get("items") or []:
            vid = (it.get("id") or {}).get("videoId")
            if vid:
                return str(vid)
    except Exception as exc:  # noqa: BLE001
        logger.info("live video search failed ch=%s: %s", channel, exc)
    return None


def _fetch_source_video_snippet(
    youtube, video_id: str
) -> dict[str, Any] | None:
    if not video_id:
        return None
    try:
        resp = (
            youtube.videos()
            .list(part="snippet", id=video_id)
            .execute()
        )
        items = resp.get("items") or []
        if not items:
            return None
        snip = items[0].get("snippet") or {}
        return {
            "title": snip.get("title"),
            "description": snip.get("description"),
            "thumbnails": snip.get("thumbnails") or {},
        }
    except Exception as exc:  # noqa: BLE001
        logger.info("source video snippet fetch failed %s: %s", video_id, exc)
        return None


def _download_yt_thumbnail(
    thumbnails: dict[str, Any],
    *,
    dest_dir: Path,
) -> Path | None:
    """Download highest-res YouTube thumb into dest_dir if network available."""
    order = ("maxres", "standard", "high", "medium", "default")
    url = None
    for key in order:
        row = thumbnails.get(key) if isinstance(thumbnails, dict) else None
        if isinstance(row, dict) and row.get("url"):
            url = str(row["url"])
            break
    if not url:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "yt_source_thumb.jpg"
    try:
        import httpx

        r = httpx.get(url, timeout=30.0, follow_redirects=True)
        if r.status_code != 200 or len(r.content) < 1024:
            return None
        dest.write_bytes(r.content)
        return _usable_image(dest)
    except Exception as exc:  # noqa: BLE001
        logger.info("yt thumb download failed: %s", exc)
        return None


def _build_youtube(channel: str):
    from src.services.publish_youtube import PublishModule

    return PublishModule(channel=channel)._build_youtube_client()


def _apply_snippet_and_thumb(
    youtube,
    *,
    broadcast_id: str,
    title: str,
    description: str,
    thumbnail_path: Path | None,
    scheduled_start_time: str | None,
) -> dict[str, Any]:
    """Update Live broadcast packaging.

    Prefer ``videos.update`` for already-live broadcasts (avoids
    scheduledStartTime 403 on ``liveBroadcasts.update``). Fall back to
    ``liveBroadcasts.update`` for upcoming / unbound cases.
    """
    out: dict[str, Any] = {
        "broadcast_id": broadcast_id,
        "snippet_via": None,
        "thumbnail_uploaded": False,
    }
    title = (title or "")[:_TITLE_MAX]
    description = (description or "")[:_DESC_MAX]

    # 1) videos.update — reliable for live video ids
    try:
        existing = (
            youtube.videos()
            .list(part="snippet", id=broadcast_id)
            .execute()
            .get("items")
            or []
        )
        if existing:
            snip = dict(existing[0].get("snippet") or {})
            snip["title"] = title
            snip["description"] = description
            if not snip.get("categoryId"):
                snip["categoryId"] = "27"  # Education — history docs default
            youtube.videos().update(
                part="snippet", body={"id": broadcast_id, "snippet": snip}
            ).execute()
            out["snippet_via"] = "videos.update"
    except Exception as exc:  # noqa: BLE001
        out["videos_update_error"] = str(exc)[:240]

    # 2) liveBroadcasts.update fallback (upcoming / when videos.update missed)
    if out.get("snippet_via") is None:
        try:
            existing = (
                youtube.liveBroadcasts()
                .list(part="snippet", id=broadcast_id)
                .execute()
                .get("items")
                or []
            )
            snip = dict((existing[0].get("snippet") or {}) if existing else {})
            start = scheduled_start_time or snip.get("scheduledStartTime")
            if not start:
                start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            body = {
                "id": broadcast_id,
                "snippet": {
                    "title": title,
                    "description": description,
                    "scheduledStartTime": start,
                },
            }
            youtube.liveBroadcasts().update(part="snippet", body=body).execute()
            out["snippet_via"] = "liveBroadcasts.update"
            out["scheduledStartTime"] = start
        except Exception as exc:  # noqa: BLE001
            out["liveBroadcasts_error"] = str(exc)[:240]
            out["ok"] = False
            return out

    if thumbnail_path is not None:
        thumb = _usable_image(Path(thumbnail_path))
        if thumb is not None:
            try:
                from googleapiclient.http import MediaFileUpload

                suffix = thumb.suffix.lower()
                mime = {
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".png": "image/png",
                    ".webp": "image/webp",
                    ".gif": "image/gif",
                }.get(suffix, "image/jpeg")
                media = MediaFileUpload(str(thumb), mimetype=mime, resumable=False)
                youtube.thumbnails().set(
                    videoId=broadcast_id, media_body=media
                ).execute()
                out["thumbnail_uploaded"] = True
                out["thumbnail_path"] = str(thumb)
            except Exception as exc:  # noqa: BLE001
                out["thumbnail_error"] = str(exc)[:240]
        else:
            out["thumbnail_skipped"] = "unusable_image"
    else:
        out["thumbnail_skipped"] = "no_local_thumb"

    out["ok"] = out.get("snippet_via") is not None
    out["title"] = title
    return out


def sync_channel_live_meta(
    channel: str,
    *,
    featured_path: str | Path | None = None,
    force: bool = False,
    dry_run: bool = False,
    allow_yt_thumb_download: bool = True,
) -> dict[str, Any]:
    """Apply featured VOD packaging onto the channel's Live broadcast (idempotent)."""
    from src.services.youtube_channel_auth import normalize_youtube_channel

    ch = normalize_youtube_channel(channel)
    feat = str(featured_path) if featured_path else featured_path_for_channel(ch)
    result: dict[str, Any] = {
        "ok": False,
        "channel": ch,
        "featured_path": feat,
        "action": None,
        "dry_run": dry_run,
        "ts": _utc_now(),
    }
    if not feat:
        result["action"] = "skipped_no_featured"
        result["error"] = "no_featured_path"
        return result

    state = load_state()
    # Pre-select without network for idempotency check.
    meta = select_featured_vod_meta(feat)
    skip, skip_why = _should_skip_idempotent(
        ch, featured_path=feat, meta=meta, force=force, state=state
    )
    if skip:
        result["ok"] = True
        result["action"] = "skipped_idempotent"
        result["skip_reason"] = skip_why
        result["meta"] = {
            "title": meta.get("title"),
            "title_src": meta.get("title_src"),
            "thumbnail_path": meta.get("thumbnail_path"),
            "thumbnail_src": meta.get("thumbnail_src"),
        }
        return result

    if dry_run:
        result["ok"] = True
        result["action"] = "dry_run"
        result["meta"] = meta
        return result

    try:
        youtube = _build_youtube(ch)
    except Exception as exc:  # noqa: BLE001
        result["action"] = "auth_error"
        result["error"] = str(exc)[:240]
        _record_attempt(state, ch, result, meta, feat, ok=False)
        return result

    # Enrich from source VOD on YouTube when local desc/title thin / no thumb.
    src_id = str(meta.get("source_video_id") or "").strip()
    yt_fetch: dict[str, Any] | None = None
    if src_id and (
        not meta.get("description")
        or meta.get("description_src") in {"pipeline_topic", "script", None}
        or (not meta.get("thumbnail_path") and allow_yt_thumb_download)
    ):
        snip = _fetch_source_video_snippet(youtube, src_id)
        if snip:
            yt_fetch = {
                "title": snip.get("title"),
                "description": snip.get("description"),
            }
            if allow_yt_thumb_download and not meta.get("thumbnail_path"):
                job_dir = Path(str(meta.get("job_dir") or ""))
                dest = (
                    job_dir / "youtube_meta"
                    if job_dir.is_dir()
                    else OPS / "live_thumbs" / ch
                )
                dl = _download_yt_thumbnail(
                    snip.get("thumbnails") or {}, dest_dir=dest
                )
                if dl:
                    yt_fetch["thumbnail_path"] = str(dl)
                    yt_fetch["thumbnail_src"] = "youtube_download"
            meta = select_featured_vod_meta(feat, youtube_fetch=yt_fetch)

    if not meta.get("ok") or not meta.get("title"):
        result["action"] = "skipped_no_meta"
        result["error"] = meta.get("error") or "no_title"
        result["meta"] = meta
        _record_attempt(state, ch, result, meta, feat, ok=False)
        return result

    bcast = resolve_active_broadcast_id(youtube, ch)
    if not bcast.get("ok") or not bcast.get("broadcast_id"):
        result["action"] = "no_broadcast"
        result["error"] = bcast.get("error") or "no_broadcast"
        result["broadcast"] = bcast
        _record_attempt(state, ch, result, meta, feat, ok=False)
        return result

    applied = _apply_snippet_and_thumb(
        youtube,
        broadcast_id=str(bcast["broadcast_id"]),
        title=str(meta["title"]),
        description=str(meta.get("description") or ""),
        thumbnail_path=Path(meta["thumbnail_path"])
        if meta.get("thumbnail_path")
        else None,
        scheduled_start_time=bcast.get("scheduledStartTime"),
    )
    result["broadcast_id"] = bcast.get("broadcast_id")
    result["broadcast_source"] = bcast.get("source")
    result["lifeCycleStatus"] = bcast.get("lifeCycleStatus")
    result["apply"] = applied
    result["meta"] = {
        "title": meta.get("title"),
        "title_src": meta.get("title_src"),
        "description_src": meta.get("description_src"),
        "description_hash": _sha1_text(str(meta.get("description") or "")),
        "thumbnail_path": meta.get("thumbnail_path"),
        "thumbnail_src": meta.get("thumbnail_src"),
        "source_video_id": meta.get("source_video_id"),
        "job_dir": meta.get("job_dir"),
    }
    result["ok"] = bool(applied.get("ok"))
    result["action"] = "applied" if result["ok"] else "apply_failed"
    _record_attempt(state, ch, result, meta, feat, ok=result["ok"])
    _log_ops(ch, result)
    return result


def _record_attempt(
    state: dict[str, Any],
    channel: str,
    result: dict[str, Any],
    meta: dict[str, Any],
    featured_path: str | None,
    *,
    ok: bool,
) -> None:
    channels = dict(state.get("channels") or {})
    row = {
        "featured_path": featured_path,
        "title": meta.get("title"),
        "description_hash": _sha1_text(str(meta.get("description") or "")),
        "thumbnail_path": meta.get("thumbnail_path"),
        "broadcast_id": result.get("broadcast_id"),
        "action": result.get("action"),
        "ok": ok,
        "last_attempt_at": _utc_now(),
        "title_src": meta.get("title_src"),
        "thumbnail_src": meta.get("thumbnail_src"),
        "error": result.get("error"),
    }
    if ok and result.get("action") == "applied":
        row["applied_at"] = _utc_now()
    channels[channel] = row
    state["channels"] = channels
    try:
        save_state(state)
    except OSError as exc:
        logger.info("live_broadcast_meta state save failed: %s", exc)


def _log_ops(channel: str, result: dict[str, Any]) -> None:
    try:
        from src.agents.ledger import OpsLedger

        meta = result.get("meta") or {}
        OpsLedger().write(
            agent="live_broadcast_meta",
            problem=f"Live packaging sync {channel}",
            action=str(result.get("action") or ""),
            severity="info" if result.get("ok") else "warn",
            extra={
                "channel": channel,
                "broadcast_id": result.get("broadcast_id"),
                "featured_path": result.get("featured_path"),
                "title": meta.get("title"),
                "title_src": meta.get("title_src"),
                "thumbnail_src": meta.get("thumbnail_src"),
                "thumbnail_uploaded": (result.get("apply") or {}).get(
                    "thumbnail_uploaded"
                ),
                "snippet_via": (result.get("apply") or {}).get("snippet_via"),
                "error": result.get("error"),
            },
        )
    except Exception:  # noqa: BLE001
        pass


def sync_all_live_meta(
    *,
    force: bool = False,
    dry_run: bool = False,
    channels: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Sync both Brand Account Live broadcasts from current featured VODs."""
    if channels is None:
        try:
            from src.streaming.vod_loop import CHANNELS

            channels = list(CHANNELS)
        except Exception:  # noqa: BLE001
            channels = ["napstorian", "napping_historian"]
    rows: dict[str, Any] = {}
    for ch in channels:
        try:
            rows[ch] = sync_channel_live_meta(ch, force=force, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            logger.exception("live meta sync failed channel=%s", ch)
            rows[ch] = {
                "ok": False,
                "channel": ch,
                "action": "exception",
                "error": str(exc)[:240],
                "ts": _utc_now(),
            }
    return {
        "ok": all(bool((r or {}).get("ok")) for r in rows.values()) if rows else False,
        "ts": _utc_now(),
        "module": "live_broadcast_meta",
        "channels": rows,
    }


def maybe_sync_after_encode_event(
    channel: str,
    *,
    reason: str,
    featured_path: str | Path | None = None,
) -> dict[str, Any]:
    """Best-effort hook for start/relive/swap — never raises into encode path."""
    try:
        out = sync_channel_live_meta(
            channel, featured_path=featured_path, force=False, dry_run=False
        )
        out["hook_reason"] = reason
        return out
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "live meta hook skipped channel=%s reason=%s: %s", channel, reason, exc
        )
        return {
            "ok": False,
            "channel": channel,
            "action": "hook_error",
            "error": str(exc)[:240],
            "hook_reason": reason,
            "ts": _utc_now(),
        }
