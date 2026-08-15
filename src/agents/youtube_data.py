"""Thin YouTube Data API helper (API key) — channels / uploads / video stats.

Uses cheap endpoints (channels, playlistItems, videos). Avoid search.list here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

logger = logging.getLogger(__name__)

YT_BASE = "https://www.googleapis.com/youtube/v3"


class YouTubeDataClient:
    def __init__(self, client: Any, api_key: str):
        self.client = client
        self.api_key = api_key
        self.api_calls = 0

    def channels_bundle(self, channel_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Return channel_id → {subscribers, total_views, video_count, uploads_playlist_id, title}."""
        out: dict[str, dict[str, Any]] = {}
        ids = [c.strip() for c in channel_ids if c and c.strip()]
        for chunk in _chunks(ids, 50):
            resp = self.client.get(
                f"{YT_BASE}/channels",
                params={
                    "part": "snippet,statistics,contentDetails",
                    "id": ",".join(chunk),
                    "key": self.api_key,
                },
            )
            self.api_calls += 1
            if resp.status_code != 200:
                logger.warning("channels.list %s: %s", resp.status_code, resp.text[:200])
                continue
            for item in resp.json().get("items") or []:
                cid = item.get("id") or ""
                stats = item.get("statistics") or {}
                cd = item.get("contentDetails") or {}
                related = cd.get("relatedPlaylists") or {}
                sn = item.get("snippet") or {}
                out[cid] = {
                    "channel_id": cid,
                    "title": (sn.get("title") or "").strip(),
                    "subscribers": _int(stats.get("subscriberCount")),
                    "total_views": _int(stats.get("viewCount")),
                    "video_count": _int(stats.get("videoCount")),
                    "uploads_playlist_id": (related.get("uploads") or "").strip(),
                    "hidden_subscriber_count": bool(stats.get("hiddenSubscriberCount")),
                }
        return out

    def recent_uploads(
        self, uploads_playlist_id: str, *, max_n: int = 10
    ) -> list[dict[str, Any]]:
        """playlistItems → [{video_id, published_at, title}]."""
        if not uploads_playlist_id:
            return []
        resp = self.client.get(
            f"{YT_BASE}/playlistItems",
            params={
                "part": "snippet,contentDetails",
                "playlistId": uploads_playlist_id,
                "maxResults": max(1, min(int(max_n), 50)),
                "key": self.api_key,
            },
        )
        self.api_calls += 1
        if resp.status_code != 200:
            logger.warning(
                "playlistItems %s: %s", resp.status_code, resp.text[:200]
            )
            return []
        out: list[dict[str, Any]] = []
        for item in resp.json().get("items") or []:
            sn = item.get("snippet") or {}
            cd = item.get("contentDetails") or {}
            vid = (cd.get("videoId") or sn.get("resourceId", {}).get("videoId") or "").strip()
            published = (
                (cd.get("videoPublishedAt") or sn.get("publishedAt") or "").strip()
            )
            title = (sn.get("title") or "").strip()
            if not vid:
                continue
            out.append(
                {
                    "video_id": vid,
                    "published_at": published,
                    "title": title,
                }
            )
        return out

    def videos_stats(self, video_ids: list[str]) -> dict[str, dict[str, Any]]:
        """video_id → {views, likes, comments, published_at, title, channel_id}."""
        out: dict[str, dict[str, Any]] = {}
        ids = [v.strip() for v in video_ids if v and v.strip()]
        for chunk in _chunks(ids, 50):
            resp = self.client.get(
                f"{YT_BASE}/videos",
                params={
                    "part": "snippet,statistics",
                    "id": ",".join(chunk),
                    "key": self.api_key,
                },
            )
            self.api_calls += 1
            if resp.status_code != 200:
                logger.warning("videos.list %s: %s", resp.status_code, resp.text[:200])
                continue
            for item in resp.json().get("items") or []:
                vid = item.get("id") or ""
                sn = item.get("snippet") or {}
                st = item.get("statistics") or {}
                out[vid] = {
                    "video_id": vid,
                    "title": (sn.get("title") or "").strip(),
                    "published_at": (sn.get("publishedAt") or "").strip(),
                    "channel_id": (sn.get("channelId") or "").strip(),
                    "channel_title": (sn.get("channelTitle") or "").strip(),
                    "views": _int(st.get("viewCount")),
                    "likes": _int(st.get("likeCount")),
                    "comments": _int(st.get("commentCount")),
                }
        return out


