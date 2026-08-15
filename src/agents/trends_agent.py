"""Trends + Algo Agent — inspire from competitors/search, invent ORIGINAL titles only."""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from src.agents.ledger import OpsLedger
from src.agents.policy_agent import PolicyAgent
from src.agents.smm_harvest_bridge import (
    apply_winner_bias_to_row,
    load_smm_winner_signals,
    merge_winner_topics,
    prefer_queries_for_winners,
    strip_internal_bias_fields,
    summarize_bias,
    write_smm_harvest_feedback,
)
from src.agents.store import OpsStore
from src.agents.title_queue import TitleQueue
from src.services.settings import CONFIG_DIR, get_settings

logger = logging.getLogger(__name__)

# Original divergence bank — used when LLM unavailable. NOT scraped from competitors.
# Napstorian / what_if style only.
_ORIGINAL_BANK_WHAT_IF = [
    "What If the Spanish Armada Had Won in 1588?",
    "What If Anne Boleyn Outlived Henry the Eighth?",
    "What If the Library of Alexandria Never Burned?",
    "What If Richard the Third Had Won at Bosworth?",
    "What If the Gunpowder Plot Had Succeeded?",
    "What If Elizabeth the First Had Married and Had an Heir?",
    "What If the Black Death Had Never Reached England?",
    "What If the Normans Had Lost at Hastings?",
    "What If Mary Queen of Scots Had Taken the English Throne?",
    "What If the Pilgrimage of Grace Had Toppled Henry?",
    "What If Constantinople Had Never Fallen in 1453?",
    "What If the Wars of the Roses Never Ended?",
    "What If Cromwell Had Refused the Crown Forever?",
    "What If the Mayflower Had Never Sailed?",
    "What If Joan of Arc Had Survived the Trial?",
    "What If the Printing Press Had Come to England Fifty Years Earlier?",
    "What If the Viking Great Heathen Army Had Held York Forever?",
    "What If the Magna Carta Had Failed Completely?",
    "What If Henry the Fifth Had Lived to Old Age?",
    "What If the Spanish Inquisition Had Taken Root in England?",
]

# napping_historian documentary / mystery bank — never "What If …" prefixes.
_ORIGINAL_BANK_DOCUMENTARY = [
    "The Dark Secret Henry VIII Tried to Bury in the Tower",
    "What Really Happened the Night Anne Boleyn Was Arrested",
    "The Forgotten Letters That Doomed Catherine Howard",
    "Inside the Tudor Court: The Whisper Network Behind the Throne",
    "The Mysterious Death of Prince Arthur and the Rise of Henry VIII",
    "Why the Tower of London Still Haunts English History",
    "The Dark History of Lady Jane Grey's Nine-Day Reign",
    "The Secret Life of Thomas Cromwell Before the Fall",
    "Elizabeth I's Hidden Spies and the Shadow War for England",
    "The Unexplained Vanishings of the Princes in the Tower",
    "Fall of a Dynasty: How the Wars of the Roses Remade England",
    "The Eerie Final Hours of Mary Queen of Scots",
    "Catherine of Aragon: The Queen England Tried to Erase",
    "The Silent Coup That Nearly Toppled the Tudor Throne",
    "Dark Docs of the Reformation: Faith, Fire, and Forbidden Books",
    "The Bedtime History of Medieval England's Bloodiest Winter",
    "Why Civilizations Fall: Lessons From Carthage to Constantinople",
    "The Whispered Prophecy That Terrified the Tudor Court",
    "Anne of Cleves and the Marriage Henry VIII Wanted Forgotten",
    "The Night Owl's Guide to England's Most Haunted Royal Sites",
]

# Back-compat alias for older imports / tests.
_ORIGINAL_BANK = _ORIGINAL_BANK_WHAT_IF

_PLACEHOLDER_SOURCES = frozenset(
    {
        "",
        "llm_original",
        "original",
        "original_bank",
        "yt_search",
    }
)


