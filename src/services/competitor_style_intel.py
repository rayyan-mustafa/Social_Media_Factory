"""Extract competitor winner style signals → cluster hints for SMM editing levers."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.store import OPS_DIR
from src.services.editing_overrides import get_channel_profile, load_style_profiles
from src.services.settings import CONFIG_DIR

INTEL_PATH = OPS_DIR / "competitor_style_intel.json"

# Title token buckets (no paid APIs beyond existing YT Data when available)
SLEEP_KW = frozenset(
    {
        "sleep",
        "fall asleep",
        "bedtime",
        "asmr",
        "calm",
        "relax",
        "meditation",
        "dream",
        "whisper",
        "soothing",
        "night",
    }
)
DRAMA_KW = frozenset(
    {
        "what if",
        "alternate",
        "timeline",
        "counterfactual",
        "divergence",
        "could have",
        "never happened",
        "changed history",
        "scenario",
    }
)
MAP_KW = frozenset({"map", "border", "empire", "territory", "invasion", "conquest"})
MYSTERY_KW = frozenset(
    {
        "secret",
        "mystery",
        "forgotten",
        "what really happened",
        "dark history",
        "haunts",
        "sealed",
        "hidden",
        "unexplained",
        "letter",
        "trial",
        "execution",
        "why did",
    }
)

# Style-role weights: History Calling–tier anchors beat FoC aspirational epics.
STYLE_ROLE_WEIGHT = {
    "primary_anchor": 3.0,
    "style_peer": 1.5,
    "optional_format": 0.8,
    "ambient_sleep": 0.5,
    "aspirational_reference": 0.12,
}

# Labels that must never dominate historian style/length targets.
HISTORIAN_DEPRIORITIZE_LABELS = frozenset(
    {
        "fall of civilizations",
        "unknown frequencies",
        "extra history",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _metrics_csv(channel: str) -> Path:
    if channel == "napping_historian":
        return OPS_DIR / "competitors_metrics_napping_historian.csv"
    return OPS_DIR / "competitors_metrics.csv"


def _inspiration_path(channel: str) -> Path:
    if channel == "napping_historian":
        return OPS_DIR / "last_inspiration_napping_historian.json"
    return OPS_DIR / "last_inspiration.json"


def _competitors_config_path(channel: str) -> Path:
    if channel == "napping_historian":
        return CONFIG_DIR / "competitors_napping_historian.json"
    return CONFIG_DIR / "competitors.json"


def _load_competitor_style_index(channel: str) -> dict[str, dict[str, Any]]:
    """Map competitor label/handle → style_role / priority from config."""
    data = _read_json(_competitors_config_path(channel))
    out: dict[str, dict[str, Any]] = {}
    for c in data.get("competitors") or []:
        if not isinstance(c, dict):
            continue
        meta = {
            "style_role": str(c.get("style_role") or "style_peer").strip().lower(),
            "style_priority": int(c.get("style_priority") or 50),
            "eligible_for_titles": bool(c.get("eligible_for_titles")),
            "label": str(c.get("label") or ""),
        }
        for key in (
            str(c.get("label") or "").strip().lower(),
            str(c.get("handle") or "").strip().lower().lstrip("@"),
            str(c.get("channel_id") or "").strip().lower(),
        ):
            if key:
                out[key] = meta
    return out


def _style_weight_for_label(channel: str, label: str | None, index: dict[str, dict[str, Any]] | None = None) -> float:
    idx = index if index is not None else _load_competitor_style_index(channel)
    key = str(label or "").strip().lower()
    meta = idx.get(key) or {}
    role = str(meta.get("style_role") or "style_peer")
    weight = float(STYLE_ROLE_WEIGHT.get(role, 1.0))
    if key in HISTORIAN_DEPRIORITIZE_LABELS or any(b in key for b in HISTORIAN_DEPRIORITIZE_LABELS):
        weight = min(weight, STYLE_ROLE_WEIGHT["aspirational_reference"])
    return weight


def _title_style_signals(title: str) -> dict[str, Any]:
    t = (title or "").lower()
    return {
        "sleep": any(k in t for k in SLEEP_KW),
        "drama_what_if": any(k in t for k in DRAMA_KW),
        "map_hint": any(k in t for k in MAP_KW),
        "mystery": any(k in t for k in MYSTERY_KW),
        "word_count": len(t.split()),
        "has_question": "?" in title,
    }


def _infer_form_factor(
    *,
    duration_s: float | None,
    title: str,
    channel: str,
) -> str:
    sig = _title_style_signals(title)
    if channel == "napstorian" or sig["drama_what_if"]:
        if duration_s is not None and duration_s <= 480:
            return "short_form"
        return "mid_form_drama"
    # Historian: History Calling–tier mystery TV-doc (~45–90 min), not FoC 3–4h default.
    if duration_s is not None:
        if duration_s >= 7200:
            return "epic_aspirational"  # FoC-length — never default style target
        if duration_s <= 480:
            return "short_form"
        if 900 <= duration_s <= 5400 or sig["mystery"]:
            return "mystery_doc"
        if duration_s > 5400:
            return "long_form"
    if sig["sleep"] and not sig["mystery"]:
        return "long_form_sleep"
    if channel == "napping_historian":
        return "mystery_doc"
    return "mid_form"


def _load_competitor_videos(channel: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    insp = _read_json(_inspiration_path(channel))
    for item in insp.get("items") or []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") != "competitor_video":
            continue
        title = str(item.get("blocked_title") or item.get("title") or "").strip()
        if not title:
            continue
        rows.append(
            {
                "video_id": item.get("video_id"),
                "title": title,
                "views": item.get("video_views"),
                "likes": item.get("video_likes"),
                "uploaded_at": item.get("video_uploaded_at"),
                "channel_label": item.get("channel_label"),
                "good_video": bool(item.get("good_video")),
            }
        )
    return rows


def _fetch_yt_durations(video_ids: list[str]) -> dict[str, float]:
    """Optional YouTube Data API duration lookup ($0 quota — existing key)."""
    ids = [v for v in video_ids if v][:50]
    if not ids:
        return {}
    try:
        import os

        import httpx

        api_key = (os.getenv("YOUTUBE_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
        if not api_key:
            return {}
        out: dict[str, float] = {}
        for i in range(0, len(ids), 50):
            batch = ids[i : i + 50]
            resp = httpx.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={
                    "part": "contentDetails,snippet",
                    "id": ",".join(batch),
                    "key": api_key,
                },
                timeout=30.0,
            )
            if resp.status_code != 200:
                break
            for item in resp.json().get("items") or []:
                vid = item.get("id")
                dur = ((item.get("contentDetails") or {}).get("duration")) or ""
                secs = _iso8601_duration_seconds(dur)
                if vid and secs is not None:
                    out[str(vid)] = secs
        return out
    except Exception:  # noqa: BLE001
        return {}


def _iso8601_duration_seconds(raw: str | None) -> float | None:
    if not raw:
        return None
    m = re.fullmatch(
        r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)(?:\.\d+)?S)?",
        raw.strip().upper(),
    )
    if not m:
        return None
    days, hours, mins, secs = (int(x or 0) for x in m.groups())
    return float(days * 86400 + hours * 3600 + mins * 60 + secs)


def _publish_hour_from_iso(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return int(dt.hour)
    except ValueError:
        return None


def cluster_top_performers(
    channel: str,
    *,
    videos: list[dict[str, Any]] | None = None,
    durations: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Cluster competitor winners → style hint deltas vs seeded profile."""
    vids = videos if videos is not None else _load_competitor_videos(channel)
    durations = durations or {}
    style_index = _load_competitor_style_index(channel)
    # Top by style-weighted views (History Calling–tier beats FoC aspirational).
    scored = []
    for v in vids:
        views = v.get("views")
        try:
            views_f = float(views) if views is not None else 0.0
        except (TypeError, ValueError):
            views_f = 0.0
        if views_f <= 0:
            continue
        vid = str(v.get("video_id") or "")
        dur = durations.get(vid)
        title = str(v.get("title") or "")
        label = str(v.get("channel_label") or "")
        weight = _style_weight_for_label(channel, label, style_index)
        form = _infer_form_factor(duration_s=dur, title=title, channel=channel)
        sig = _title_style_signals(title)
        scored.append(
            {
                **v,
                "views_f": views_f,
                "style_weight": weight,
                "weighted_views": views_f * weight,
                "duration_s": dur,
                "form_factor": form,
                **sig,
            }
        )

    scored.sort(key=lambda x: x["weighted_views"], reverse=True)
    # Prefer non-aspirational forms for the cluster head when available.
    preferred = [
        s
        for s in scored
        if s.get("form_factor") not in {"epic_aspirational"}
        and float(s.get("style_weight") or 0) >= 0.5
    ]
    pool = preferred if len(preferred) >= 3 else scored
    top = pool[: max(5, min(10, len(pool)))]
    if not top:
        return {"channel": channel, "n_top": 0, "cluster": None, "hints": {}}

    forms = Counter(str(t.get("form_factor")) for t in top)
    dominant_form = forms.most_common(1)[0][0]
    # Never let FoC-length dominate historian defaults.
    if channel == "napping_historian" and dominant_form == "epic_aspirational":
        dominant_form = "mystery_doc"
    sleep_ratio = sum(1 for t in top if t.get("sleep")) / len(top)
    drama_ratio = sum(1 for t in top if t.get("drama_what_if")) / len(top)
    map_ratio = sum(1 for t in top if t.get("map_hint")) / len(top)
    mystery_ratio = sum(1 for t in top if t.get("mystery")) / len(top)
    avg_words = sum(int(t.get("word_count") or 0) for t in top) / len(top)
    hours = [h for t in top if (h := _publish_hour_from_iso(t.get("uploaded_at"))) is not None]
    top_hours = [h for h, _ in Counter(hours).most_common(3)] if hours else []

    profile = get_channel_profile(channel)
    hints: dict[str, Any] = {}
    thumb = dict(profile.get("thumbnail_style") or {})
    if map_ratio >= 0.4 and channel == "napstorian":
        hints["thumbnail_style"] = {**thumb, "map_vs_face": "map", "focal_subject": "map"}
    elif sleep_ratio >= 0.5 or (channel == "napping_historian" and mystery_ratio >= 0.3):
        hints["thumbnail_style"] = {
            **thumb,
            "mood": "moody_calm",
            "word_count_max": min(int(thumb.get("word_count_max") or 5), 4),
        }
    if drama_ratio >= 0.5 and channel == "napstorian":
        hints["hook_pattern"] = {
            **(profile.get("hook_pattern") or {}),
            "default": "ticking_clock",
        }
    if channel == "napping_historian" and dominant_form in {
        "mystery_doc",
        "long_form",
        "long_form_sleep",
    }:
        pacing = dict(profile.get("pacing") or {})
        pacing.update(
            {
                "target_length_band_min_s": int(
                    pacing.get("target_length_band_min_s") or 2700
                ),
                "target_length_band_max_s": int(
                    pacing.get("target_length_band_max_s") or 5400
                ),
                "format_mode": "standard",
            }
        )
        hints["pacing"] = pacing
        hints["compose"] = {
            **(profile.get("compose") or {}),
            "sfx_density": "none",
            "beat_interval_s": 0,
            "sfx_event_cap": 0,
        }
        hints["hook_pattern"] = {
            **(profile.get("hook_pattern") or {}),
            "default": (profile.get("hook_pattern") or {}).get("default")
            or "false_assumption",
        }
        hints["motion"] = {
            **(profile.get("motion") or {}),
            "motion_mode": "ken_burns",
        }
        hints["audio"] = {
            **(profile.get("audio") or {}),
            "score_mode": "ambient_soft",
            "historian_sfx_off": True,
        }
    elif dominant_form == "long_form_sleep":
        # Sleep ASMR is tone-only — keep History Calling mid-length band, not FoC.
        hints["pacing"] = {
            **(profile.get("pacing") or {}),
            "target_length_band_min_s": int(
                (profile.get("pacing") or {}).get("target_length_band_min_s") or 2700
            ),
            "target_length_band_max_s": int(
                (profile.get("pacing") or {}).get("target_length_band_max_s") or 5400
            ),
            "format_mode": "standard",
        }
        hints["compose"] = {
            **(profile.get("compose") or {}),
            "sfx_density": "none",
            "beat_interval_s": 0,
        }
    elif dominant_form in {"mid_form_drama", "short_form"} and channel == "napstorian":
        hints["compose"] = {
            **(profile.get("compose") or {}),
            "beat_interval_s": 50,
            "overlay_cadence_s": 55,
        }

    return {
        "channel": channel,
        "n_top": len(top),
        "dominant_form": dominant_form,
        "sleep_ratio": round(sleep_ratio, 3),
        "drama_ratio": round(drama_ratio, 3),
        "map_ratio": round(map_ratio, 3),
        "mystery_ratio": round(mystery_ratio, 3),
        "avg_title_words": round(avg_words, 1),
        "top_publish_hours_utc": top_hours,
        "top_titles": [t.get("title") for t in top[:5]],
        "top_channel_labels": [t.get("channel_label") for t in top[:5]],
        "style_roles_used": sorted(
            {
                str(
                    (style_index.get(str(t.get("channel_label") or "").lower()) or {}).get(
                        "style_role"
                    )
                    or "unknown"
                )
                for t in top
            }
        ),
        "hints": hints,
    }


