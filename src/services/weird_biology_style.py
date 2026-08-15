"""Weird Biology visual style constants + config loader."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.services.settings import ROOT

CONFIG_PATH = ROOT / "config" / "weird_biology_visual.json"

# Defaults (overridden by JSON)
BG_TEAL = "#3E6B6B"
BG_SLATE = "#4A5F6A"
CORAL = "#E8724C"
HEAD_FILL = "#FFFFFF"
STICK_STROKE = "#111111"
PLANNER_MODEL = "google/gemini-2.5-flash-lite"

# Reference crib-scene warm palette (sampled from Rayyan upload)
PALETTE_VIBRANT_DEFAULT: dict[str, str] = {
    "bg_teal": "#F2DAAE",
    "bg_slate": "#C8B08A",
    "floor_line": "#2A2218",
    "stick_stroke": STICK_STROKE,
    "head_fill": HEAD_FILL,
    "eye_white": "#FFFFFF",
    "eye_pupil": "#111111",
    "coral_accent": "#D0942C",
    "xray_fill": "#E8D4B0",
    "xray_internal": "#D0942C",
    "prop_fill": "#F2DAAE",
    "prop_stroke": "#111111",
    "crib_fill": "#989898",
    "crib_side": "#6A6A6A",
    "crib_post": "#5A5A5A",
    "door_fill": "#F2DAAE",
    "door_panel": "#E8D09A",
    "floor_band": "#B09870",
    "wall_base_shade": "#E8D09A",
    "wall_corner_shadow": "#E0C898",
}

STYLE_DEFAULT = "default"
STYLE_REFERENCE_VIBRANT = "reference_vibrant"
STYLES = (STYLE_DEFAULT, STYLE_REFERENCE_VIBRANT)


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = (h or "").strip().lstrip("#")
    if len(h) != 6:
        raise ValueError(f"bad hex color: {h!r}")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


@lru_cache(maxsize=1)
def load_visual_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {
            "width": 1280,
            "height": 720,
            "fps": 12,
            "palette": {
                "bg_teal": BG_TEAL,
                "bg_slate": BG_SLATE,
                "floor_line": "#2A3F45",
                "stick_stroke": STICK_STROKE,
                "head_fill": HEAD_FILL,
                "eye_white": "#FFFFFF",
                "eye_pupil": "#111111",
                "coral_accent": CORAL,
                "xray_fill": "#9BB8B8",
                "xray_internal": CORAL,
                "prop_fill": "#5A7A7A",
                "prop_stroke": "#1A2A2E",
            },
            "palette_vibrant": dict(PALETTE_VIBRANT_DEFAULT),
            "scene_styles": {
                "crib_door_scene": STYLE_REFERENCE_VIBRANT,
                "default": STYLE_DEFAULT,
            },
            "fonts": {
                "kinetic": str(ROOT / "assets/fonts/ArchivoBlack-Regular.ttf"),
                "fallback": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            },
            "timing": {
                "target_beat_s_min": 2.0,
                "target_beat_s_max": 4.0,
                "voice_wpm": 150.0,
            },
            "planner_model": PLANNER_MODEL,
            "tts_mode": "whole_vo",
            "visual_backend": "svg_stickman_rig",
            "stickman_quality": "v5",
        }
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return data


def render_scale() -> float:
    """SVG raster supersample factor (2.0 = v5 production default)."""
    cfg = load_visual_config()
    render = cfg.get("render") or {}
    return max(1.0, float(render.get("render_scale") or 2.0))


def resolve_style(style: str | None = None, *, layout: str | None = None) -> str:
    """Resolve visual style: explicit style wins, else scene_styles[layout], else default."""
    s = (style or "").strip().lower()
    if s in STYLES:
        return s
    cfg = load_visual_config()
    scene_styles = cfg.get("scene_styles") or {}
    layout_key = (layout or "default").strip().lower()
    mapped = str(scene_styles.get(layout_key) or "").strip().lower()
    if mapped in STYLES:
        return mapped
    if layout_key == "crib_door_scene":
        return STYLE_REFERENCE_VIBRANT
    return STYLE_DEFAULT


def palette_rgb(
    style: str | None = None,
    *,
    layout: str | None = None,
) -> dict[str, tuple[int, int, int]]:
    """Return palette RGB map. Teal brand default, or warm reference_vibrant."""
    cfg = load_visual_config()
    resolved = resolve_style(style, layout=layout)
    if resolved == STYLE_REFERENCE_VIBRANT:
        pal = cfg.get("palette_vibrant") or PALETTE_VIBRANT_DEFAULT
    else:
        pal = cfg.get("palette") or {}
    out = {k: _hex_to_rgb(str(v)) for k, v in pal.items()}
    # Ensure keys used by renderer always exist (merge vibrant extras onto default if sparse)
    if resolved == STYLE_REFERENCE_VIBRANT:
        for k, v in PALETTE_VIBRANT_DEFAULT.items():
            out.setdefault(k, _hex_to_rgb(v))
    return out


def kinetic_font_path() -> Path:
    cfg = load_visual_config()
    fonts = cfg.get("fonts") or {}
    primary = ROOT / str(fonts.get("kinetic") or "assets/fonts/ArchivoBlack-Regular.ttf")
    if primary.exists():
        return primary
    fb = Path(str(fonts.get("fallback") or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
    return fb


def planner_model() -> str:
    return str(load_visual_config().get("planner_model") or PLANNER_MODEL).strip()
