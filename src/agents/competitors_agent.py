"""Competitors Agent — discover/append niche channels + search_queries.

Writes to config/competitors.json (napstorian / legacy) or
config/competitors_<channel>.json for other Brand Accounts.
Format/topic inspiration for Trends only — never copies competitor video
titles into the Sheet / title queue.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from src.agents.ledger import OpsLedger
from src.agents.store import OpsStore
from src.services.settings import CONFIG_DIR, get_settings
from src.services.youtube_channel_auth import (
    KNOWN_CHANNELS,
    LEGACY_DEFAULT_CHANNEL,
    normalize_youtube_channel,
)

logger = logging.getLogger(__name__)

COMPETITORS_PATH = CONFIG_DIR / "competitors.json"


def competitors_path(channel: str | None = None) -> Path:
    """Resolve competitors JSON for a sheet/Brand Account channel.

    napstorian (legacy) → config/competitors.json
    other known channels (e.g. napping_historian) → always
    config/competitors_<channel>.json (never silently fall back to napstorian's
    file — harvest/metrics must stay channel-scoped even if the file is empty
    or missing).
    Unknown extra channels → scoped file when present, else legacy shared file.
    """
    ch = normalize_youtube_channel(channel)
    if ch == LEGACY_DEFAULT_CHANNEL:
        return COMPETITORS_PATH
    scoped = CONFIG_DIR / f"competitors_{ch}.json"
    if ch in KNOWN_CHANNELS or scoped.is_file():
        return scoped
    return COMPETITORS_PATH


def channel_for_competitors_path(path: Path) -> str:
    """Inverse of competitors_path for ops labels / sheet tabs."""
    name = path.name
    if name == "competitors.json":
        return LEGACY_DEFAULT_CHANNEL
    if name.startswith("competitors_") and name.endswith(".json"):
        return name[len("competitors_") : -len(".json")]
    return LEGACY_DEFAULT_CHANNEL


def iter_competitors_paths() -> list[Path]:
    """All competitor config files that exist (legacy + per-channel)."""
    seen: set[Path] = set()
    out: list[Path] = []
    for ch in KNOWN_CHANNELS:
        p = competitors_path(ch)
        rp = p.resolve()
        if not p.is_file() or rp in seen:
            continue
        seen.add(rp)
        out.append(p)
    # Also pick up any extra competitors_*.json not in KNOWN_CHANNELS
    for p in sorted(CONFIG_DIR.glob("competitors_*.json")):
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        out.append(p)
    return out

# Seed discovery queries for the historical “what if” niche (quota-capped per run).
_DISCOVERY_QUERIES = [
    "what if history",
    "alternate history documentary",
    "counterfactual history",
    "historical what if",
    "what if tudor",
    "what if roman empire",
]

_NICHE_HINTS = (
    "what if",
    "alternate history",
    "counterfactual",
    "alt history",
    "historical",
    "history",
)


class CompetitorsAgent:
    """Resolve empty channel IDs and append fresh competitors / search_queries."""

    def __init__(
        self,
        store: OpsStore | None = None,
        ledger: OpsLedger | None = None,
        path: Path | None = None,
        channel: str | None = None,
    ):
        self.store = store or OpsStore()
        self.ledger = ledger or OpsLedger(self.store)
        self.channel = normalize_youtube_channel(channel) if channel else None
        if path is not None:
            self.path = path
        elif channel is not None:
            self.path = competitors_path(channel)
        else:
            self.path = COMPETITORS_PATH
        if self.channel is None:
            self.channel = channel_for_competitors_path(self.path)
        self.s = get_settings()
        self.cfg = _load(self.path)

    def refresh(
        self,
        *,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Resolve IDs, discover channels, append queries. Quota-capped."""
        refresh_cfg = _refresh_cfg(self.cfg)
        agents_cfg = _agents_refresh_cfg()
        enabled = bool(refresh_cfg.get("enabled", True)) and bool(
            agents_cfg.get("enabled", True)
        )
        if not enabled and not force:
            return {"skipped": True, "reason": "competitors_refresh disabled"}

        due, due_reason = self._is_due(refresh_cfg, agents_cfg, force=force)
        if not due:
            return {"skipped": True, "reason": due_reason}

        api_key = (getattr(self.s, "youtube_api_key", None) or "").strip()
        if not api_key:
            return {"error": "YOUTUBE_API_KEY not set", "skipped": True}

        max_channels = int(
            agents_cfg.get("max_new_channels_per_run")
            or refresh_cfg.get("max_new_channels_per_run")
            or 8
        )
        max_queries = int(
            agents_cfg.get("max_new_queries_per_run")
            or refresh_cfg.get("max_new_queries_per_run")
            or 12
        )
        max_channels = max(1, min(max_channels, 10))
        max_queries = max(1, min(max_queries, 15))

        import httpx

        competitors = [dict(c) for c in (self.cfg.get("competitors") or [])]
        queries = [str(q).strip() for q in (self.cfg.get("search_queries") or []) if str(q).strip()]
        niche_hints = _niche_hints(refresh_cfg)
        resolved: list[dict[str, Any]] = []
        appended_channels: list[dict[str, Any]] = []
        appended_queries: list[str] = []
        api_calls = 0
        errors: list[str] = []

        with httpx.Client(timeout=30.0) as client:
            # 1) Fill empty / fake channel_ids for named/handle competitors
            for c in competitors:
                if _is_real_channel_id(c.get("channel_id") or ""):
                    continue
                try:
                    info, n = _resolve_channel(client, api_key, c)
                    api_calls += n
                    if not info:
                        errors.append(
                            f"unresolved:{c.get('handle') or c.get('label') or '?'}"
                        )
                        continue
                    c["channel_id"] = info["channel_id"]
                    if info.get("title"):
                        label = (c.get("label") or "").strip()
                        if not label or label.lower().startswith("placeholder"):
                            c["label"] = info["title"]
                    if info.get("handle") and not (c.get("handle") or "").strip():
                        c["handle"] = info["handle"]
                    c["url"] = info.get("url") or c.get("url") or _channel_url(
                        info["channel_id"], info.get("handle") or c.get("handle")
                    )
                    notes = (c.get("notes") or "").strip()
                    if not notes or "placeholder" in notes.lower():
                        c["notes"] = "resolved via YouTube Data API"
                    resolved.append(
                        {
                            "label": c.get("label"),
                            "handle": c.get("handle"),
                            "channel_id": c["channel_id"],
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"resolve:{exc}")

            # 2) Discover new competitor channels (search type=channel)
            existing_ids = {
                (c.get("channel_id") or "").strip()
                for c in competitors
                if _is_real_channel_id(c.get("channel_id") or "")
            }
            existing_names = {
                _norm_name(c.get("label") or c.get("handle") or "")
                for c in competitors
            }
            discovery_qs = list(
                refresh_cfg.get("discovery_queries") or _DISCOVERY_QUERIES
            )
            # Prefer existing niche queries first (already tuned)
            for q in queries:
                if q not in discovery_qs:
                    discovery_qs.append(q)
            # Cap search.list calls hard (100 quota units each)
            search_budget = min(5, max(2, (max_channels + 3) // 2))
            for q in discovery_qs[:search_budget]:
                if len(appended_channels) >= max_channels:
                    break
                try:
                    found, n = _search_channels(client, api_key, q, max_results=5)
                    api_calls += n
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"search:{q}:{exc}")
                    continue
                for ch in found:
                    if len(appended_channels) >= max_channels:
                        break
                    cid = ch["channel_id"]
                    name_key = _norm_name(ch.get("title") or ch.get("handle") or "")
                    if cid in existing_ids or (name_key and name_key in existing_names):
                        continue
                    if not _looks_niche(
                        ch.get("title") or "",
                        ch.get("description") or "",
                        hints=niche_hints,
                    ):
                        # Still allow if discovery query was strongly niche
                        if not _query_is_niche(q, hints=niche_hints):
                            continue
                    entry = {
                        "channel_id": cid,
                        "handle": ch.get("handle") or "",
                        "label": ch.get("title") or cid,
                        "url": ch.get("url")
                        or _channel_url(cid, ch.get("handle")),
                        "notes": f"discovered via search:{q[:40]}",
                    }
                    competitors.append(entry)
                    existing_ids.add(cid)
                    if name_key:
                        existing_names.add(name_key)
                    appended_channels.append(
                        {
                            "label": entry["label"],
                            "handle": entry.get("handle"),
                            "channel_id": cid,
                        }
                    )

            # 3) Derive extra search_queries from a few competitor uploads (topics only)
            if len(appended_queries) < max_queries:
                seed_ids = [
                    (c.get("channel_id") or "").strip()
                    for c in competitors
                    if _is_real_channel_id(c.get("channel_id") or "")
                ][:4]
                existing_q = {_norm_query(x) for x in queries}
                for cid in seed_ids:
                    if len(appended_queries) >= max_queries:
                        break
                    try:
                        titles, n = _recent_video_titles(
                            client, api_key, cid, max_results=5
                        )
                        api_calls += n
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"uploads:{cid}:{exc}")
                        continue
                    for title in titles:
                        for q in _queries_from_title(title, hints=niche_hints):
                            k = _norm_query(q)
                            if not k or k in existing_q:
                                continue
                            existing_q.add(k)
                            queries.append(q)
                            appended_queries.append(q)
                            if len(appended_queries) >= max_queries:
                                break

            # Also append a few static niche queries if still room
            static_qs = list(discovery_qs[:8]) or list(_DISCOVERY_QUERIES)
            for q in static_qs:
                if len(appended_queries) >= max_queries:
                    break
                k = _norm_query(q)
                if k in {_norm_query(x) for x in queries}:
                    continue
                # only add if not already in original list before this refresh
                if k not in {_norm_query(x) for x in (self.cfg.get("search_queries") or [])}:
                    queries.append(q)
                    appended_queries.append(q)

        now = datetime.now(timezone.utc).isoformat()
        new_cfg = dict(self.cfg)
        new_cfg["competitors"] = competitors
        new_cfg["search_queries"] = queries
        refresh_meta = dict(refresh_cfg)
        refresh_meta["enabled"] = refresh_cfg.get("enabled", True)
        refresh_meta["max_new_channels_per_run"] = max_channels
        refresh_meta["max_new_queries_per_run"] = max_queries
        refresh_meta["min_interval_hours"] = int(
            refresh_cfg.get("min_interval_hours")
            or agents_cfg.get("min_interval_hours")
            or 168
        )
        refresh_meta["last_refresh_at"] = now
        new_cfg["refresh"] = refresh_meta

        summary = {
            "at": now,
            "channel": self.channel,
            "path": str(self.path),
            "due_reason": due_reason,
            "dry_run": dry_run,
            "api_calls_est": api_calls,
            "resolved_channel_ids": resolved,
            "appended_competitors": appended_channels,
            "appended_search_queries": appended_queries,
            "competitors_total": len(competitors),
            "search_queries_total": len(queries),
            "errors": errors[:10],
            "note": "inspiration only — Trends invents originals; titles never pasted to Sheet",
        }

        if dry_run:
            return summary

        self.path.write_text(
            json.dumps(new_cfg, indent=2) + "\n", encoding="utf-8"
        )
        self.cfg = new_cfg

        # Score channels (subs / recent views / activity) + sync Competitors sheet
        try:
            metrics_summary = self.refresh_metrics(force=True, dry_run=False)
            summary["metrics"] = {
                k: metrics_summary.get(k)
                for k in (
                    "scored",
                    "eligible",
                    "api_calls",
                    "sheet",
                    "error",
                    "skipped",
                )
                if k in metrics_summary
            }
        except Exception as exc:  # noqa: BLE001
            summary["metrics"] = {"error": str(exc)}

        self.ledger.write(
            agent="competitors",
            problem=(
                f"refresh: resolved={len(resolved)} "
                f"new_channels={len(appended_channels)} "
                f"new_queries={len(appended_queries)}"
            ),
            action=(
                f"appended {self.path.name} for Trends inspiration only "
                f"(channel={self.channel})"
            ),
            severity="info",
            extra=summary,
        )
        suffix = "" if self.path == COMPETITORS_PATH else f"_{self.channel}"
        state_path = self.store.root / f"competitors_refresh_last{suffix}.json"
        state_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        return summary

    def refresh_metrics(
        self,
        *,
        force: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Pull YouTube stats, set eligible_for_titles, sync Competitors sheet."""
        del force  # always allowed; cheap vs search.list
        api_key = (getattr(self.s, "youtube_api_key", None) or "").strip()
        if not api_key:
            return {"error": "YOUTUBE_API_KEY not set", "skipped": True}

        self.cfg = _load(self.path)
        power = _power_filter(self.cfg)
        competitors = [dict(c) for c in (self.cfg.get("competitors") or [])]
        ids = [
            (c.get("channel_id") or "").strip()
            for c in competitors
            if _is_real_channel_id(c.get("channel_id") or "")
        ]
        if not ids:
            return {"skipped": True, "reason": "no_channel_ids"}

        import httpx

        from src.agents.competitors_sheet import CompetitorsSheet
        from src.agents.youtube_data import YouTubeDataClient, score_channel

        errors: list[str] = []
        api_calls = 0
        scored = 0
        eligible_n = 0
        with httpx.Client(timeout=45.0) as client:
            yt = YouTubeDataClient(client, api_key)
            bundles = yt.channels_bundle(ids)
            sample_n = int(power.get("recent_video_sample") or 10)
            all_video_ids: list[str] = []
            uploads_by_channel: dict[str, list[dict[str, Any]]] = {}
            for cid in ids:
                b = bundles.get(cid) or {}
                pl = b.get("uploads_playlist_id") or ""
                try:
                    ups = yt.recent_uploads(pl, max_n=sample_n) if pl else []
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"uploads:{cid}:{exc}")
                    ups = []
                uploads_by_channel[cid] = ups
                all_video_ids.extend(
                    u["video_id"] for u in ups if u.get("video_id")
                )
            try:
                vstats = yt.videos_stats(all_video_ids)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"videos:{exc}")
                vstats = {}

            for c in competitors:
                cid = (c.get("channel_id") or "").strip()
                if not _is_real_channel_id(cid):
                    c["eligible_for_titles"] = False
                    continue
                bundle = bundles.get(cid) or {}
                metrics = score_channel(
                    channel=bundle,
                    uploads=uploads_by_channel.get(cid) or [],
                    video_stats=vstats,
                    power=power,
                )
                c.update(metrics)
                scored += 1
                if metrics.get("eligible_for_titles"):
                    eligible_n += 1
            api_calls = yt.api_calls

        now = datetime.now(timezone.utc).isoformat()
        power_meta = dict(power)
        power_meta["last_metrics_at"] = now
        new_cfg = dict(self.cfg)
        new_cfg["competitors"] = competitors
        new_cfg["power_filter"] = power_meta

        summary: dict[str, Any] = {
            "at": now,
            "channel": self.channel,
            "path": str(self.path),
            "dry_run": dry_run,
            "scored": scored,
            "eligible": eligible_n,
            "api_calls": api_calls,
            "good_channel_views": power.get("good_channel_views"),
            "errors": errors[:10],
        }

        if dry_run:
            summary["preview"] = [
                {
                    "label": c.get("label"),
                    "subscribers": c.get("subscribers"),
                    "avg_recent_views": c.get("avg_recent_views"),
                    "active_last_7_days": c.get("active_last_7_days"),
                    "eligible_for_titles": c.get("eligible_for_titles"),
                }
                for c in competitors[:8]
            ]
            return summary

        self.path.write_text(json.dumps(new_cfg, indent=2) + "\n", encoding="utf-8")
        self.cfg = new_cfg
        sheet_tab = None
        csv_name = "competitors_metrics.csv"
        if self.path != COMPETITORS_PATH:
            sheet_tab = f"Competitors_{self.channel}"
            csv_name = f"competitors_metrics_{self.channel}.csv"
        sheet_summary = CompetitorsSheet(
            tab=sheet_tab,
            csv_path=self.store.root / csv_name,
        ).sync_from_competitors(competitors)
        summary["sheet"] = sheet_summary
        self.ledger.write(
            agent="competitors",
            problem=f"metrics: scored={scored} eligible={eligible_n} channel={self.channel}",
            action="power_filter + Competitors sheet sync",
            severity="info",
            extra=summary,
        )
        suffix = "" if self.path == COMPETITORS_PATH else f"_{self.channel}"
        (self.store.root / f"competitors_metrics_last{suffix}.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
        return summary

    def _is_due(
        self,
        refresh_cfg: dict[str, Any],
        agents_cfg: dict[str, Any],
        *,
        force: bool,
    ) -> tuple[bool, str]:
        if force:
            return True, "force"
        competitors = self.cfg.get("competitors") or []
        empty_ids = sum(
            1
            for c in competitors
            if not _is_real_channel_id(c.get("channel_id") or "")
        )
        if empty_ids and bool(agents_cfg.get("run_when_ids_empty", True)):
            return True, f"empty_channel_ids={empty_ids}"
        thin = int(agents_cfg.get("thin_competitors_below") or refresh_cfg.get("thin_below") or 3)
        real_n = sum(
            1 for c in competitors if _is_real_channel_id(c.get("channel_id") or "")
        )
        if real_n < thin:
            return True, f"thin_list={real_n}<{thin}"

        min_hours = int(
            agents_cfg.get("min_interval_hours")
            or refresh_cfg.get("min_interval_hours")
            or 168
        )
        last = (refresh_cfg.get("last_refresh_at") or "").strip()
        if not last:
            return True, "never_refreshed"
        try:
            at = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except ValueError:
            return True, "bad_last_refresh_at"
        age = datetime.now(timezone.utc) - at
        if age >= timedelta(hours=min_hours):
            return True, f"interval_elapsed={age.total_seconds()/3600:.1f}h>={min_hours}h"
        return False, f"not_due age={age.total_seconds()/3600:.1f}h min={min_hours}h"


# --- YouTube Data API helpers (API key; search=100u, channels=1u) ---


def _resolve_channel(
    client: Any, api_key: str, entry: dict[str, Any]
) -> tuple[dict[str, Any] | None, int]:
    """Resolve channel_id via forHandle (cheap) or search (expensive). Returns (info, api_calls)."""
    handle = (entry.get("handle") or "").strip()
    label = (entry.get("label") or "").strip()
    calls = 0

    if handle:
        bare = handle.lstrip("@")
        resp = client.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={
                "part": "snippet",
                "forHandle": bare,
                "key": api_key,
            },
        )
        calls += 1
        if resp.status_code == 200:
            items = resp.json().get("items") or []
            if items:
                return _channel_from_item(items[0]), calls
        else:
            logger.warning("channels.forHandle %s: %s", bare, resp.status_code)

    q = handle.lstrip("@") if handle else label
    if not q or q.startswith("placeholder"):
        return None, calls
    resp = client.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "snippet",
            "q": q,
            "type": "channel",
            "maxResults": 3,
            "key": api_key,
        },
    )
    calls += 1
    if resp.status_code != 200:
        logger.warning("search resolve %s: %s", q, resp.status_code)
        return None, calls
    for item in resp.json().get("items") or []:
        sn = item.get("snippet") or {}
        cid = (item.get("id") or {}).get("channelId") or sn.get("channelId")
        if not cid:
            continue
        title = (sn.get("title") or "").strip()
        # Prefer exact-ish handle/title match
        if handle and handle.lstrip("@").lower() in (
            title.lower().replace(" ", ""),
            (sn.get("customUrl") or "").lstrip("@").lower(),
        ):
            return {
                "channel_id": cid,
                "title": title or q,
                "handle": handle if handle.startswith("@") else f"@{handle.lstrip('@')}",
                "url": _channel_url(cid, handle),
                "description": sn.get("description") or "",
            }, calls
        return {
            "channel_id": cid,
            "title": title or q,
            "handle": handle or "",
            "url": _channel_url(cid, handle),
            "description": sn.get("description") or "",
        }, calls
    return None, calls


def _search_channels(
    client: Any, api_key: str, query: str, *, max_results: int = 5
) -> tuple[list[dict[str, Any]], int]:
    resp = client.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "snippet",
            "q": query,
            "type": "channel",
            "maxResults": max(1, min(max_results, 5)),
            "key": api_key,
            "relevanceLanguage": "en",
        },
    )
    if resp.status_code != 200:
        logger.warning("channel search %s: %s", query, resp.status_code)
        return [], 1
    out: list[dict[str, Any]] = []
    for item in resp.json().get("items") or []:
        sn = item.get("snippet") or {}
        cid = (item.get("id") or {}).get("channelId") or sn.get("channelId")
        if not cid:
            continue
        custom = (sn.get("customUrl") or "").strip()
        handle = custom if custom.startswith("@") else (f"@{custom.lstrip('@')}" if custom else "")
        out.append(
            {
                "channel_id": cid,
                "title": (sn.get("title") or "").strip(),
                "handle": handle,
                "description": sn.get("description") or "",
                "url": _channel_url(cid, handle or None),
            }
        )
    return out, 1


def _recent_video_titles(
    client: Any, api_key: str, channel_id: str, *, max_results: int = 5
) -> tuple[list[str], int]:
    resp = client.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "snippet",
            "channelId": channel_id,
            "order": "date",
            "type": "video",
            "maxResults": max(1, min(max_results, 5)),
            "key": api_key,
        },
    )
    if resp.status_code != 200:
        return [], 1
    titles: list[str] = []
    for item in resp.json().get("items") or []:
        t = ((item.get("snippet") or {}).get("title") or "").strip()
        if t:
            titles.append(t)
    return titles, 1


def _channel_from_item(item: dict[str, Any]) -> dict[str, Any]:
    sn = item.get("snippet") or {}
    cid = item.get("id") or ""
    custom = (sn.get("customUrl") or "").strip()
    handle = custom if custom.startswith("@") else (f"@{custom.lstrip('@')}" if custom else "")
    return {
        "channel_id": cid,
        "title": (sn.get("title") or "").strip(),
        "handle": handle,
        "url": _channel_url(cid, handle or None),
        "description": sn.get("description") or "",
    }


def _channel_url(channel_id: str, handle: str | None = None) -> str:
    if handle:
        h = handle if handle.startswith("@") else f"@{handle.lstrip('@')}"
        return f"https://www.youtube.com/{quote(h)}"
    return f"https://www.youtube.com/channel/{channel_id}"


def _queries_from_title(
    title: str, *, hints: tuple[str, ...] | None = None
) -> list[str]:
    """Turn a video title into short search_query phrases — never queue titles."""
    t = re.sub(r"[^\w\s]", " ", title.lower())
    t = re.sub(r"\s+", " ", t).strip()
    out: list[str] = []
    if "what if" in t:
        # Keep short stem: "what if anne boleyn" etc.
        m = re.search(r"what if ([a-z0-9 ]{3,40})", t)
        if m:
            stem = m.group(1).strip()
            words = stem.split()[:4]
            if words:
                out.append("what if " + " ".join(words))
    # Entity-ish bigrams after stripping stopwords
    stop = {
        "what", "if", "the", "a", "an", "and", "or", "to", "of", "in", "on", "for",
        "had", "have", "was", "were", "been", "this", "that", "with", "from", "how",
        "why", "when", "video", "history", "documentary", "full", "episode", "part",
        "never", "survived", "won", "lost", "changed",
    }
    words = [w for w in t.split() if len(w) > 3 and w not in stop]
    if len(words) >= 2:
        out.append(" ".join(words[:3]))
    # Prefer niche-shaped queries only
    return [q for q in out if _query_is_niche(q, hints=hints) or "what if" in q][:2]


def _niche_hints(refresh_cfg: dict[str, Any] | None = None) -> tuple[str, ...]:
    raw = (refresh_cfg or {}).get("niche_hints")
    if isinstance(raw, list) and raw:
        out = tuple(str(h).strip().lower() for h in raw if str(h).strip())
        if out:
            return out
    return _NICHE_HINTS


def _looks_niche(
    title: str, description: str, *, hints: tuple[str, ...] | None = None
) -> bool:
    blob = f"{title} {description}".lower()
    return any(h in blob for h in (hints or _NICHE_HINTS))


def _query_is_niche(q: str, *, hints: tuple[str, ...] | None = None) -> bool:
    ql = q.lower()
    return any(h in ql for h in (hints or _NICHE_HINTS))


def _is_real_channel_id(cid: str) -> bool:
    """YouTube channel IDs look like UC… (~24 chars). Reject placeholders."""
    c = (cid or "").strip()
    if not c or "placeholder" in c.lower():
        return False
    if not c.startswith("UC"):
        return False
    return len(c) >= 20


def _norm_name(s: str) -> str:
    s = s.lower().strip().lstrip("@")
    return re.sub(r"[^a-z0-9]", "", s)


def _norm_query(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().strip())


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _refresh_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    raw = cfg.get("refresh")
    return dict(raw) if isinstance(raw, dict) else {}


def _agents_refresh_cfg() -> dict[str, Any]:
    path = CONFIG_DIR / "agents_settings.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    raw = data.get("competitors_refresh")
    return dict(raw) if isinstance(raw, dict) else {}


def _power_filter(cfg: dict[str, Any]) -> dict[str, Any]:
    defaults = {
        "good_channel_views": 50000,
        "min_subscribers": 10000,
        "require_active_last_7_days": False,
        "require_recent_upload": True,
        "activity_window_days": 45,
        "recent_video_sample": 10,
        "use_median": False,
        "min_metrics_interval_hours": 24,
        # Video-level bar for trends-harvest provenance (viral-ish inspiration)
        "good_video_views": 100000,
        "good_video_vs_channel_avg": 1.0,
        "min_video_likes": 500,
    }
    raw = cfg.get("power_filter")
    if not isinstance(raw, dict):
        return defaults
    out = dict(defaults)
    out.update(raw)
    return out


def metrics_is_due(cfg: dict[str, Any]) -> tuple[bool, str]:
    """Whether a standalone metrics refresh should run (daily by default)."""
    power = _power_filter(cfg)
    min_hours = int(power.get("min_metrics_interval_hours") or 24)
    last = (power.get("last_metrics_at") or "").strip()
    if not last:
        # Also treat missing eligible flags as due
        comps = cfg.get("competitors") or []
        if comps and not any("avg_recent_views" in c for c in comps):
            return True, "never_scored"
        return True, "never_metrics"
    try:
        at = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return True, "bad_last_metrics_at"
    age = datetime.now(timezone.utc) - at
    if age >= timedelta(hours=min_hours):
        return True, f"metrics_interval={age.total_seconds()/3600:.1f}h>={min_hours}h"
    return False, f"metrics_not_due age={age.total_seconds()/3600:.1f}h"
