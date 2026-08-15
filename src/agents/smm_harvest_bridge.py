"""SMM → Harvest bridge: read channel winners and bias title invent.

SMM writes ``channel_winners`` into OpsStore ``benchmarks.json``
(via ``SocialMediaManager.learn_channel_winners`` / ``maybe_learn_channel_winners``,
refreshed by sleep_factory + watchdog ``_maybe_smm_scan``).

Harvest / TrendsAgent **reads** that surface to prefer winning themes —
never writes winners, never sets sheet ``approved``.

When ``smm.evergreen_bias`` is on, winner/query bias prefers themes with lasting
search demand (Tudor court, sealed letters, succession, sleep/calm mystery)
over flash-in-pan newsjacking or one-week meme formats. Dual-channel:
napstorian = evergreen What-If curiosity; napping_historian = evergreen
sleep/bedtime history — both avoid time-stamped "this week's news" hooks.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Quality→virality theme lexicon for Tudor / alternate-history farm.
_ENTITY_ALIASES: list[tuple[str, str]] = [
    ("henry viii", "Henry VIII"),
    ("henry the eighth", "Henry VIII"),
    ("anne boleyn", "Anne Boleyn"),
    ("anne of cleves", "Anne of Cleves"),
    ("lady jane grey", "Lady Jane Grey"),
    ("jane grey", "Lady Jane Grey"),
    ("mary i", "Mary I"),
    ("mary tudor", "Mary I"),
    ("thomas cromwell", "Thomas Cromwell"),
    ("elizabeth i", "Elizabeth I"),
    ("elizabeth the first", "Elizabeth I"),
    ("catherine of aragon", "Catherine of Aragon"),
    ("katherine howard", "Katherine Howard"),
    ("jane seymour", "Jane Seymour"),
    ("wolsey", "Wolsey"),
    ("tudor", "Tudor court"),
]

_ERA_ALIASES: list[tuple[str, str]] = [
    ("tudor", "Tudor"),
    ("medieval", "Medieval"),
    ("stuart", "Stuart"),
    ("plantagenet", "Plantagenet"),
    ("renaissance", "Renaissance"),
    ("reformation", "Reformation"),
    ("wars of the roses", "Wars of the Roses"),
]

_PACKAGING_TOKENS = (
    "what if",
    "imagine",
    "letter",
    "whisper",
    "illness",
    "feared",
    "coup",
    "queen",
    "spared",
    "divorced",
    "monster",
    "horrifying",
)

# Packaging patterns that compound for years (letter/whisper/what-if family).
_EVERGREEN_PACKAGING = (
    "what if",
    "letter",
    "whisper",
    "secret",
    "forgotten",
    "sealed",
    "imagine",
    "feared",
    "coup",
    "illness",
)

# Default lasting-demand themes (override via smm.evergreen_themes).
_DEFAULT_EVERGREEN_THEMES: tuple[str, ...] = (
    "Tudor court",
    "sealed letters",
    "succession crises",
    "sleep history",
    "calm mystery",
    "forgotten queens",
    "royal intrigue",
    "empire fall",
    "counterfactual divergence",
    "bedtime history",
    "court whisper",
    "primary sources",
)

# Needle → label for evergreen theme matching in titles/queries.
_EVERGREEN_THEME_ALIASES: list[tuple[str, str]] = [
    ("tudor court", "Tudor court"),
    ("tudor", "Tudor court"),
    ("sealed letter", "sealed letters"),
    ("secret letter", "sealed letters"),
    ("letter", "sealed letters"),
    ("succession", "succession crises"),
    ("heir", "succession crises"),
    ("nine day", "succession crises"),
    ("13-day", "succession crises"),
    ("thirteen day", "succession crises"),
    ("sleep", "sleep history"),
    ("bedtime", "bedtime history"),
    ("for sleep", "sleep history"),
    ("calm", "calm mystery"),
    ("whisper", "court whisper"),
    ("forgotten", "forgotten queens"),
    ("intrigue", "royal intrigue"),
    ("empire fall", "empire fall"),
    ("fall of", "empire fall"),
    ("what if", "counterfactual divergence"),
    ("alternate history", "counterfactual divergence"),
    ("primary source", "primary sources"),
    ("tower of london", "royal intrigue"),
    ("anne boleyn", "Tudor court"),
    ("henry viii", "Tudor court"),
]

_DEFAULT_EPHEMERAL_DEMOTES: tuple[str, ...] = (
    "this week",
    "breaking",
    "just happened",
    "trending now",
    "reacts to",
    "reaction to",
    "tiktok",
    "meme",
    "today only",
    "news today",
    "in the news",
    "right now",
    "viral challenge",
    "this month's",
    "2024 news",
    "2025 news",
    "2026 news",
)


def load_evergreen_cfg(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve ``smm.evergreen_*`` knobs (agents_settings or override dict)."""
    smm: dict[str, Any] = {}
    if isinstance(cfg, dict) and ("evergreen_bias" in cfg or "evergreen_themes" in cfg):
        smm = dict(cfg)
    elif isinstance(cfg, dict) and isinstance(cfg.get("smm"), dict):
        smm = dict(cfg["smm"])
    else:
        try:
            from src.services.settings import CONFIG_DIR

            path = CONFIG_DIR / "agents_settings.json"
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                smm = dict(raw.get("smm") or {})
        except Exception:  # noqa: BLE001
            smm = {}
    themes = smm.get("evergreen_themes")
    if not isinstance(themes, list) or not themes:
        themes = list(_DEFAULT_EVERGREEN_THEMES)
    demotes = smm.get("ephemeral_demote_patterns") or smm.get("ephemeral_demote")
    if not isinstance(demotes, list) or not demotes:
        demotes = list(_DEFAULT_EPHEMERAL_DEMOTES)
    return {
        "evergreen_bias": bool(smm.get("evergreen_bias", True)),
        "evergreen_boost": float(smm.get("evergreen_boost") or 0.08),
        "ephemeral_penalty": float(smm.get("ephemeral_penalty") or 0.10),
        "evergreen_themes": [str(t).strip() for t in themes if str(t).strip()],
        "ephemeral_demote_patterns": [
            str(p).strip().lower() for p in demotes if str(p).strip()
        ],
        "evergreen_query_inject": bool(smm.get("evergreen_query_inject", True)),
    }