def score_channel(
    *,
    channel: dict[str, Any],
    uploads: list[dict[str, Any]],
    video_stats: dict[str, dict[str, Any]],
    power: dict[str, Any],
    tz_name: str = "Asia/Karachi",
) -> dict[str, Any]:
    """Build metrics + eligible_for_titles for one channel."""
    from zoneinfo import ZoneInfo

    now = datetime.now(timezone.utc)
    sample_n = int(power.get("recent_video_sample") or 10)
    views: list[int] = []
    published_ats: list[datetime] = []
    views_last_7d = 0
    upload_freq_30d = 0
    hour_weights = [0.0] * 24
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = timezone(timedelta(hours=5))  # PKT fallback

    for up in uploads[:sample_n]:
        vid = up.get("video_id") or ""
        st = video_stats.get(vid) or {}
        v = int(st.get("views") or 0)
        views.append(v)
        pub_raw = (st.get("published_at") or up.get("published_at") or "").strip()
        pub = _parse_dt(pub_raw)
        if pub:
            published_ats.append(pub)
            age = now - pub
            if age <= timedelta(days=7):
                views_last_7d += v
            if age <= timedelta(days=30):
                upload_freq_30d += 1
            local_h = int(pub.astimezone(tz).hour)
            hour_weights[local_h] += float(max(v, 1))

    avg_views = int(sum(views) / len(views)) if views else 0
    med_views = int(median(views)) if views else 0
    last_upload = max(published_ats) if published_ats else None
    active_7d = bool(last_upload and (now - last_upload) <= timedelta(days=7))
    activity_days = int(power.get("activity_window_days") or 30)
    active_window = bool(
        last_upload and (now - last_upload) <= timedelta(days=activity_days)
    )

    use_median = bool(power.get("use_median"))
    strength = med_views if use_median else avg_views
    good = int(power.get("good_channel_views") or 50000)
    min_subs = int(power.get("min_subscribers") or 10000)
    # Prefer explicit activity_window; fall back to legacy 7d flag
    if "require_recent_upload" in power:
        require_recent = bool(power.get("require_recent_upload"))
    else:
        require_recent = bool(power.get("require_active_last_7_days", True))
    subs = int(channel.get("subscribers") or 0)

    eligible = strength >= good and subs >= min_subs
    if require_recent:
        eligible = eligible and active_window

    hour_ranked = sorted(
        ((h, hour_weights[h]) for h in range(24) if hour_weights[h] > 0),
        key=lambda x: x[1],
        reverse=True,
    )
    best_hours = [h for h, _w in hour_ranked[:5]]
    hour_hist = {str(h): round(w, 1) for h, w in hour_ranked}

    return {
        "subscribers": subs,
        "total_views": int(channel.get("total_views") or 0),
        "video_count": int(channel.get("video_count") or 0),
        "uploads_playlist_id": channel.get("uploads_playlist_id") or "",
        "avg_recent_views": avg_views,
        "median_recent_views": med_views,
        "upload_freq_per_30d": upload_freq_30d,
        "last_upload_at": last_upload.isoformat() if last_upload else "",
        "active_last_7_days": active_7d,
        "active_in_window": active_window,
        "views_last_7d_uploads": views_last_7d,
        "eligible_for_titles": eligible,
        "metrics_updated_at": now.isoformat(),
        "recent_video_ids": [u.get("video_id") for u in uploads[:sample_n] if u.get("video_id")],
        "upload_hours_tz": tz_name,
        "upload_hour_histogram_local": hour_hist,
        "best_upload_hours_local": best_hours,
    }


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _parse_dt(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
