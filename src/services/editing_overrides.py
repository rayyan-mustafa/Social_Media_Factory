"""Runtime channel editing overrides — profiles + SMM patches for compose/hook/thumb/script."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.services.settings import CONFIG_DIR, get_settings

PROFILES_PATH = CONFIG_DIR / "competitor_style_profiles.json"
OVERRIDES_PATH = CONFIG_DIR / "channel_editing_overrides.json"


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


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_style_profiles() -> dict[str, Any]:
    return _read_json(PROFILES_PATH)


def load_channel_overrides_file() -> dict[str, Any]:
    return _read_json(OVERRIDES_PATH)


def get_channel_profile(channel: str) -> dict[str, Any]:
    ch = (channel or "napstorian").strip().lower()
    profiles = load_style_profiles()
    channels = profiles.get("channels") or {}
    return dict(channels.get(ch) or channels.get("napstorian") or {})


def get_channel_runtime_overrides(channel: str) -> dict[str, Any]:
    ch = (channel or "napstorian").strip().lower()
    data = load_channel_overrides_file()
    block = (data.get("channels") or {}).get(ch) or {}
    return dict(block.get("overrides") or {})


def get_merged_channel_editing(channel: str) -> dict[str, Any]:
    """Profile + runtime overrides + optional active directive snapshot."""
    ch = (channel or "napstorian").strip().lower()
    base = get_channel_profile(ch)
    runtime = get_channel_runtime_overrides(ch)
    merged = _deep_merge(base, runtime) if runtime else base
    data = load_channel_overrides_file()
    block = (data.get("channels") or {}).get(ch) or {}
    directive = block.get("editing_directive")
    if isinstance(directive, dict):
        merged["_directive"] = directive
    return merged


def hook_pattern_prefs(channel: str | None) -> list[str] | None:
    editing = get_merged_channel_editing(channel or "")
    hp = editing.get("hook_pattern") or {}
    prefs = hp.get("preferred")
    if isinstance(prefs, list) and prefs:
        return [str(p) for p in prefs]
    default = hp.get("default")
    if default:
        return [str(default)]
    return None


def thumbnail_style_preset(channel: str | None) -> dict[str, Any]:
    editing = get_merged_channel_editing(channel or "")
    return dict(editing.get("thumbnail_style") or {})


def target_length_band(channel: str | None) -> tuple[int | None, int | None]:
    editing = get_merged_channel_editing(channel or "")
    pacing = editing.get("pacing") or {}
    format_mode = str(pacing.get("format_mode") or "").strip().lower()
    if format_mode in {"foc_epic", "foc", "3-4h"}:
        lo = pacing.get("foc_epic_band_min_s", 10800)
        hi = pacing.get("foc_epic_band_max_s", 14400)
        try:
            return int(lo), int(hi)
        except (TypeError, ValueError):
            return 10800, 14400
    lo = pacing.get("target_length_band_min_s")
    hi = pacing.get("target_length_band_max_s")
    try:
        return (int(lo) if lo is not None else None, int(hi) if hi is not None else None)
    except (TypeError, ValueError):
        return None, None


def format_mode_for_channel(channel: str | None) -> str:
    editing = get_merged_channel_editing(channel or "")
    pacing = editing.get("pacing") or {}
    mode = str(pacing.get("format_mode") or "standard").strip().lower()
    return mode or "standard"


def motion_mode_for_channel(channel: str | None) -> str:
    editing = get_merged_channel_editing(channel or "")
    motion = editing.get("motion") or {}
    return str(motion.get("motion_mode") or "ken_burns").strip().lower() or "ken_burns"


def voice_mode_for_channel(channel: str | None) -> str:
    editing = get_merged_channel_editing(channel or "")
    audio = editing.get("audio") or {}
    return str(audio.get("voice_mode") or "tts_single").strip().lower() or "tts_single"


def score_mode_for_channel(channel: str | None) -> str:
    editing = get_merged_channel_editing(channel or "")
    audio = editing.get("audio") or {}
    return str(audio.get("score_mode") or "none").strip().lower() or "none"


def apply_editing_to_settings(settings: Any, channel: str | None) -> dict[str, Any]:
    """Patch Settings instance in-place for compose/SFX/infographic levers."""
    ch = (channel or "napstorian").strip().lower()
    editing = get_merged_channel_editing(ch)
    compose = editing.get("compose") or {}
    audio = editing.get("audio") or {}
    applied: dict[str, Any] = {"channel": ch}

    if "compose_sfx_enabled" in audio:
        if ch in {"napping_historian", "historian", "sleep"}:
            settings.compose_sfx_historian = bool(audio["compose_sfx_enabled"])
        else:
            settings.compose_sfx_napstorian = bool(audio["compose_sfx_enabled"])

    if compose.get("beat_interval_s") is not None:
        settings.compose_sfx_beat_interval_s = float(compose["beat_interval_s"])
        applied["compose_sfx_beat_interval_s"] = settings.compose_sfx_beat_interval_s

    if compose.get("sfx_event_cap") is not None:
        cap = int(compose["sfx_event_cap"])
        settings.compose_sfx_max_infographic_whooshes = max(0, cap)
        applied["sfx_event_cap"] = cap

    if compose.get("overlay_cadence_s") is not None:
        settings.compose_infographic_target_interval_s = float(compose["overlay_cadence_s"])
        applied["overlay_cadence_s"] = settings.compose_infographic_target_interval_s

    if compose.get("max_infographic_cards") is not None:
        mx = int(compose["max_infographic_cards"])
        settings.compose_infographic_max_cards = mx
        settings.compose_infographic_min_cards = min(
            int(getattr(settings, "compose_infographic_min_cards", 8)), mx
        )
        applied["max_infographic_cards"] = mx

    if compose.get("ken_burns_scale") is not None:
        kb = float(compose["ken_burns_scale"])
        settings.ken_burns_zoom_end = kb
        settings.ken_burns_zoom_end_phase_b = min(kb + 0.04, 1.2)
        applied["ken_burns_scale"] = kb

    if audio.get("ambient_bed_db") is not None:
        settings.compose_sfx_ambient_db = float(audio["ambient_bed_db"])
        applied["ambient_bed_db"] = settings.compose_sfx_ambient_db

    if audio.get("historian_sfx_off") and ch in {"napping_historian", "historian", "sleep"}:
        settings.compose_sfx_historian = False
        applied["historian_sfx_off"] = True

    return applied


def apply_channel_editing_to_composer(composer: Any, channel: str | None) -> dict[str, Any]:
    """Apply ken_burns + compose knobs onto an EditModule instance."""
    s = getattr(composer, "s", None) or get_settings()
    applied = apply_editing_to_settings(s, channel)
    compose = get_merged_channel_editing(channel or "").get("compose") or {}
    kb = compose.get("ken_burns_scale")
    if kb is not None:
        composer._kb_zoom_end = float(kb)
        composer._kb_zoom_end_phase_b = min(float(kb) + 0.04, 1.2)
        applied["ken_burns_instance"] = kb
    composer.s = s
    return applied


def stamp_editing_directive_meta(meta: dict[str, Any], channel: str | None) -> dict[str, Any]:
    """Merge active editing directive into job/pipeline meta for downstream stages."""
    ch = (channel or meta.get("channel") or "").strip().lower()
    if not ch:
        return meta
    data = load_channel_overrides_file()
    block = (data.get("channels") or {}).get(ch) or {}
    directive = block.get("editing_directive")
    merged = get_merged_channel_editing(ch)
    out = {**meta, "channel": ch}
    if directive:
        out["editing_directive"] = directive
    out["editing_profile"] = {
        "hook_pattern": (merged.get("hook_pattern") or {}).get("default"),
        "thumbnail_preset": (merged.get("thumbnail_style") or {}).get("preset"),
        "sfx_density": (merged.get("compose") or {}).get("sfx_density"),
        "motion_mode": (merged.get("motion") or {}).get("motion_mode"),
        "format_mode": (merged.get("pacing") or {}).get("format_mode"),
        "voice_mode": (merged.get("audio") or {}).get("voice_mode"),
        "score_mode": (merged.get("audio") or {}).get("score_mode"),
    }
    lo, hi = target_length_band(ch)
    if lo is not None and hi is not None:
        out["target_length_band_s"] = [lo, hi]
    # High-effort levers for downstream script / TTS / compose
    out["motion_mode"] = (merged.get("motion") or {}).get("motion_mode") or "ken_burns"
    out["format_mode"] = (merged.get("pacing") or {}).get("format_mode") or "standard"
    out["voice_mode"] = (merged.get("audio") or {}).get("voice_mode") or "tts_single"
    out["score_mode"] = (merged.get("audio") or {}).get("score_mode") or "none"
    try:
        from src.services.voice_cast import channel_cast, load_voice_cast

        out["voice_cast"] = channel_cast(ch, cast=load_voice_cast())
    except Exception:  # noqa: BLE001
        pass
    return out


def write_channel_overrides(
    channel: str,
    *,
    overrides_patch: dict[str, Any] | None = None,
    editing_directive: dict[str, Any] | None = None,
    experiment: dict[str, Any] | None = None,
    replace_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist runtime overrides (auto — no Rayyan consent)."""
    ch = (channel or "napstorian").strip().lower()
    data = load_channel_overrides_file()
    channels = dict(data.get("channels") or {})
    block = dict(channels.get(ch) or {})
    if replace_overrides is not None:
        block["overrides"] = copy.deepcopy(replace_overrides)
    elif overrides_patch:
        current = dict(block.get("overrides") or {})
        block["overrides"] = _deep_merge(current, overrides_patch)
    if editing_directive is not None:
        block["editing_directive"] = editing_directive
    if experiment is not None:
        block["active_experiment"] = experiment
    block["updated_at"] = _now()
    channels[ch] = block
    data["channels"] = channels
    data["updated_at"] = _now()
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDES_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return block