def extract_competitor_style_intel(
    channel: str,
    *,
    fetch_durations: bool = True,
) -> dict[str, Any]:
    """Full intel pass for one Brand channel."""
    videos = _load_competitor_videos(channel)
    durations: dict[str, float] = {}
    if fetch_durations:
        ids = [str(v.get("video_id") or "") for v in videos if v.get("video_id")]
        durations = _fetch_yt_durations(ids)

    cluster = cluster_top_performers(channel, videos=videos, durations=durations)
    csv_path = _metrics_csv(channel)
    channel_meta: dict[str, Any] = {"csv": str(csv_path), "csv_exists": csv_path.is_file()}
    if csv_path.is_file():
        with csv_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        channel_meta["n_competitor_channels"] = len(rows)
        channel_meta["eligible_channels"] = sum(
            1 for r in rows if str(r.get("eligible_for_titles", "")).upper() == "TRUE"
        )

    style_index = _load_competitor_style_index(channel)
    anchors = [
        meta["label"]
        for meta in style_index.values()
        if meta.get("style_role") == "primary_anchor" and meta.get("label")
    ]
    # de-dupe preserving order
    seen: set[str] = set()
    anchor_labels = []
    for a in anchors:
        if a not in seen:
            seen.add(a)
            anchor_labels.append(a)

    return {
        "channel": channel,
        "updated_at": _now(),
        "n_videos": len(videos),
        "durations_fetched": len(durations),
        "cluster": cluster,
        "channel_meta": channel_meta,
        "style_anchors": anchor_labels,
        "competitors_config": str(_competitors_config_path(channel)),
        "profile_version": load_style_profiles().get("version"),
    }


