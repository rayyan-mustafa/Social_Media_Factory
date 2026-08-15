"""Load retention / longform / epic pacing profiles."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
ROOT = CONFIG_DIR.parent

DEFAULT_PROFILE = "retention"  # Pakistan pack: ~15 min / 125 stills until revenue
KNOWN_PROFILES = frozenset(
    {
        "retention",
        "longform",
        "epic",
        "foc_epic",
        "primary_source_reading",
        "short_test",
        "short_test_1min",
        "short_test_30s",
    }
)


@lru_cache(maxsize=1)
def load_retention_profiles() -> dict:
    path = CONFIG_DIR / "retention_profiles.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def active_profile_name() -> str:
    return (os.getenv("RETENTION_PROFILE") or DEFAULT_PROFILE).strip().lower()


def normalize_format(value: str | None, *, default: str | None = None) -> str:
    """Map sheet ``format`` / profile name → known profile; blank → default."""
    raw = (value or "").strip().lower()
    if not raw:
        return (default or active_profile_name() or DEFAULT_PROFILE).strip().lower()
    aliases = {
        "14": "retention",
        "14min": "retention",
        "14-min": "retention",
        "15": "retention",
        "15min": "retention",
        "15-min": "retention",
        "20": "longform",
        "20min": "longform",
        "20-min": "longform",
        "90": "epic",
        "90min": "epic",
        "90-min": "epic",
        "foc": "foc_epic",
        "foc-epic": "foc_epic",
        "3h": "foc_epic",
        "3-4h": "foc_epic",
        "4h": "foc_epic",
        "primary": "primary_source_reading",
        "primary_source": "primary_source_reading",
        "voices": "primary_source_reading",
        "short": "short_test",
        "short-test": "short_test",
        "3-5min": "short_test",
        "4min": "short_test",
        "4-min": "short_test",
        "1min": "short_test_1min",
        "1-min": "short_test_1min",
        "1m": "short_test_1min",
        "short_1min": "short_test_1min",
        "short-test-1min": "short_test_1min",
        "30s": "short_test_30s",
        "30sec": "short_test_30s",
        "30-sec": "short_test_30s",
        "0.5min": "short_test_30s",
        "short_30s": "short_test_30s",
        "short-test-30s": "short_test_30s",
    }
    name = aliases.get(raw, raw)
    if name not in KNOWN_PROFILES:
        return (default or active_profile_name() or DEFAULT_PROFILE).strip().lower()
    return name


def get_profile(name: str | None = None) -> dict:
    profiles = load_retention_profiles()
    key = normalize_format(name)
    return dict(profiles.get(key) or profiles.get(DEFAULT_PROFILE) or {})


def resolve_prompts_dir(
    file_cfg: dict | None = None,
    *,
    channel: str | None = None,
) -> Path:
    """Absolute prompts directory for channel overlay, then active profile.

    Precedence:
    1. ``channel`` / ``SCRIPT_CHANNEL`` → sheet_channels.prompts_dir (if set)
    2. ``file_cfg["prompts_dir"]`` (retention profile, e.g. epic → config/prompts/epic)
    3. ``config/prompts``
    """
    cfg = file_cfg if file_cfg is not None else merge_profile_into_script_settings({})
    ch = (channel or os.getenv("SCRIPT_CHANNEL") or "").strip()
    if ch:
        try:
            from src.agents.sheet_channels import channel_prompts_dir

            override = channel_prompts_dir(ch)
        except Exception:  # noqa: BLE001
            override = None
        if override:
            rel = override.strip()
            p = Path(rel)
            if not p.is_absolute():
                p = ROOT / p
            return p
    rel = (cfg.get("prompts_dir") or "config/prompts").strip()
    p = Path(rel)
    if not p.is_absolute():
        p = ROOT / p
    return p


def merge_profile_into_script_settings(base: dict) -> dict:
    """Overlay active retention profile onto script_settings.json values."""
    profiles = load_retention_profiles()
    name = active_profile_name()
    prof = profiles.get(name)
    if not prof:
        return dict(base)
    merged = dict(base)
    merged.update(prof)
    merged["_retention_profile"] = name
    return merged


def profile_gate_r_band() -> tuple[float, float]:
    prof = get_profile()
    lo = float(prof.get("gate_r_min_duration_s", 600))
    hi = float(prof.get("gate_r_max_duration_s", 1080))
    return lo, hi


def profile_gate_a_band() -> tuple[float, float]:
    """Gate A duration band for the active profile (falls back to longform defaults)."""
    prof = get_profile()
    lo = float(prof.get("gate_a_min_duration_s", prof.get("gate_r_min_duration_s", 900)))
    hi = float(prof.get("gate_a_max_duration_s", prof.get("gate_r_max_duration_s", 1500)))
    return lo, hi