class TrendsAgent:
    """Trend fit + original title invention. Never copies YouTuber titles into the Sheet."""

    def __init__(
        self,
        store: OpsStore | None = None,
        ledger: OpsLedger | None = None,
        policy: PolicyAgent | None = None,
        queue: TitleQueue | None = None,
    ):
        self.store = store or OpsStore()
        self.ledger = ledger or OpsLedger(self.store)
        self.policy = policy or PolicyAgent(self.store, self.ledger)
        self.queue = queue or TitleQueue()
        self.s = get_settings()
        self.cfg = _load_competitors()

    def harvest_titles(
        self,
        *,
        dry_run: bool = False,
        channel: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        # Per-channel competitors (napstorian → competitors.json;
        # napping_historian → competitors_napping_historian.json).
        from src.agents.competitors_agent import competitors_path
        from src.services.youtube_channel_auth import (
            LEGACY_DEFAULT_CHANNEL,
            normalize_youtube_channel,
        )

        sheet_channel = (
            normalize_youtube_channel(channel)
            if channel
            else LEGACY_DEFAULT_CHANNEL
        )
        competitors_file = competitors_path(sheet_channel)
        self.cfg = _load_competitors(sheet_channel)
        logger.info(
            "harvest channel=%s competitors=%s loaded=%s",
            sheet_channel,
            competitors_file.name,
            len(self.cfg.get("competitors") or []),
        )
        # Always write to the resolved sheet tab (never cross-contaminate).
        channel = sheet_channel
        if self.store.latest_policy_snapshot() is None:
            try:
                self.policy.refresh()
            except Exception as exc:  # noqa: BLE001
                logger.warning("policy refresh skipped: %s", exc)

        # Inspiration only — topics / entities / format signals (never sheet titles)
        # SMM→Harvest: read channel_winners for THIS sheet (no Meta cookies).
        winner_signals = self._load_smm_winner_signals(channel=channel)
        invent_cfg = _title_invent_cfg(self.cfg, channel=channel)
        inspiration = self._collect_inspiration(
            winner_signals=winner_signals,
            channel=channel,
            invent_cfg=invent_cfg,
        )
        eligible_n = int(inspiration.get("eligible_channels") or 0)
        if eligible_n <= 0:
            msg = (
                "no eligible competitors (run competitors-metrics; "
                "check power_filter good_channel_views / active_last_7_days)"
            )
            logger.warning(msg)
            self.ledger.write(
                agent="trends",
                problem=msg,
                action="skipped harvest — refuse weak-channel inspiration",
                severity="warn",
            )
            return []

        good_n = int(inspiration.get("good_videos") or 0)
        if good_n <= 0:
            bar = inspiration.get("video_filter") or _video_filter_cfg(self.cfg)
            msg = (
                "no competitor videos passed viral bar "
                f"(good_video_views={bar.get('good_video_views')}, "
                f"vs_channel_avg={bar.get('good_video_vs_channel_avg')}, "
                f"min_likes={bar.get('min_video_likes')}); "
                "refuse weak-video inspiration"
            )
            logger.warning(msg)
            self.ledger.write(
                agent="trends",
                problem=msg,
                action="skipped harvest — no viral-video provenance",
                severity="warn",
                extra={"video_filter": bar, "eligible_channels": eligible_n},
            )
            return []

        blocked = self._blocked_titles(inspiration)
        originals = self._invent_original_titles(
            inspiration,
            blocked=blocked,
            invent_cfg=invent_cfg,
            channel=channel,
        )

        cfg_max = int(self.cfg.get("max_titles_per_run") or 15)
        if limit is not None:
            try:
                max_n = max(1, min(int(limit), cfg_max))
            except (TypeError, ValueError):
                max_n = cfg_max
        else:
            max_n = cfg_max
        prepared: list[dict[str, Any]] = []
        for item in originals:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            # Hard reject: too close to any competitor/search title we saw
            if self._too_close_to_blocked(title, blocked):
                continue
            sat_ok, sat_errs = self._saturation_ok(title)
            if not sat_ok:
                continue
            ok, errs = self.policy.title_policy_ok(title)
            # Sheet stocks policy_ok only — never leave blocked junk on tabs.
            if not ok:
                continue
            row = {
                    "title": title,
                    "competitor_source": item.get("inspired_by")
                    or _fallback_channel_url(self.cfg),
                    "source_subs": item.get("source_subs", ""),
                    "source_avg_recent_views": item.get(
                        "source_avg_recent_views", ""
                    ),
                    "source_eligible": item.get("source_eligible", ""),
                    "source_video_views": item.get("source_video_views", ""),
                    "source_video_likes": item.get("source_video_likes", ""),
                    "source_video_comments": item.get(
                        "source_video_comments", ""
                    ),
                    "source_video_uploaded_at": item.get(
                        "source_video_uploaded_at", ""
                    ),
                    "trend_score": float(item.get("score") or 0.55),
                    "policy_ok": True,
                    "approved": False,
                    "public_approved": False,
                    "status": "queued",
                    "notes": item.get("notes") or "ok",
                }
            row = apply_winner_bias_to_row(
                row,
                winner_signals,
                invent_style=str(invent_cfg.get("style") or ""),
                channel=channel,
            )
            if row.get("_smm_winner_reject"):
                continue
            prepared.append(row)
            if len(prepared) >= max_n:
                break

        prepared.sort(key=lambda x: (-x["trend_score"],))
        prepared = prepared[:max_n]
        bias_counts = summarize_bias(prepared)
        try:
            write_smm_harvest_feedback(
                None,
                signals=winner_signals,
                prepared=prepared,
                dry_run=dry_run,
                channel=channel,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("smm harvest feedback write failed: %s", exc)

        sheet_rows = [strip_internal_bias_fields(p) for p in prepared]
        if dry_run:
            logger.info(
                "harvest dry_run winner_bias biased=%s/%s rejected=%s",
                bias_counts.get("biased"),
                bias_counts.get("titles"),
                bias_counts.get("rejected_too_close"),
            )
            return sheet_rows

        added = self.queue.append_titles(sheet_rows, channel=channel)
        self._record_harvest(sheet_rows, channel=channel)
        self._save_last_inspiration(inspiration, channel=channel)
        self.ledger.write(
            agent="trends",
            problem=f"invented {len(sheet_rows)} ORIGINAL titles ({added} new)",
            action="populated queue — viral-video inspired + SMM winner bias",
            severity="info",
            extra={
                "channel": channel,
                "competitors_file": competitors_file.name,
                "competitors_loaded": len(self.cfg.get("competitors") or []),
                "titles": [p["title"] for p in sheet_rows],
                "sources": [p["competitor_source"] for p in sheet_rows],
                "source_video_views": [
                    p.get("source_video_views") for p in sheet_rows
                ],
                "good_videos": good_n,
                "video_filter": inspiration.get("video_filter")
                or _video_filter_cfg(self.cfg),
                "smm_winner_bias": bias_counts,
                "smm_winners_updated_at": winner_signals.get("updated_at"),
            },
        )
        return sheet_rows

    def backfill_competitor_sources(
        self, *, dry_run: bool = False, use_api: bool = False
    ) -> dict[str, Any]:
        """Rewrite placeholder competitor_source values to YouTube URLs.

        Prefer last-harvest video URLs when available; else match to competitor
        channel URLs from competitors.json. Optional light API refresh is off by
        default (quota-safe).
        """
        inspiration = self._load_last_inspiration()
        if use_api or not (inspiration.get("items") or []):
            if use_api:
                inspiration = self._collect_inspiration()
                if not dry_run:
                    self._save_last_inspiration(inspiration)
            else:
                # Channel-only pool from config — no YouTube API calls
                inspiration = {
                    "topics": [],
                    "blocked_titles": [],
                    "sources": [],
                    "items": _channel_inspiration_items(self.cfg),
                    "format": inspiration.get("format") or "",
                }

        items = list(inspiration.get("items") or [])
        if not items:
            items = _channel_inspiration_items(self.cfg)

        rows = self.queue.list_rows()
        updated: list[dict[str, Any]] = []
        used: set[str] = set()
        for row in rows:
            if not _needs_source_backfill(row.competitor_source):
                # Still collect already-good URLs so rotation avoids collisions
                if _is_youtube_url(row.competitor_source) and "%40" not in row.competitor_source:
                    used.add(row.competitor_source.strip())
                continue
            url = _pick_provenance(
                row.title, items, prefer_video=True, used=used
            )
            if not url:
                url = _fallback_channel_url(self.cfg)
            url = url.replace("/%40", "/@")
            if not url or url == row.competitor_source:
                continue
            updated.append(
                {
                    "row_index": row.row_index,
                    "title": row.title,
                    "from": row.competitor_source,
                    "to": url,
                }
            )
            row.competitor_source = url
            used.add(url)

        if not dry_run and updated:
            self.queue._persist(rows)  # noqa: SLF001 — intentional full rewrite
            self.ledger.write(
                agent="trends",
                problem=f"backfilled competitor_source on {len(updated)} rows",
                action="replaced llm_original/placeholders with YouTube URLs",
                severity="info",
                extra={"updated": len(updated)},
            )

        return {
            "updated": len(updated),
            "dry_run": dry_run,
            "samples": updated[:8],
            "inspiration_items": len(items),
        }

    def precheck_title(
        self, title: str, *, skip_row_index: int | None = None
    ) -> tuple[bool, list[str]]:
        """Farm-time gate: policy only.

        Saturation (30d harvest history / near-dup queue) applies at *harvest*
        when inventing sheet titles — not when an already-approved row is picked
        for farming. Otherwise every harvested title fails its own history entry.
        """
        del skip_row_index  # reserved for callers; saturation no longer used here
        return self.policy.title_policy_ok(title)

    def _collect_inspiration(
        self,
        *,
        winner_signals: dict[str, Any] | None = None,
        channel: str | None = None,
        invent_cfg: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Pull topics/entities from search + eligible competitors — titles for blocklist only.

        Provenance / invent pool is restricted to videos that pass the viral bar
        (power_filter.good_video_*). Weak videos still feed blocklist + topics.
        When ``winner_signals`` is present, prefer competitor/search queries that
        match SMM channel_winners themes (quality→virality).
        """
        invent_cfg = invent_cfg or _title_invent_cfg(self.cfg, channel=channel)
        queries = prefer_queries_for_winners(
            list(self.cfg.get("search_queries") or []),
            winner_signals,
            invent_style=str(invent_cfg.get("style") or ""),
            query_inject_template=str(
                invent_cfg.get("query_inject_template") or ""
            ),
            channel=channel,
        )
        blocked_titles: list[str] = []
        topics: list[str] = list(queries)
        sources: list[str] = []
        raw_items: list[dict[str, Any]] = []
        video_filter = _video_filter_cfg(self.cfg)

        eligible = _eligible_competitors(self.cfg)
        # Channel-level fallback only used when no viral videos exist (then harvest skips)
        channel_items = _channel_inspiration_items(self.cfg, eligible_only=True)

        api_key = (getattr(self.s, "youtube_api_key", None) or "").strip()
        if api_key:
            try:
                import httpx

                from src.agents.youtube_data import YouTubeDataClient

                # Topic/blocklist probes via search (quota-heavy; failures are non-fatal)
                for q in queries[:4]:
                    with httpx.Client(timeout=30.0) as client:
                        resp = client.get(
                            "https://www.googleapis.com/youtube/v3/search",
                            params={
                                "part": "snippet",
                                "q": q,
                                "type": "video",
                                "maxResults": 5,
                                "key": api_key,
                                "relevanceLanguage": "en",
                            },
                        )
                        if resp.status_code != 200:
                            logger.warning("YT search %s: %s", q, resp.status_code)
                            continue
                        for item in resp.json().get("items") or []:
                            entry = _inspiration_from_search_item(
                                item, kind="search_video", competitor=None
                            )
                            if not entry:
                                continue
                            raw = entry.get("blocked_title") or ""
                            if raw:
                                blocked_titles.append(raw)
                                topics.extend(_extract_topics(raw))
                            # Candidate provenance only if search hit is eligible channel
                            if _channel_is_eligible(
                                self.cfg, entry.get("channel_id") or ""
                            ):
                                sources.append(
                                    entry.get("channel_label") or "yt_search"
                                )
                                raw_items.append(entry)
                            else:
                                sources.append("yt_search_blocklist_only")

                # Eligible competitor uploads via playlistItems (cheap) — primary viral pool
                sample_n = int(
                    (self.cfg.get("power_filter") or {}).get("recent_video_sample")
                    or 10
                )
                sample_n = max(5, min(sample_n, 20))
                with httpx.Client(timeout=45.0) as client:
                    yt = YouTubeDataClient(client, api_key)
                    for c in eligible:
                        pl = (c.get("uploads_playlist_id") or "").strip()
                        cid = (c.get("channel_id") or "").strip()
                        if not pl and cid:
                            bundle = yt.channels_bundle([cid]).get(cid) or {}
                            pl = (bundle.get("uploads_playlist_id") or "").strip()
                            if pl:
                                c["uploads_playlist_id"] = pl
                        if not pl:
                            continue
                        ups = yt.recent_uploads(pl, max_n=sample_n)
                        for up in ups:
                            entry = _inspiration_from_upload(
                                up, kind="competitor_video", competitor=c
                            )
                            if not entry:
                                continue
                            raw = entry.get("blocked_title") or ""
                            if raw:
                                blocked_titles.append(raw)
                                topics.extend(_extract_topics(raw))
                            sources.append(c.get("label") or cid)
                            raw_items.append(entry)

                # Enrich inspiration videos with public stats (views/likes/comments/upload time)
                raw_items = _enrich_items_with_video_stats(
                    raw_items, api_key=api_key, cfg=self.cfg
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("inspiration fetch failed: %s", exc)

        # Quota / empty live fetch: reuse last viral-enriched cache if it still passes the bar
        # Cache is channel-scoped so napstorian AlternateHistory* never pollutes historian.
        if not any((it.get("video_url") or "").strip() for it in raw_items):
            cached = self._load_last_inspiration(channel=channel)
            cached_items = list(cached.get("items") or [])
            if cached_items:
                logger.info(
                    "inspiration: using %d cached items (live fetch empty/rate-limited) channel=%s",
                    len(cached_items),
                    channel or "napstorian",
                )
                for it in cached_items:
                    # Refresh channel stamps from THIS channel's competitors file
                    comp = _competitor_by_id(self.cfg, it.get("channel_id") or "")
                    if not comp:
                        # Drop cross-channel / stale competitor videos
                        continue
                    it["source_subs"] = comp.get("subscribers")
                    it["source_avg_recent_views"] = comp.get("avg_recent_views")
                    it["source_eligible"] = bool(comp.get("eligible_for_titles"))
                    if not it.get("source_eligible"):
                        continue
                    raw_items.append(it)
                    raw = it.get("blocked_title") or ""
                    if raw:
                        blocked_titles.append(raw)
                        topics.extend(_extract_topics(raw))

        for c in eligible:
            sources.append(c.get("label") or c.get("handle") or "competitor")

        # Dedupe topics
        seen: set[str] = set()
        uniq_topics: list[str] = []
        for t in topics:
            k = t.lower().strip()
            if k and k not in seen:
                seen.add(k)
                uniq_topics.append(t.strip())

        raw_items = _dedupe_inspiration_items(raw_items)
        # Viral-only invent/provenance pool; weak videos already contributed to blocklist
        good_videos = [
            it
            for it in raw_items
            if (it.get("video_url") or "").strip()
            and _passes_good_video_bar(it, self.cfg)
        ]
        for it in good_videos:
            it["good_video"] = True
            it["views_bar"] = _effective_video_views_bar(it, self.cfg)

        # Prefer viral videos; never use channel-only as primary when viral exist
        if good_videos:
            items = good_videos
        else:
            items = []

        uniq_topics = merge_winner_topics(
            uniq_topics,
            winner_signals,
            invent_style=str(invent_cfg.get("style") or ""),
        )
        return {
            "topics": uniq_topics[:40],
            "smm_winner_bias": bool((winner_signals or {}).get("has_winners")),
            "smm_winner_entities": list((winner_signals or {}).get("entities") or []),
            "smm_evergreen_themes": list(
                (winner_signals or {}).get("evergreen_themes") or []
            )[:12],
            "blocked_titles": blocked_titles,
            "sources": sources[:20],
            "items": items,
            "eligible_channels": len(eligible),
            "good_videos": len(good_videos),
            "weak_videos_seen": sum(
                1
                for it in raw_items
                if (it.get("video_url") or "").strip()
                and not _passes_good_video_bar(it, self.cfg)
            ),
            "channel_items_available": len(channel_items),
            "video_filter": video_filter,
            "format": str(
                invent_cfg.get("format_description")
                or "What If … ? alternate-history documentary for history nerds"
            ),
            "title_invent_style": str(invent_cfg.get("style") or "what_if"),
            "forbid_what_if_prefix": bool(
                invent_cfg.get("forbid_what_if_prefix")
            ),
            "channel": channel,
        }

    def _invent_original_titles(
        self,
        inspiration: dict[str, Any],
        *,
        blocked: list[str],
        invent_cfg: dict[str, Any] | None = None,
        channel: str | None = None,
    ) -> list[dict[str, Any]]:
        invent_cfg = invent_cfg or _title_invent_cfg(
            self.cfg, channel=channel or inspiration.get("channel")
        )
        style = str(invent_cfg.get("style") or "what_if").strip().lower()
        forbid_what_if = bool(invent_cfg.get("forbid_what_if_prefix"))
        max_n = int(self.cfg.get("max_titles_per_run") or 15)
        items = list(inspiration.get("items") or [])
        used_urls: set[str] = set()
        llm_titles = self._llm_original_titles(
            inspiration, count=max_n + 5, invent_cfg=invent_cfg
        )
        out: list[dict[str, Any]] = []
        for t in llm_titles:
            title = _format_invented_title(t, invent_cfg=invent_cfg)
            if not title:
                continue
            if forbid_what_if and _is_what_if_title(title):
                continue
            if title and not self._too_close_to_blocked(title, blocked):
                url, src = _pick_provenance_item(
                    title, items, prefer_video=True, used=used_urls
                )
                if url:
                    used_urls.add(url)
                row = {
                    "title": title,
                    "score": 0.75,
                    "inspired_by": url or _fallback_channel_url(self.cfg),
                    "notes": "original — inspired by competitor format/topic",
                }
                row.update(_stamps_from_item(src, self.cfg))
                out.append(row)

        # Fill from style-matched original bank if LLM short
        bank = (
            _ORIGINAL_BANK_DOCUMENTARY
            if style == "documentary_mystery"
            else _ORIGINAL_BANK_WHAT_IF
        )
        for t in bank:
            if len(out) >= max_n + 5:
                break
            if forbid_what_if and _is_what_if_title(t):
                continue
            if self._too_close_to_blocked(t, blocked):
                continue
            if any(_similar(t, x["title"]) > 0.85 for x in out):
                continue
            url, src = _pick_provenance_item(
                t, items, prefer_video=True, used=used_urls
            )
            if url:
                used_urls.add(url)
            row = {
                "title": t,
                "score": 0.55,
                "inspired_by": url or _fallback_channel_url(self.cfg),
                "notes": "original bank title",
            }
            row.update(_stamps_from_item(src, self.cfg))
            out.append(row)

        # Topic → original title if still short
        for topic in inspiration.get("topics") or []:
            if len(out) >= max_n + 5:
                break
            title = _topic_to_original_title(topic, invent_cfg=invent_cfg)
            if not title or self._too_close_to_blocked(title, blocked):
                continue
            if forbid_what_if and _is_what_if_title(title):
                continue
            if any(_similar(title, x["title"]) > 0.85 for x in out):
                continue
            url, src = _pick_provenance_item(
                title, items, prefer_video=True, used=used_urls
            )
            if url:
                used_urls.add(url)
            row = {
                "title": title,
                "score": 0.5,
                "inspired_by": url or _fallback_channel_url(self.cfg),
                "notes": "original from topic keyword",
            }
            row.update(_stamps_from_item(src, self.cfg))
            out.append(row)
        return out

    def _llm_original_titles(
        self,
        inspiration: dict[str, Any],
        *,
        count: int,
        invent_cfg: dict[str, Any] | None = None,
    ) -> list[str]:
        """Ask OpenRouter/WaveSpeed for ORIGINAL titles; never echo competitor titles."""
        invent_cfg = invent_cfg or _title_invent_cfg(
            self.cfg, channel=inspiration.get("channel")
        )
        default_topics = (
            "Tudor court intrigue and dark history mysteries"
            if str(invent_cfg.get("style") or "").lower() == "documentary_mystery"
            else "Tudor alternate history"
        )
        topics = ", ".join((inspiration.get("topics") or [])[:20]) or default_topics
        winner_prefs = ", ".join(
            list(inspiration.get("smm_winner_entities") or [])[:6]
            or [t for t in (inspiration.get("topics") or [])[:6]]
        )
        blocked_sample = (inspiration.get("blocked_titles") or [])[:8]
        system = str(
            invent_cfg.get("llm_system")
            or (
                "You invent ORIGINAL YouTube titles for a historical 'What If' documentary channel. "
                "Audience: history nerds. Format: 'What If …?' only. "
                "CRITICAL: Do NOT copy, paraphrase closely, or reuse any competitor title provided. "
                "Only use topics as loose inspiration. Return JSON: {\"titles\": [\"...\", ...]}"
            )
        )
        format_line = str(
            invent_cfg.get("llm_format_line")
            or "FORMAT to match: What If [historical divergence]?"
        )
        user = (
            f"Invent {count} original titles.\n"
            f"Topic inspiration (keywords only): {topics}\n"
            f"Prefer themes that already work on THIS channel (SMM winners, not copies): {winner_prefs or topics}\n"
            f"{format_line}\n"
            f"NEVER copy these competitor titles (blocklist):\n"
            + "\n".join(f"- {b}" for b in blocked_sample)
            + "\nReturn JSON only."
        )
        # Prefer OpenRouter (packaging key); fall back to WaveSpeed LLM client
        try:
            if (self.s.openrouter_api_key or "").strip():
                from src.services.openrouter import OpenRouterClient

                data = OpenRouterClient(self.s).chat_json(
                    system=system, user=user, temperature=0.9
                )
                titles = data.get("titles") if isinstance(data, dict) else None
                if isinstance(titles, list):
                    return [str(t).strip() for t in titles if str(t).strip()]
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenRouter title invent failed: %s", exc)

        try:
            from src.services.llm import LLMClient

            raw = LLMClient(self.s).chat_json(
                system=system, user=user, temperature=0.9
            )
            titles = raw.get("titles") if isinstance(raw, dict) else None
            if isinstance(titles, list):
                return [str(t).strip() for t in titles if str(t).strip()]
        except Exception as exc:  # noqa: BLE001
            logger.warning("WaveSpeed title invent failed: %s", exc)
        return []


    def _load_smm_winner_signals(
        self, channel: str | None = None
    ) -> dict[str, Any]:
        """OpsStore read API for SMM channel_winners (Harvest consumer)."""
        try:
            if hasattr(self.store, "get_smm_winner_signals"):
                return self.store.get_smm_winner_signals(channel=channel)
            return load_smm_winner_signals(
                self.store.get_benchmarks(), channel=channel
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("smm winner signals load failed: %s", exc)
            return load_smm_winner_signals({}, channel=channel)

    def _blocked_titles(self, inspiration: dict[str, Any]) -> list[str]:
        return list(inspiration.get("blocked_titles") or [])

    def _too_close_to_blocked(self, title: str, blocked: list[str]) -> bool:
        for b in blocked:
            if _similar(title, b) >= 0.72:  # stricter than queue saturation
                return True
        return False

    def _saturation_ok(
        self, title: str, *, skip_row_index: int | None = None
    ) -> tuple[bool, list[str]]:
        lookback = int(self.cfg.get("saturation_lookback_days") or 30)
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback)
        errs: list[str] = []

        # Against current queue
        for row in self.queue.list_rows():
            if skip_row_index is not None and row.row_index == skip_row_index:
                continue
            if _similar(title, row.title) > 0.85:
                errs.append(f"saturation: too similar to queue '{row.title}'")
                return False, errs

        # Against harvest history within lookback window
        for h in self._harvest_history():
            try:
                at = datetime.fromisoformat(str(h.get("at", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if at < cutoff:
                continue
            if _similar(title, str(h.get("title") or "")) > 0.85:
                errs.append(
                    f"saturation: seen within {lookback}d — '{h.get('title')}'"
                )
                return False, errs
        return True, []

    def _harvest_history(self) -> list[dict[str, Any]]:
        path = self.store.root / "harvest_history.json"
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []

    def _record_harvest(
        self, prepared: list[dict[str, Any]], *, channel: str | None = None
    ) -> None:
        path = self.store.root / "harvest_history.json"
        hist = self._harvest_history()
        now = datetime.now(timezone.utc).isoformat()
        for p in prepared:
            hist.append(
                {
                    "id": f"har_{uuid.uuid4().hex[:8]}",
                    "title": p["title"],
                    "competitor_source": p.get("competitor_source") or "",
                    "channel": channel or "",
                    "at": now,
                }
            )
        # Keep ~1 year
        hist = hist[-2000:]
        path.write_text(json.dumps(hist, indent=2) + "\n", encoding="utf-8")

    def _inspiration_path(self, channel: str | None = None) -> Any:
        """Per-channel inspiration cache — avoids AlternateHistory* bleed into historian."""
        from pathlib import Path

        ch = (channel or "").strip().lower()
        root = Path(self.store.root)
        if ch and ch not in {"", "napstorian", "default"}:
            return root / f"last_inspiration_{ch}.json"
        return root / "last_inspiration.json"

    def _save_last_inspiration(
        self, inspiration: dict[str, Any], *, channel: str | None = None
    ) -> None:
        path = self._inspiration_path(channel)
        payload = {
            "at": datetime.now(timezone.utc).isoformat(),
            "channel": channel or "napstorian",
            "topics": inspiration.get("topics") or [],
            "blocked_titles": inspiration.get("blocked_titles") or [],
            "items": inspiration.get("items") or [],
            "good_videos": inspiration.get("good_videos"),
            "weak_videos_seen": inspiration.get("weak_videos_seen"),
            "video_filter": inspiration.get("video_filter")
            or _video_filter_cfg(self.cfg),
            "format": inspiration.get("format") or "",
            "title_invent_style": inspiration.get("title_invent_style") or "",
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _load_last_inspiration(
        self, channel: str | None = None
    ) -> dict[str, Any]:
        path = self._inspiration_path(channel)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}


def _extract_topics(title: str) -> list[str]:
    """Pull rough historical nouns — not usable as final titles."""
    t = re.sub(r"[^\w\s]", " ", title)
    stop = {
        "what", "if", "the", "a", "an", "and", "or", "to", "of", "in", "on", "for",
        "had", "have", "was", "were", "been", "this", "that", "with", "from", "how",
        "why", "when", "video", "history", "documentary", "full", "episode",
    }
    words = [w for w in t.split() if len(w) > 3 and w.lower() not in stop]
    out: list[str] = []
    if words:
        out.append(" ".join(words[:4]))
    return out


def _title_invent_cfg(
    cfg: dict[str, Any] | None, *, channel: str | None = None
) -> dict[str, Any]:
    """Resolve invent style from competitors JSON (channel-scoped).

    Defaults: napping_historian → documentary_mystery; else what_if (napstorian).
    """
    raw = (cfg or {}).get("title_invent")
    base: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    ch = (channel or "").strip().lower()
    style = str(base.get("style") or "").strip().lower()
    if not style:
        style = (
            "documentary_mystery"
            if ch == "napping_historian"
            else "what_if"
        )
    forbid = base.get("forbid_what_if_prefix")
    if forbid is None:
        forbid = style == "documentary_mystery"
    out = {
        "style": style,
        "forbid_what_if_prefix": bool(forbid),
        "format_description": base.get("format_description")
        or (
            "Tudor / general-history documentary mystery for night owls"
            if style == "documentary_mystery"
            else "What If … ? alternate-history documentary for history nerds"
        ),
        "llm_system": base.get("llm_system") or "",
        "llm_format_line": base.get("llm_format_line") or "",
        "query_inject_template": base.get("query_inject_template")
        or (
            "{theme} history documentary mystery"
            if style == "documentary_mystery"
            else "What If {theme} alternate history"
        ),
    }
    return out


def _is_what_if_title(title: str) -> bool:
    t = re.sub(r"\s+", " ", (title or "").strip()).lower()
    return t.startswith("what if ") or t == "what if" or t.startswith("what if?")


def _format_invented_title(
    title: str, *, invent_cfg: dict[str, Any] | None = None
) -> str:
    cfg = invent_cfg or {}
    style = str(cfg.get("style") or "what_if").strip().lower()
    if style == "documentary_mystery":
        return _format_documentary_title(title)
    return _format_what_if(title)


def _format_documentary_title(title: str) -> str:
    """Clean documentary/mystery titles — never force a What If prefix."""
    t = re.sub(r"\s+", " ", (title or "").strip())
    t = re.sub(r"\|.*$", "", t).strip()
    if not t:
        return ""
    # Strip accidental What If wrapping from mixed prompts / winners.
    if t.lower().startswith("what if "):
        rest = t[8:].strip()
        # Convert "What If X Had …" leftovers into documentary framing when possible.
        if rest:
            rest = re.sub(
                r"\?+\s*$",
                "",
                rest,
            ).strip()
            if rest.lower().startswith("the "):
                t = rest
            else:
                t = f"The Dark History of {rest.rstrip('?')}"
        else:
            return ""
    t = t.rstrip("?")
    # Soft length cap for sheet/YouTube
    return t[:120].strip()


def _format_what_if(title: str) -> str:
    t = re.sub(r"\s+", " ", (title or "").strip())
    t = re.sub(r"\|.*$", "", t).strip()
    if not t:
        return ""
    if not t.lower().startswith("what if"):
        t = f"What If {t}"
    if not t.endswith("?"):
        t = t.rstrip(".!") + "?"
    return t[:120]


def _topic_to_original_title(
    topic: str, *, invent_cfg: dict[str, Any] | None = None
) -> str:
    cfg = invent_cfg or {}
    style = str(cfg.get("style") or "what_if").strip().lower()
    topic = re.sub(r"\s+", " ", topic).strip()
    if not topic:
        return ""
    if style == "documentary_mystery":
        # Avoid turning SMM "What If … alternate history" injected queries into titles.
        low = topic.lower()
        if low.startswith("what if "):
            topic = re.sub(
                r"(?i)^what if\s+",
                "",
                topic,
            ).strip()
            topic = re.sub(
                r"(?i)\balternate history\b",
                "",
                topic,
            ).strip()
        if not topic or topic.lower().startswith("what if"):
            return ""
        if topic.lower().startswith("the "):
            return _format_documentary_title(topic)
        return _format_documentary_title(f"The Secret History of {topic}")
    return _topic_to_original_what_if(topic)


def _topic_to_original_what_if(topic: str) -> str:
    topic = re.sub(r"\s+", " ", topic).strip()
    if not topic:
        return ""
    if topic.lower().startswith("what if"):
        return _format_what_if(topic)
    return _format_what_if(f"What If {topic} Had Gone Differently?")


def _similar(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _load_competitors(channel: str | None = None) -> dict[str, Any]:
    from src.agents.competitors_agent import competitors_path

    path = competitors_path(channel)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _channel_url(channel_id: str, handle: str | None = None) -> str:
    if handle:
        h = handle if handle.startswith("@") else f"@{handle.lstrip('@')}"
        # Keep @ readable; only encode spaces/odd chars
        return f"https://www.youtube.com/{quote(h, safe='@')}"
    return f"https://www.youtube.com/channel/{channel_id}"


def _competitor_channel_url(c: dict[str, Any]) -> str:
    explicit = (c.get("url") or "").strip()
    if explicit.startswith("http") and "youtube.com" in explicit:
        # Normalize percent-encoded @ handles from older configs
        return explicit.replace("/%40", "/@")
    cid = (c.get("channel_id") or "").strip()
    handle = (c.get("handle") or "").strip() or None
    if cid:
        return _channel_url(cid, handle)
    if handle:
        return _channel_url("", handle)
    return ""


def _video_url(video_id: str) -> str:
    vid = (video_id or "").strip()
    if not vid:
        return ""
    return f"https://www.youtube.com/watch?v={vid}"


def _eligible_competitors(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in cfg.get("competitors") or []:
        if c.get("eligible_for_titles") is True:
            out.append(c)
    return out


def _channel_is_eligible(cfg: dict[str, Any], channel_id: str) -> bool:
    cid = (channel_id or "").strip()
    if not cid:
        return False
    for c in cfg.get("competitors") or []:
        if (c.get("channel_id") or "").strip() == cid:
            return bool(c.get("eligible_for_titles"))
    return False


def _competitor_by_id(cfg: dict[str, Any], channel_id: str) -> dict[str, Any] | None:
    cid = (channel_id or "").strip()
    for c in cfg.get("competitors") or []:
        if (c.get("channel_id") or "").strip() == cid:
            return c
    return None


def _video_filter_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """Video-level viral bar knobs (under power_filter)."""
    raw = cfg.get("power_filter")
    power = raw if isinstance(raw, dict) else {}
    min_likes = power.get("min_video_likes", 500)
    try:
        min_likes_n = int(min_likes) if min_likes is not None and min_likes != "" else None
    except (TypeError, ValueError):
        min_likes_n = 500
    try:
        floor = int(power.get("good_video_views", 100000) or 100000)
    except (TypeError, ValueError):
        floor = 100000
    try:
        ratio = float(power.get("good_video_vs_channel_avg", 1.0) or 1.0)
    except (TypeError, ValueError):
        ratio = 1.0
    return {
        "good_video_views": max(0, floor),
        "good_video_vs_channel_avg": max(0.0, ratio),
        "min_video_likes": min_likes_n,
    }


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _item_video_views(item: dict[str, Any] | None) -> int:
    if not item:
        return 0
    return _as_int(item.get("video_views")) or 0


def _channel_avg_for_item(item: dict[str, Any], cfg: dict[str, Any]) -> int | None:
    avg = _as_int(item.get("source_avg_recent_views"))
    if avg is not None:
        return avg
    comp = _competitor_by_id(cfg, item.get("channel_id") or "")
    if comp:
        return _as_int(comp.get("avg_recent_views"))
    return None


def _effective_video_views_bar(item: dict[str, Any], cfg: dict[str, Any]) -> int:
    """views must be >= max(absolute_floor, channel_avg * ratio) when avg known."""
    vf = _video_filter_cfg(cfg)
    floor = int(vf["good_video_views"])
    avg = _channel_avg_for_item(item, cfg)
    if avg is not None and avg > 0:
        relative = int(avg * float(vf["good_video_vs_channel_avg"]))
        return max(floor, relative)
    return floor


def _passes_good_video_bar(item: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """True when video clears absolute+relative views bar (and optional likes floor)."""
    if not (item.get("video_url") or "").strip():
        return False
    views = _as_int(item.get("video_views"))
    if views is None:
        return False
    if views < _effective_video_views_bar(item, cfg):
        return False
    min_likes = _video_filter_cfg(cfg).get("min_video_likes")
    if min_likes is not None:
        likes = _as_int(item.get("video_likes"))
        if likes is None or likes < int(min_likes):
            return False
    return True


def _enrich_items_with_video_stats(
    items: list[dict[str, Any]], *, api_key: str, cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    vids = [
        str(it.get("video_id") or "").strip()
        for it in items
        if (it.get("video_id") or "").strip()
    ]
    if not vids:
        return items
    import httpx

    from src.agents.youtube_data import YouTubeDataClient

    with httpx.Client(timeout=45.0) as client:
        yt = YouTubeDataClient(client, api_key)
        stats = yt.videos_stats(vids)
    for it in items:
        vid = (it.get("video_id") or "").strip()
        st = stats.get(vid) or {}
        if st:
            it["video_views"] = st.get("views")
            it["video_likes"] = st.get("likes")
            it["video_comments"] = st.get("comments")
            it["video_uploaded_at"] = st.get("published_at") or ""
        comp = _competitor_by_id(cfg, it.get("channel_id") or "")
        if comp:
            it["source_subs"] = comp.get("subscribers")
            it["source_avg_recent_views"] = comp.get("avg_recent_views")
            it["source_eligible"] = bool(comp.get("eligible_for_titles"))
    return items


def _stamps_from_item(
    item: dict[str, Any] | None, cfg: dict[str, Any]
) -> dict[str, Any]:
    it = item or {}
    cid = (it.get("channel_id") or "").strip()
    comp = _competitor_by_id(cfg, cid) if cid else None
    subs = it.get("source_subs")
    if subs is None and comp:
        subs = comp.get("subscribers")
    avg = it.get("source_avg_recent_views")
    if avg is None and comp:
        avg = comp.get("avg_recent_views")
    eligible = it.get("source_eligible")
    if eligible is None and comp is not None:
        eligible = bool(comp.get("eligible_for_titles"))
    return {
        "source_subs": "" if subs is None else str(subs),
        "source_avg_recent_views": "" if avg is None else str(avg),
        "source_eligible": (
            "TRUE"
            if eligible is True
            else ("FALSE" if eligible is False else "")
        ),
        "source_video_views": (
            "" if it.get("video_views") is None else str(it.get("video_views"))
        ),
        "source_video_likes": (
            "" if it.get("video_likes") is None else str(it.get("video_likes"))
        ),
        "source_video_comments": (
            ""
            if it.get("video_comments") is None
            else str(it.get("video_comments"))
        ),
        "source_video_uploaded_at": str(it.get("video_uploaded_at") or ""),
    }


def _channel_inspiration_items(
    cfg: dict[str, Any], *, eligible_only: bool = False
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pool = (
        _eligible_competitors(cfg) if eligible_only else list(cfg.get("competitors") or [])
    )
    for c in pool:
        url = _competitor_channel_url(c)
        if not url:
            continue
        label = (c.get("label") or c.get("handle") or c.get("channel_id") or "").strip()
        notes = (c.get("notes") or "").strip()
        topics = _extract_topics(f"{label} {notes}")
        out.append(
            {
                "kind": "competitor_channel",
                "video_id": "",
                "video_url": "",
                "channel_id": (c.get("channel_id") or "").strip(),
                "channel_url": url,
                "channel_label": label,
                "blocked_title": "",
                "topics": topics,
                "provenance_url": url,
                "source_subs": c.get("subscribers"),
                "source_avg_recent_views": c.get("avg_recent_views"),
                "source_eligible": bool(c.get("eligible_for_titles")),
            }
        )
    return out


def _inspiration_from_search_item(
    item: dict[str, Any],
    *,
    kind: str,
    competitor: dict[str, Any] | None,
) -> dict[str, Any] | None:
    sn = item.get("snippet") or {}
    raw = (sn.get("title") or "").strip()
    vid = ((item.get("id") or {}).get("videoId") or "").strip()
    channel_id = (sn.get("channelId") or "").strip()
    if competitor:
        channel_url = _competitor_channel_url(competitor)
        channel_label = (
            competitor.get("label")
            or competitor.get("handle")
            or sn.get("channelTitle")
            or channel_id
        )
        if not channel_id:
            channel_id = (competitor.get("channel_id") or "").strip()
    else:
        channel_label = (sn.get("channelTitle") or "yt_search").strip()
        channel_url = (
            _channel_url(channel_id) if channel_id else ""
        )
    video_url = _video_url(vid)
    provenance = video_url or channel_url
    if not provenance:
        return None
    return {
        "kind": kind,
        "video_id": vid,
        "video_url": video_url,
        "channel_id": channel_id,
        "channel_url": channel_url,
        "channel_label": channel_label,
        "blocked_title": raw,
        "topics": _extract_topics(raw),
        "provenance_url": provenance,
    }


def _inspiration_from_upload(
    upload: dict[str, Any],
    *,
    kind: str,
    competitor: dict[str, Any],
) -> dict[str, Any] | None:
    """Build inspiration entry from playlistItems upload row."""
    vid = (upload.get("video_id") or "").strip()
    raw = (upload.get("title") or "").strip()
    channel_id = (competitor.get("channel_id") or "").strip()
    channel_url = _competitor_channel_url(competitor)
    channel_label = (
        competitor.get("label")
        or competitor.get("handle")
        or channel_id
    )
    video_url = _video_url(vid)
    provenance = video_url or channel_url
    if not provenance:
        return None
    entry = {
        "kind": kind,
        "video_id": vid,
        "video_url": video_url,
        "channel_id": channel_id,
        "channel_url": channel_url,
        "channel_label": channel_label,
        "blocked_title": raw,
        "topics": _extract_topics(raw),
        "provenance_url": provenance,
        "source_subs": competitor.get("subscribers"),
        "source_avg_recent_views": competitor.get("avg_recent_views"),
        "source_eligible": bool(competitor.get("eligible_for_titles")),
    }
    published = (upload.get("published_at") or "").strip()
    if published:
        entry["video_uploaded_at"] = published
    return entry


def _dedupe_inspiration_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    # Videos first, then channels
    ordered = sorted(
        items,
        key=lambda x: (0 if x.get("video_url") else 1, x.get("kind") or ""),
    )
    for it in ordered:
        key = (it.get("video_url") or it.get("channel_url") or it.get("provenance_url") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _tokens(text: str) -> set[str]:
    stop = {
        "what", "if", "the", "a", "an", "and", "or", "to", "of", "in", "on", "for",
        "had", "have", "was", "were", "been", "this", "that", "with", "from", "how",
        "why", "when", "video", "history", "documentary", "full", "episode", "gone",
        "differently", "never", "would", "could",
    }
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 3 and w not in stop}


def _pick_provenance(
    title: str,
    items: list[dict[str, Any]],
    *,
    prefer_video: bool = True,
    used: set[str] | None = None,
) -> str:
    url, _item = _pick_provenance_item(
        title, items, prefer_video=prefer_video, used=used
    )
    return url


def _pick_provenance_item(
    title: str,
    items: list[dict[str, Any]],
    *,
    prefer_video: bool = True,
    used: set[str] | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Bind an invented title to the closest inspiration video/channel URL + item.

    Among topical matches, prefer the highest-view viral video.
    """
    if not items:
        return "", None
    used = used or set()
    title_toks = _tokens(title)
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for it in items:
        url = ""
        if prefer_video and it.get("video_url"):
            url = str(it["video_url"])
        else:
            url = str(it.get("provenance_url") or it.get("channel_url") or "")
        if not url:
            continue
        blob = " ".join(
            [
                str(it.get("blocked_title") or ""),
                " ".join(it.get("topics") or []),
                str(it.get("channel_label") or ""),
            ]
        )
        overlap = len(title_toks & _tokens(blob))
        sim = (
            _similar(title, str(it.get("blocked_title") or ""))
            if it.get("blocked_title")
            else 0.0
        )
        novelty = 0.15 if url not in used else 0.0
        video_bonus = 0.25 if it.get("video_url") and prefer_video else 0.0
        competitor_bonus = (
            0.1 if str(it.get("kind") or "").startswith("competitor") else 0.0
        )
        score = overlap * 1.0 + sim * 2.0 + novelty + video_bonus + competitor_bonus
        scored.append((score, url, it))

    if not scored:
        return "", None

    # Topical rank first, then pick highest-view among near-best topical matches
    scored.sort(key=lambda x: (-x[0], x[1]))
    best_topic = scored[0][0]
    topic_floor = max(0.35, best_topic * 0.75) if best_topic >= 0.35 else 0.0
    topical = [s for s in scored if s[0] >= topic_floor] or scored
    topical.sort(
        key=lambda x: (
            -_item_video_views(x[2]),
            -x[0],
            0 if x[1] not in used else 1,
            x[1],
        )
    )
    best_score, best_url, best_item = topical[0]
    if best_score < 0.35:
        for _score, url, it in topical:
            if url not in used:
                return url, it
        # All used — still bind highest-view topical match
        return best_url, best_item
    return best_url, best_item


def _fallback_channel_url(cfg: dict[str, Any]) -> str:
    for c in _eligible_competitors(cfg):
        url = _competitor_channel_url(c)
        if url:
            return url
    for c in cfg.get("competitors") or []:
        url = _competitor_channel_url(c)
        if url:
            return url
    return "https://www.youtube.com/"

def _needs_source_backfill(value: str) -> bool:
    v = (value or "").strip()
    if v in _PLACEHOLDER_SOURCES:
        return True
    if v.startswith("topic:"):
        return True
    # Legacy encoded @handles from quote(); rewrite to readable URLs
    if "%40" in v:
        return True
    if "youtube.com/" in v.lower() or "youtu.be/" in v.lower():
        return False
    # Non-URL legacy labels (channel names, etc.)
    return True


def _is_youtube_url(value: str) -> bool:
    v = (value or "").strip().lower()
    return "youtube.com/" in v or "youtu.be/" in v
