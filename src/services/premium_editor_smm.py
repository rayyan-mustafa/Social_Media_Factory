"""Premium editor SMM — merge competitor intel + scorecard → editing directives per channel."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.store import OPS_DIR
from src.services.competitor_style_intel import load_intel, refresh_both_channels
from src.services.editing_overrides import (
    get_channel_profile,
    get_merged_channel_editing,
    write_channel_overrides,
)
from src.services.settings import CONFIG_DIR

PREMIUM_LOG = OPS_DIR / "premium_editor_log.jsonl"

# Scorecard red flags → first editing lever to try
RED_TO_LEVER = [
    ("ctr_pct", "thumbnail_style"),
    ("first_60s_retention_pct", "hook_pattern"),
    ("avd_pct", "overlay_cadence_s"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_log(row: dict[str, Any]) -> None:
    PREMIUM_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PREMIUM_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _channel_reds(
    channel: str,
    *,
    goals: dict[str, Any] | None,
    scorecard: dict[str, Any] | None,
) -> list[str]:
    """Return metric keys under goal for this channel (aggregate or worst video)."""
    goals = goals or {}
    ch_goals = ((goals.get("channels") or {}).get(channel)) or {}
    reds: list[str] = []
    block = (scorecard or {}).get("channels", {}).get(channel) or {}
    videos = list(block.get("videos") or [])
    if not videos:
        return reds

    def _num(v: Any) -> float | None:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    for key, goal_key in (
        ("ctr_pct", "ctr_pct_min"),
        ("avd_pct", "avd_pct_min"),
        ("first_60s_retention_pct", "first_60s_retention_pct_min"),
    ):
        threshold = _num(ch_goals.get(goal_key))
        if threshold is None:
            continue
        vals = [_num(v.get(key)) for v in videos]
        vals = [v for v in vals if v is not None]
        if not vals:
            continue
        med = sorted(vals)[len(vals) // 2]
        if med < threshold:
            reds.append(key)
    return reds


def build_editing_directive(
    channel: str,
    *,
    intel: dict[str, Any] | None = None,
    cluster_shift: bool = False,
    underperforming: bool = False,
    proposed_lever: str | None = None,
    lever_value: Any = None,
) -> dict[str, Any]:
    """Merged directive for next farm jobs on this channel."""
    profile = get_channel_profile(channel)
    merged = get_merged_channel_editing(channel)
    ch_intel = ((intel or {}).get("channels") or {}).get(channel) or {}
    cluster = (ch_intel.get("cluster") or {}) if ch_intel else {}
    hints = dict(cluster.get("hints") or {})

    directive: dict[str, Any] = {
        "channel": channel,
        "created_at": _now(),
        "source": "premium_editor_smm",
        "profile_label": profile.get("label"),
        "dominant_competitor_form": cluster.get("dominant_form"),
        "cluster_shift": cluster_shift,
        "underperforming": underperforming,
        "applied_lever": proposed_lever,
        "lever_value": lever_value,
        "hook_pattern": (merged.get("hook_pattern") or {}).get("default"),
        "thumbnail_style": merged.get("thumbnail_style") or {},
        "compose": merged.get("compose") or {},
        "audio": merged.get("audio") or {},
        "pacing": merged.get("pacing") or {},
        "motion": merged.get("motion") or {},
        "motion_mode": (merged.get("motion") or {}).get("motion_mode") or "ken_burns",
        "format_mode": (merged.get("pacing") or {}).get("format_mode") or "standard",
        "voice_mode": (merged.get("audio") or {}).get("voice_mode") or "tts_single",
        "score_mode": (merged.get("audio") or {}).get("score_mode") or "none",
        "competitor_hints": hints,
    }
    try:
        from src.services.voice_cast import channel_cast, load_voice_cast

        directive["voice_cast"] = channel_cast(channel, cast=load_voice_cast())
    except Exception:  # noqa: BLE001
        pass
    if proposed_lever and lever_value is not None:
        directive["experiment_lever"] = {"lever": proposed_lever, "value": lever_value}
    return directive


def run_premium_editor_beat(
    *,
    goals: dict[str, Any] | None = None,
    scorecard: dict[str, Any] | None = None,
    dry_run: bool = False,
    fetch_durations: bool = True,
    channels: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """CEO beat hook: refresh intel, pick one lever per underperforming channel."""
    from src.services import smm_pipeline_ab as ab

    target_channels = tuple(
        c.strip().lower()
        for c in (channels or ("napstorian", "napping_historian"))
        if str(c).strip()
    )
    if not target_channels:
        target_channels = ("napstorian", "napping_historian")

    # Full intel refresh keeps both channels current; lever apply is scoped.
    intel = refresh_both_channels(fetch_durations=fetch_durations) if not dry_run else load_intel()
    if not intel.get("channels"):
        intel = refresh_both_channels(fetch_durations=False)

    if scorecard is None:
        sc_path = OPS_DIR / "smm_yt_scorecard.json"
        if sc_path.is_file():
            try:
                scorecard = json.loads(sc_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                scorecard = {}

    failed = {
        str(f.get("lever"))
        for f in ab._read_jsonl(ab.FAILS)
        if f.get("outcome") == "fails"
    }
    results: dict[str, Any] = {
        "ok": True,
        "channels": {},
        "dry_run": dry_run,
        "target_channels": list(target_channels),
    }

    # After competitor intel / train cues: redesign thumbs by seeing competitor images.
    try:
        from src.services.smm_thumb_redesign import run_thumb_redesign_beat

        results["thumb_redesign"] = run_thumb_redesign_beat(
            dry_run=dry_run,
            channels=target_channels,
            fetch_images=not dry_run,
            use_vision=not dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        results["thumb_redesign"] = {"ok": False, "error": str(exc)[:300]}

    for channel in target_channels:
        reds = _channel_reds(channel, goals=goals, scorecard=scorecard)
        under = bool(reds)
        ch_intel = (intel.get("channels") or {}).get(channel) or {}
        cluster = ch_intel.get("cluster") or {}
        hints = cluster.get("hints") or {}
        cluster_shift = bool(hints)

        levers = ab.rank_editing_levers(
            channel,
            failed_levers=failed,
            underperforming=under,
            cluster_hints=hints,
            scorecard_reds=reds,
        )
        chosen_lever = levers[0] if levers else None
        lever_value = ab.editing_lever_default_value(channel, chosen_lever) if chosen_lever else None

        directive = build_editing_directive(
            channel,
            intel=intel,
            cluster_shift=cluster_shift,
            underperforming=under,
            proposed_lever=chosen_lever,
            lever_value=lever_value,
        )

        ch_out: dict[str, Any] = {
            "reds": reds,
            "underperforming": under,
            "cluster_shift": cluster_shift,
            "chosen_lever": chosen_lever,
            "lever_value": lever_value,
            "directive_preview": directive,
            "style_anchors": ch_intel.get("style_anchors"),
        }

        if not dry_run and chosen_lever and lever_value is not None and (under or cluster_shift):
            applied = ab.apply_editing_lever(channel, chosen_lever, lever_value)
            ch_out["apply"] = applied
            write_channel_overrides(channel, editing_directive=directive)
            _append_log(
                {
                    "ts": _now(),
                    "channel": channel,
                    "lever": chosen_lever,
                    "value": lever_value,
                    "reds": reds,
                    "dominant_form": cluster.get("dominant_form"),
                    "dry_run": False,
                }
            )
        elif not dry_run:
            write_channel_overrides(channel, editing_directive=directive)
            _append_log(
                {
                    "ts": _now(),
                    "channel": channel,
                    "lever": None,
                    "note": "directive_only_no_lever_change",
                    "reds": reds,
                    "dry_run": False,
                }
            )

        results["channels"][channel] = ch_out

    results["intel_path"] = str(OPS_DIR / "competitor_style_intel.json")
    results["log_path"] = str(PREMIUM_LOG)
    return results