def classify_evergreen(
    text: str,
    *,
    channel: str | None = None,
    evergreen_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Tag a title/query as evergreen vs ephemeral for harvest bias.

    Returns ``kind`` (``evergreen`` / ``ephemeral`` / ``neutral``), ``score``
    in roughly ``[-1, 1]``, theme hits, and ephemeral pattern hits.
    """
    cfg = evergreen_cfg if isinstance(evergreen_cfg, dict) else load_evergreen_cfg()
    low = (text or "").strip().lower()
    empty = {
        "kind": "neutral",
        "score": 0.0,
        "theme_hits": [],
        "ephemeral_hits": [],
        "packaging_hits": [],
        "channel_angle": "",
    }
    if not low:
        return empty
    ch = (channel or "").strip().lower()
    ephemeral_hits = [
        p for p in (cfg.get("ephemeral_demote_patterns") or []) if p and p in low
    ]
    theme_hits = _match_aliases(low, _EVERGREEN_THEME_ALIASES)
    # Also honor explicit allowlist labels present as substrings.
    for label in cfg.get("evergreen_themes") or []:
        lab = str(label).strip()
        if lab and lab.lower() in low and lab not in theme_hits:
            theme_hits.append(lab)
    packaging_hits = [tok for tok in _EVERGREEN_PACKAGING if tok in low]
    score = 0.0
    if theme_hits:
        score += 0.35 + 0.08 * min(3, len(theme_hits) - 1)
    if packaging_hits:
        score += 0.15 + 0.05 * min(2, len(packaging_hits) - 1)
    # Durable packaging shapes (letter/whisper/secret) without news hooks.
    if any(p in low for p in ("letter", "whisper", "secret", "forgotten", "what if")):
        score += 0.1
    if ephemeral_hits:
        score -= 0.55 + 0.1 * min(2, len(ephemeral_hits) - 1)
    # Channel angles: both evergreen, different packaging.
    channel_angle = ""
    if ch == "napping_historian":
        channel_angle = "sleep_bedtime_history"
        if any(x in low for x in ("sleep", "bedtime", "calm", "ambient", "whisper", "night")):
            score += 0.12
        if low.startswith("what if"):
            score -= 0.05  # soft — documentary prefers non-What-If
    elif ch in {"napstorian", "default", ""}:
        channel_angle = "evergreen_what_if_curiosity"
        if "what if" in low or "alternate" in low:
            score += 0.08
        if any(x in low for x in ("this week", "breaking", "news today")):
            score -= 0.2
    score = round(max(-1.0, min(1.0, score)), 3)
    if ephemeral_hits and score <= 0:
        kind = "ephemeral"
    elif score >= 0.25 or (theme_hits and not ephemeral_hits):
        kind = "evergreen"
    elif ephemeral_hits:
        kind = "ephemeral"
    else:
        kind = "neutral"
    return {
        "kind": kind,
        "score": score,
        "theme_hits": theme_hits[:8],
        "ephemeral_hits": ephemeral_hits[:6],
        "packaging_hits": packaging_hits[:6],
        "channel_angle": channel_angle,
    }


def tag_winner_evergreen(
    winners: list[dict[str, Any]],
    *,
    channel: str | None = None,
    evergreen_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Annotate winner rows + summarize evergreen share for SMM learn."""
    cfg = evergreen_cfg if isinstance(evergreen_cfg, dict) else load_evergreen_cfg()
    tagged: list[dict[str, Any]] = []
    evergreen_n = 0
    ephemeral_n = 0
    evergreen_hooks: list[str] = []
    for w in winners:
        row = dict(w)
        title = str(row.get("title") or "")
        eg = classify_evergreen(title, channel=channel, evergreen_cfg=cfg)
        row["evergreen_kind"] = eg["kind"]
        row["evergreen_score"] = eg["score"]
        row["evergreen_themes"] = eg["theme_hits"]
        if eg["kind"] == "evergreen":
            evergreen_n += 1
            hook = title.strip()
            if hook and hook not in evergreen_hooks:
                evergreen_hooks.append(hook)
        elif eg["kind"] == "ephemeral":
            ephemeral_n += 1
        tagged.append(row)
    n = max(len(tagged), 1)
    return {
        "top": tagged,
        "evergreen_share": round(evergreen_n / n, 2),
        "ephemeral_share": round(ephemeral_n / n, 2),
        "evergreen_hooks": evergreen_hooks[:5],
        "evergreen_bias": bool(cfg.get("evergreen_bias", True)),
    }


def load_smm_winner_signals(
    benchmarks: dict[str, Any] | None,
    *,
    channel: str | None = None,
    evergreen_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize ``benchmarks.channel_winners`` into a Harvest-ready read payload.

    Pure function aside from optional evergreen cfg load. Safe when benchmarks
    are missing/empty. When ``channel`` is set, prefer
    ``channel_winners_by_channel[channel]``.
    """
    bench = benchmarks if isinstance(benchmarks, dict) else {}
    ch = (channel or "").strip().lower()
    eg_cfg = (
        evergreen_cfg
        if isinstance(evergreen_cfg, dict)
        else load_evergreen_cfg()
    )
    cw: dict[str, Any] = {}
    by_ch = bench.get("channel_winners_by_channel")
    if ch and isinstance(by_ch, dict):
        row = by_ch.get(ch)
        if isinstance(row, dict) and (row.get("top") or row.get("patterns")):
            cw = row
    if not cw:
        legacy = bench.get("channel_winners")
        if isinstance(legacy, dict):
            # Only use legacy blob for napstorian / unset — avoid cross-channel bias.
            if not ch or ch in {"napstorian", "default"}:
                cw = legacy
    patterns = cw.get("patterns") if isinstance(cw.get("patterns"), dict) else {}
    top = [t for t in (cw.get("top") or []) if isinstance(t, dict)]
    titles = [str(t.get("title") or "").strip() for t in top if str(t.get("title") or "").strip()]
    hooks = [
        str(h).strip()
        for h in (patterns.get("title_hooks") or [])
        if str(h).strip()
    ]
    # Prefer evergreen hooks when SMM already tagged them during learn.
    eg_hooks = [
        str(h).strip()
        for h in (patterns.get("evergreen_hooks") or [])
        if str(h).strip()
    ]
    if eg_hooks and bool(eg_cfg.get("evergreen_bias", True)):
        hooks = list(dict.fromkeys([*eg_hooks, *hooks]))[:8]
    corpus = " | ".join(titles + hooks).lower()
    entities = _match_aliases(corpus, _ENTITY_ALIASES)
    eras = _match_aliases(corpus, _ERA_ALIASES)
    # Free-form topic phrases from winner titles (stopword-light).
    topic_phrases: list[str] = []
    for title in titles:
        phrase = _topic_phrase(title)
        if phrase and phrase.lower() not in {p.lower() for p in topic_phrases}:
            topic_phrases.append(phrase)
    packaging = [
        tok for tok in _PACKAGING_TOKENS if tok in corpus
    ]
    evergreen_packaging = [tok for tok in _EVERGREEN_PACKAGING if tok in corpus]
    title_tags: list[dict[str, Any]] = []
    evergreen_titles: list[str] = []
    for title in titles:
        eg = classify_evergreen(title, channel=ch or None, evergreen_cfg=eg_cfg)
        title_tags.append({"title": title, **eg})
        if eg["kind"] == "evergreen":
            evergreen_titles.append(title)
    evergreen_themes = _match_aliases(corpus, _EVERGREEN_THEME_ALIASES)
    for label in eg_cfg.get("evergreen_themes") or []:
        if str(label).lower() in corpus and label not in evergreen_themes:
            evergreen_themes.append(str(label))
    return {
        "updated_at": cw.get("updated_at"),
        "channel": ch or cw.get("channel"),
        "channel_id": cw.get("channel_id"),
        "sample": cw.get("sample"),
        "channel_median_views": cw.get("channel_median_views"),
        "median_winner_views": patterns.get("median_winner_views")
        or (bench.get("winner_views_median") if not ch or ch == "napstorian" else None),
        "titles": titles,
        "title_hooks": hooks,
        "entities": entities,
        "eras": eras,
        "topic_phrases": topic_phrases[:12],
        "packaging_patterns": packaging,
        "evergreen_packaging": evergreen_packaging,
        "evergreen_themes": evergreen_themes[:12],
        "evergreen_titles": evergreen_titles[:8],
        "evergreen_share": patterns.get("evergreen_share"),
        "ephemeral_share": patterns.get("ephemeral_share"),
        "title_evergreen_tags": title_tags[:12],
        "evergreen_bias": bool(eg_cfg.get("evergreen_bias", True)),
        "patterns": patterns,
        "has_winners": bool(titles or hooks),
    }


def winner_bias_for_title(
    title: str,
    signals: dict[str, Any] | None,
    *,
    max_boost: float = 0.18,
    invent_style: str | None = None,
    channel: str | None = None,
    evergreen_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score how closely an invented title resembles winning channel themes.

    Returns ``boost`` (additive to ``trend_score``), ``matched`` theme tags,
    and ``tag`` for sheet notes (``smm_winner_bias=...``). When evergreen bias
    is on, lasting themes get an extra boost and ephemeral hooks are demoted.
    """
    eg_cfg = (
        evergreen_cfg
        if isinstance(evergreen_cfg, dict)
        else load_evergreen_cfg()
    )
    empty = {
        "boost": 0.0,
        "matched": [],
        "tag": "",
        "entity_hits": [],
        "era_hits": [],
        "title_sim": 0.0,
        "evergreen_kind": "neutral",
        "evergreen_score": 0.0,
    }
    t = (title or "").strip()
    if not t:
        return empty
    low = t.lower()
    ch = (channel or (signals or {}).get("channel") or "").strip().lower() or None
    style = (invent_style or "").strip().lower()
    eg = classify_evergreen(t, channel=ch, evergreen_cfg=eg_cfg)
    # Soft evergreen-only path when winners are empty but bias is on.
    if not signals or not signals.get("has_winners"):
        if not bool(eg_cfg.get("evergreen_bias", True)):
            return empty
        boost = 0.0
        matched: list[str] = []
        if eg["kind"] == "evergreen":
            boost = min(float(eg_cfg.get("evergreen_boost") or 0.08), max_boost)
            matched = [f"evergreen:{h}" for h in (eg.get("theme_hits") or [])[:3]]
            if not matched:
                matched = ["evergreen:packaging"]
        elif eg["kind"] == "ephemeral":
            return {
                **empty,
                "boost": 0.0,
                "matched": [f"ephemeral:{h}" for h in eg.get("ephemeral_hits") or ["hook"]],
                "tag": "smm_winner_bias=ephemeral_demoted",
                "evergreen_kind": eg["kind"],
                "evergreen_score": eg["score"],
                "reject": False,
                "ephemeral_demote": True,
            }
        tag = ""
        if matched and boost > 0:
            tag = f"smm_winner_bias={','.join(matched[:4])}"
        return {
            **empty,
            "boost": round(boost, 3),
            "matched": matched,
            "tag": tag,
            "evergreen_kind": eg["kind"],
            "evergreen_score": eg["score"],
            "reject": False,
        }
    # Documentary channels: do not reward AlternateHistory-style What-If packaging
    # even if older polluted winners still sit in channel_winners.
    if style == "documentary_mystery" and low.startswith("what if"):
        return {
            **empty,
            "matched": ["reject_what_if_for_documentary"],
            "tag": "smm_winner_bias=reject_what_if_documentary",
            "evergreen_kind": eg["kind"],
            "evergreen_score": eg["score"],
            "reject": True,
        }
    matched = []
    entity_hits = [
        e for e in (signals.get("entities") or []) if e.lower() in low
    ]
    era_hits = [e for e in (signals.get("eras") or []) if e.lower() in low]
    for e in entity_hits:
        matched.append(f"entity:{e}")
    for e in era_hits:
        matched.append(f"era:{e}")
    for phrase in signals.get("topic_phrases") or []:
        # Require meaningful overlap (not single stopword).
        toks = [w for w in re.findall(r"[a-z0-9']+", phrase.lower()) if len(w) > 3]
        # Ignore pure what-if packaging tokens when scoring documentary titles.
        if style == "documentary_mystery":
            toks = [w for w in toks if w not in {"what", "alternate", "history"}]
        hits = sum(1 for w in toks if w in low)
        if toks and hits >= max(1, min(2, len(toks) // 2)):
            matched.append(f"theme:{phrase[:40]}")
            break
    for pack in signals.get("packaging_patterns") or []:
        if pack == "what if" and style == "documentary_mystery":
            continue
        if pack in low and pack not in ("what if",):  # format is expected for what_if
            matched.append(f"pack:{pack}")
    # Soft similarity vs a winner title (never copy — boost only).
    # Prefer evergreen winner titles when tagged.
    title_sim = 0.0
    winner_titles = list(signals.get("titles") or [])[:8]
    eg_titles = list(signals.get("evergreen_titles") or [])
    if bool(eg_cfg.get("evergreen_bias", True)) and eg_titles:
        winner_titles = list(dict.fromkeys([*eg_titles, *winner_titles]))[:8]
    if style == "documentary_mystery":
        # Prefer comparing against non-What-If winners when available.
        filtered = [w for w in winner_titles if not w.lower().startswith("what if")]
        if filtered:
            winner_titles = filtered
    for wt in winner_titles:
        title_sim = max(title_sim, _similar(t, wt))
    # Reject near-copies from own winners too (caller still uses competitor blocklist).
    if title_sim >= 0.88:
        return {
            "boost": 0.0,
            "matched": ["too_close_to_own_winner"],
            "tag": "smm_winner_bias=too_close_skipped",
            "entity_hits": entity_hits,
            "era_hits": era_hits,
            "title_sim": title_sim,
            "evergreen_kind": eg["kind"],
            "evergreen_score": eg["score"],
            "reject": True,
        }
    boost = 0.0
    if entity_hits:
        boost += 0.08 + 0.02 * min(2, len(entity_hits) - 1)
    if era_hits:
        boost += 0.03
    if any(m.startswith("theme:") for m in matched):
        boost += 0.05
    if any(m.startswith("pack:") for m in matched):
        boost += 0.02
    # Soft title similarity only reinforces an already-matched theme (avoid
    # boosting unrelated bank titles that merely share "What If …?" shape).
    if matched and 0.35 <= title_sim < 0.88:
        boost += min(0.06, (title_sim - 0.35) * 0.12)
    # Evergreen / ephemeral adjustment on top of winner similarity.
    if bool(eg_cfg.get("evergreen_bias", True)):
        eg_boost = float(eg_cfg.get("evergreen_boost") or 0.08)
        eg_pen = float(eg_cfg.get("ephemeral_penalty") or 0.10)
        if eg["kind"] == "evergreen":
            boost += eg_boost
            for h in (eg.get("theme_hits") or [])[:2]:
                matched.append(f"evergreen:{h}")
            if not eg.get("theme_hits") and eg.get("packaging_hits"):
                matched.append("evergreen:packaging")
        elif eg["kind"] == "ephemeral":
            boost = max(0.0, boost - eg_pen)
            for h in (eg.get("ephemeral_hits") or [])[:2]:
                matched.append(f"ephemeral:{h}")
        # Signal-level evergreen themes from winners also reinforce.
        for th in signals.get("evergreen_themes") or []:
            if str(th).lower() in low:
                matched.append(f"evergreen_sig:{th}")
                boost += 0.02
                break
    # Cap slightly higher when evergreen bias adds lasting-theme lift.
    cap = max_boost + (
        0.06 if bool(eg_cfg.get("evergreen_bias", True)) and eg["kind"] == "evergreen" else 0.0
    )
    boost = round(min(cap, max(0.0, boost)), 3)
    # Dedupe matched labels while preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for m in matched:
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    tag = ""
    if uniq and boost > 0:
        compact = ",".join(uniq[:4])
        tag = f"smm_winner_bias={compact}"
    elif uniq and eg["kind"] == "ephemeral":
        tag = "smm_winner_bias=ephemeral_demoted"
    return {
        "boost": boost,
        "matched": uniq,
        "tag": tag,
        "entity_hits": entity_hits,
        "era_hits": era_hits,
        "title_sim": round(title_sim, 3),
        "evergreen_kind": eg["kind"],
        "evergreen_score": eg["score"],
        "reject": False,
        "ephemeral_demote": eg["kind"] == "ephemeral",
    }


def prefer_queries_for_winners(
    queries: list[str],
    signals: dict[str, Any] | None,
    *,
    invent_style: str | None = None,
    query_inject_template: str | None = None,
    channel: str | None = None,
    evergreen_cfg: dict[str, Any] | None = None,
) -> list[str]:
    """Reorder search queries so those matching winning themes come first.

    Also injects light winner-theme query variants (entities / eras) when missing.
    Style-aware: documentary_mystery channels do NOT inject ``What If …`` probes.
    With evergreen bias, lasting-demand themes rank above ephemeral newsjacking.
    """
    eg_cfg = (
        evergreen_cfg
        if isinstance(evergreen_cfg, dict)
        else load_evergreen_cfg()
    )
    base = [str(q).strip() for q in (queries or []) if str(q).strip()]
    ch = (channel or (signals or {}).get("channel") or "").strip().lower() or None
    style = (invent_style or "").strip().lower()
    tmpl = (query_inject_template or "").strip()
    if not tmpl:
        if style == "documentary_mystery":
            tmpl = "{theme} history documentary mystery"
        else:
            tmpl = "What If {theme} alternate history"

    themes = []
    if signals and signals.get("has_winners"):
        themes = [
            *(signals.get("entities") or [])[:4],
            *(signals.get("eras") or [])[:2],
            *(signals.get("topic_phrases") or [])[:3],
        ]
    # Prefer evergreen theme allowlist / winner evergreen themes for inject.
    if bool(eg_cfg.get("evergreen_bias", True)) and bool(
        eg_cfg.get("evergreen_query_inject", True)
    ):
        eg_themes = list(signals.get("evergreen_themes") or []) if signals else []
        allow = list(eg_cfg.get("evergreen_themes") or [])[:6]
        # Channel-specific inject defaults.
        if ch == "napping_historian":
            allow = list(
                dict.fromkeys(
                    [
                        "sleep history",
                        "bedtime history",
                        "Tudor court",
                        "calm mystery",
                        "sealed letters",
                        *allow,
                    ]
                )
            )[:8]
        else:
            allow = list(
                dict.fromkeys(
                    [
                        "counterfactual divergence",
                        "Tudor court",
                        "succession crises",
                        "sealed letters",
                        *allow,
                    ]
                )
            )[:8]
        themes = list(dict.fromkeys([*eg_themes[:4], *allow, *themes]))

    if not themes and not (signals and signals.get("has_winners")):
        # Still demote ephemeral queries even without winners.
        if not bool(eg_cfg.get("evergreen_bias", True)):
            return base

        def _bare_rank(q: str) -> tuple[int, float]:
            eg = classify_evergreen(q, channel=ch, evergreen_cfg=eg_cfg)
            kind_rank = {"evergreen": 2, "neutral": 1, "ephemeral": 0}.get(eg["kind"], 1)
            return (-kind_rank, -float(eg["score"]))

        return sorted(base, key=_bare_rank)

    injected: list[str] = []
    for th in themes:
        # Skip what-if-shaped topic phrases when inventing documentary titles.
        th_s = str(th).strip()
        if not th_s:
            continue
        if style == "documentary_mystery" and th_s.lower().startswith("what if"):
            th_s = re.sub(r"(?i)^what if\s+", "", th_s).strip()
            th_s = re.sub(r"(?i)\balternate history\b", "", th_s).strip()
            if not th_s:
                continue
        # Map abstract evergreen labels into searchable phrases.
        search_theme = th_s
        low_th = th_s.lower()
        if low_th == "counterfactual divergence":
            search_theme = "alternate history"
        elif low_th == "sealed letters":
            search_theme = "secret royal letters"
        elif low_th == "succession crises":
            search_theme = "royal succession crisis"
        elif low_th in {"sleep history", "bedtime history"}:
            search_theme = "history documentary for sleep"
        elif low_th == "calm mystery":
            search_theme = "calm history mystery"
        cand = tmpl.replace("{theme}", search_theme)
        if style == "documentary_mystery" and cand.lower().startswith("what if"):
            continue
        if not any(_similar(cand, q) > 0.85 for q in base + injected):
            injected.append(cand)
    combined = base + injected
    theme_blob = " ".join(themes).lower()

    def _rank(q: str) -> tuple[int, int, int, int]:
        ql = q.lower()
        eg = classify_evergreen(q, channel=ch, evergreen_cfg=eg_cfg)
        overlap = sum(1 for th in themes if th.lower() in ql) if themes else 0
        soft = 1 if theme_blob and any(
            tok in ql for tok in theme_blob.split() if len(tok) > 3
        ) else 0
        evergreen_rank = 0
        if bool(eg_cfg.get("evergreen_bias", True)):
            if eg["kind"] == "evergreen":
                evergreen_rank = 2
            elif eg["kind"] == "ephemeral":
                evergreen_rank = -2
            elif eg["score"] > 0:
                evergreen_rank = 1
        # Higher overlap / evergreen first; demote ephemeral.
        return (-evergreen_rank, -overlap, -soft, 0)

    return sorted(combined, key=_rank)


def merge_winner_topics(
    topics: list[str],
    signals: dict[str, Any] | None,
    *,
    invent_style: str | None = None,
    evergreen_cfg: dict[str, Any] | None = None,
) -> list[str]:
    """Prepend winner entities / topic phrases so invent + LLM prefer them."""
    out: list[str] = []
    seen: set[str] = set()
    sig = signals if isinstance(signals, dict) else {}
    style = (invent_style or "").strip().lower()
    eg_cfg = (
        evergreen_cfg
        if isinstance(evergreen_cfg, dict)
        else load_evergreen_cfg()
    )
    front: list[str] = []
    if bool(eg_cfg.get("evergreen_bias", True)):
        front.extend(list(sig.get("evergreen_themes") or [])[:6])
        front.extend(list(eg_cfg.get("evergreen_themes") or [])[:4])
    for t in [
        *front,
        *list(sig.get("entities") or []),
        *list(sig.get("eras") or []),
        *list(sig.get("topic_phrases") or []),
        *list(topics or []),
    ]:
        s = str(t).strip()
        if style == "documentary_mystery" and s.lower().startswith("what if"):
            s = re.sub(r"(?i)^what if\s+", "", s).strip()
            s = re.sub(r"(?i)\balternate history\b", "", s).strip()
        key = s.lower()
        if not s or key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def apply_winner_bias_to_row(
    row: dict[str, Any],
    signals: dict[str, Any] | None,
    *,
    invent_style: str | None = None,
    channel: str | None = None,
    evergreen_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mutate-copy a prepared harvest row: boost score + notes tag."""
    out = dict(row)
    bias = winner_bias_for_title(
        str(out.get("title") or ""),
        signals,
        invent_style=invent_style,
        channel=channel,
        evergreen_cfg=evergreen_cfg,
    )
    if bias.get("reject"):
        out["_smm_winner_reject"] = True
        out["_smm_winner_bias"] = bias
        return out
    boost = float(bias.get("boost") or 0.0)
    notes = str(out.get("notes") or "").strip()
    if boost > 0:
        base = float(out.get("trend_score") or out.get("score") or 0.55)
        out["trend_score"] = round(min(0.97, base + boost), 3)
        if "score" in out:
            out["score"] = out["trend_score"]
        tag = bias.get("tag") or ""
        if tag:
            out["notes"] = f"{notes}; {tag}" if notes else tag
    elif bias.get("ephemeral_demote") and bias.get("tag"):
        # Leave score alone but stamp demotion for feedback / ops.
        tag = str(bias["tag"])
        out["notes"] = f"{notes}; {tag}" if notes else tag
    out["_smm_winner_bias"] = bias
    return out


def summarize_bias(prepared: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts for smoke / feedback JSON."""
    biased = 0
    rejected = 0
    evergreen_n = 0
    ephemeral_n = 0
    tags: list[str] = []
    for row in prepared:
        b = row.get("_smm_winner_bias") or {}
        kind = str(b.get("evergreen_kind") or "")
        if kind == "evergreen":
            evergreen_n += 1
        elif kind == "ephemeral":
            ephemeral_n += 1
        if b.get("reject"):
            rejected += 1
            continue
        if float(b.get("boost") or 0) > 0:
            biased += 1
            if b.get("tag"):
                tags.append(str(b["tag"]))
    return {
        "titles": len(prepared),
        "biased": biased,
        "rejected_too_close": rejected,
        "unbiased": max(0, len(prepared) - biased - rejected),
        "evergreen": evergreen_n,
        "ephemeral": ephemeral_n,
        "tags_sample": tags[:8],
    }


def write_smm_harvest_feedback(
    root: Path | None,
    *,
    signals: dict[str, Any],
    prepared: list[dict[str, Any]],
    dry_run: bool = False,
    channel: str | None = None,
) -> Path | None:
    """Write ``output/ops/smm_harvest_feedback_last.json`` (optional bus artifact)."""
    if root is not None:
        base = Path(root)
    else:
        try:
            from src.agents.store import OpsStore

            base = OpsStore().root
        except Exception:  # noqa: BLE001
            try:
                from src.services.settings import OUTPUT_DIR

                out = Path(OUTPUT_DIR)
                base = out if out.name == "ops" else out / "ops"
            except Exception:  # noqa: BLE001
                base = Path("output/ops")
    base.mkdir(parents=True, exist_ok=True)
    path = base / "smm_harvest_feedback_last.json"
    counts = summarize_bias(prepared)
    influenced = []
    for row in prepared:
        b = row.get("_smm_winner_bias") or {}
        if float(b.get("boost") or 0) <= 0 and not b.get("reject") and not b.get(
            "ephemeral_demote"
        ):
            continue
        influenced.append(
            {
                "title": row.get("title"),
                "trend_score": row.get("trend_score"),
                "boost": b.get("boost"),
                "matched": b.get("matched"),
                "evergreen_kind": b.get("evergreen_kind"),
                "notes": row.get("notes"),
            }
        )
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": bool(dry_run),
        "channel": channel,
        "winners": {
            "updated_at": signals.get("updated_at"),
            "channel_id": signals.get("channel_id"),
            "titles": (signals.get("titles") or [])[:8],
            "entities": signals.get("entities") or [],
            "eras": signals.get("eras") or [],
            "packaging_patterns": signals.get("packaging_patterns") or [],
            "evergreen_themes": signals.get("evergreen_themes") or [],
            "evergreen_share": signals.get("evergreen_share"),
            "has_winners": bool(signals.get("has_winners")),
        },
        "counts": counts,
        "influenced": influenced[:20],
        "note": (
            "SMM→Harvest bias only (winner + evergreen); approved stays false — "
            "human gate unchanged. Prefer lasting search themes over newsjacking."
        ),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def strip_internal_bias_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Remove ephemeral ``_smm_*`` keys before sheet append / ledger."""
    return {k: v for k, v in row.items() if not str(k).startswith("_smm_")}


# --------------------------------------------------------------------------- helpers


def _match_aliases(corpus: str, aliases: list[tuple[str, str]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for needle, label in aliases:
        if needle in corpus and label not in seen:
            seen.add(label)
            out.append(label)
    return out


def _topic_phrase(title: str) -> str:
    t = re.sub(r"#\w+", " ", title or "")
    t = re.sub(r"[^\w\s']", " ", t)
    stop = {
        "what", "if", "the", "a", "an", "and", "or", "to", "of", "in", "on", "for",
        "had", "have", "was", "were", "been", "this", "that", "with", "from", "how",
        "why", "when", "video", "history", "documentary", "full", "episode", "you",
        "didn", "t", "know", "before", "became", "everyone", "tried", "hide",
    }
    words = [w for w in t.split() if len(w) > 2 and w.lower() not in stop]
    if not words:
        return ""
    return " ".join(words[:5])


def _similar(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()