def refresh_both_channels(*, fetch_durations: bool = True) -> dict[str, Any]:
    intel = {
        "updated_at": _now(),
        "channels": {
            "napstorian": extract_competitor_style_intel(
                "napstorian", fetch_durations=fetch_durations
            ),
            "napping_historian": extract_competitor_style_intel(
                "napping_historian", fetch_durations=fetch_durations
            ),
        },
    }
    INTEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    INTEL_PATH.write_text(json.dumps(intel, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return intel


def load_intel() -> dict[str, Any]:
    return _read_json(INTEL_PATH)


def validate_style_against_scorecard(
    channel: str,
    *,
    metrics: dict[str, Any],
    lever: str,
    goals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """After N publics, stamp smm_works or smm_fails for an editing lever."""
    from src.services import smm_pipeline_ab as ab

    g = ((goals or {}).get("channels") or {}).get(channel) or {}
    ctr_min = float(g.get("ctr_pct_min") or ab.DEFAULT_CTR_MIN)
    avd_min = float(g.get("avd_pct_min") or ab.DEFAULT_AVD_MIN)
    ctr = metrics.get("ctr_pct")
    avd = metrics.get("avd_pct")
    try:
        ctr_f = float(ctr) if ctr is not None else None
    except (TypeError, ValueError):
        ctr_f = None
    try:
        avd_f = float(avd) if avd is not None else None
    except (TypeError, ValueError):
        avd_f = None

    improved = False
    if ctr_f is not None and ctr_f >= ctr_min:
        improved = True
    elif avd_f is not None and avd_f >= avd_min and ctr_f is None:
        improved = True

    row = {
        "channel": channel,
        "lever": lever,
        "outcome": "works" if improved else "fails",
        "competitor_source": "competitor_style_intel",
        "after_metrics": metrics,
        "ts": _now(),
        "note": "premium editor style validation",
    }
    if improved:
        ab._append_jsonl(ab.WORKS, row)
    else:
        ab._append_jsonl(ab.FAILS, row)
    ab.refresh_playbook()
    return row
