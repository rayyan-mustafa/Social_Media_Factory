"""Weird Biology visual style constants + config loader."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.services.settings import ROOT

CONFIG_PATH = ROOT / "config" / "weird_biology_visual.json"

# ── Legacy defaults (kept for backward compat) ─────────────────────────────
BG_TEAL = "#3E6B6B"
BG_SLATE = "#4A5F6A"
CORAL = "#E8724C"
HEAD_FILL = "#FFFFFF"
STICK_STROKE = "#111111"
PLANNER_MODEL = "google/gemini-2.5-flash-lite"

# ── Style names ─────────────────────────────────────────────────────────────
STYLE_DEFAULT           = "default"
STYLE_REFERENCE_VIBRANT = "reference_vibrant"  # nursery/crib warm cream
STYLE_WARM_CREAM        = "warm_cream"          # competitor default: cream bg
STYLE_WARM_TAN_SOFA     = "warm_tan_sofa"       # mosquito sofa scene
STYLE_OUTDOOR_SPLIT     = "outdoor_split"       # white sky + flat green grass
STYLE_BLUE_BENCH        = "blue_bench"          # comparison bench scene
STYLES = (
    STYLE_DEFAULT,
    STYLE_REFERENCE_VIBRANT,
    STYLE_WARM_CREAM,
    STYLE_WARM_TAN_SOFA,
    STYLE_OUTDOOR_SPLIT,
    STYLE_BLUE_BENCH,
)

# ── Palettes — colour-sampled from the 4 reference images ───────────────────

# Image 1 — warm parchment (history / indoor biology)
PALETTE_WARM_CREAM: dict[str, str] = {
    "bg_teal":            "#EDE8C8",
    "bg_slate":           "#C8B890",
    "floor_line":         "#2A2218",
    "stick_stroke":       "#111111",
    "head_fill":          "#FFFFFF",
    "eye_white":          "#FFFFFF",
    "eye_pupil":          "#111111",
    "coral_accent":       "#E8724C",
    "xray_fill":          "#E8D4B0",
    "xray_internal":      "#E8724C",
    "prop_fill":          "#EDE8C8",
    "prop_stroke":        "#111111",
    "floor_band":         "#B8A870",
    "wall_base_shade":    "#E0D8B0",
    "wall_corner_shadow": "#D8D0A8",
}

# Image 2 — warm tan + teal sofa (mosquito / indoor room scenes)
PALETTE_WARM_TAN_SOFA: dict[str, str] = {
    "bg_teal":            "#C8B09A",
    "bg_slate":           "#A89078",
    "floor_line":         "#2A2010",
    "stick_stroke":       "#111111",
    "head_fill":          "#FFFFFF",
    "eye_white":          "#FFFFFF",
    "eye_pupil":          "#111111",
    "coral_accent":       "#E8724C",
    "xray_fill":          "#D4B898",
    "xray_internal":      "#E8724C",
    "prop_fill":          "#5A9A8A",
    "prop_stroke":        "#111111",
    "sofa_fill":          "#5A9A8A",
    "sofa_side":          "#4A8070",
    "sofa_back":          "#3A7060",
    "floor_band":         "#907060",
    "wall_base_shade":    "#B8A090",
    "wall_corner_shadow": "#A89080",
}

# Image 3 — outdoor split: white sky / flat green ground (nature / animal)
PALETTE_OUTDOOR_SPLIT: dict[str, str] = {
    "bg_teal":            "#F0F0F0",
    "bg_slate":           "#4E9A40",
    "floor_line":         "#2A5018",
    "stick_stroke":       "#111111",
    "head_fill":          "#FFFFFF",
    "eye_white":          "#FFFFFF",
    "eye_pupil":          "#111111",
    "coral_accent":       "#E8724C",
    "xray_fill":          "#C8E8C0",
    "xray_internal":      "#E8724C",
    "prop_fill":          "#F0F0F0",
    "prop_stroke":        "#111111",
    "grass_green":        "#4E9A40",
    "sky_white":          "#F0F0F0",
    "floor_band":         "#3A7830",
    "wall_base_shade":    "#E8E8E8",
    "wall_corner_shadow": "#D8D8D8",
}

# Image 4 — rich blue bench (comparison / Limburger / equals-sign scenes)
PALETTE_BLUE_BENCH: dict[str, str] = {
    "bg_teal":            "#8AAFCF",
    "bg_slate":           "#5A82A8",
    "floor_line":         "#1A2A3A",
    "stick_stroke":       "#111111",
    "head_fill":          "#FFFFFF",
    "eye_white":          "#FFFFFF",
    "eye_pupil":          "#111111",
    "coral_accent":       "#E84848",
    "xray_fill":          "#90B8D8",
    "xray_internal":      "#E84848",
    "prop_fill":          "#4A72A8",
    "prop_stroke":        "#111111",
    "bench_fill":         "#4A72A8",
    "bench_side":         "#3A5A90",
    "bench_leg":          "#2A4878",
    "floor_band":         "#4A6A90",
    "wall_base_shade":    "#7A9DC0",
    "wall_corner_shadow": "#6A8DB0",
}

# Original vibrant nursery (crib-door scene)
PALETTE_VIBRANT_DEFAULT: dict[str, str] = {
    "bg_teal":            "#F2DAAE",
    "bg_slate":           "#C8B08A",
    "floor_line":         "#2A2218",
    "stick_stroke":       STICK_STROKE,
    "head_fill":          HEAD_FILL,
    "eye_white":          "#FFFFFF",
    "eye_pupil":          "#111111",
    "coral_accent":       "#D0942C",
    "xray_fill":          "#E8D4B0",
    "xray_internal":      "#D0942C",
    "prop_fill":          "#F2DAAE",
    "prop_stroke":        "#111111",
    "crib_fill":          "#989898",
    "crib_side":          "#6A6A6A",
    "crib_post":          "#5A5A5A",
    "door_fill":          "#F2DAAE",
    "door_panel":         "#E8D09A",
    "floor_band":         "#B09870",
    "wall_base_shade":    "#E8D09A",
    "wall_corner_shadow": "#E0C898",
}

# ── bg_mode: rendering layout ───────────────────────────────────────────────
BG_MODES = (
    "default",        # standard wall + floor band
    "outdoor_split",  # white sky top + flat green bottom
    "sofa",           # draw teal sofa in scene
    "bench",          # draw blue bench (comparison scenes)
)


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
                "bg_teal":            "#EDE8C8",   # ← WARM CREAM now default
                "bg_slate":           "#C8B890",
                "floor_line":         "#2A2218",
                "stick_stroke":       STICK_STROKE,
                "head_fill":          HEAD_FILL,
                "eye_white":          "#FFFFFF",
                "eye_pupil":          "#111111",
                "coral_accent":       CORAL,
                "xray_fill":          "#E8D4B0",
                "xray_internal":      CORAL,
                "prop_fill":          "#EDE8C8",
                "prop_stroke":        "#111111",
                "floor_band":         "#B8A870",
                "wall_base_shade":    "#E0D8B0",
                "wall_corner_shadow": "#D8D0A8",
            },
            "palette_vibrant":    dict(PALETTE_VIBRANT_DEFAULT),
            "palette_warm_cream": dict(PALETTE_WARM_CREAM),
            "palette_warm_tan":   dict(PALETTE_WARM_TAN_SOFA),
            "palette_outdoor":    dict(PALETTE_OUTDOOR_SPLIT),
            "palette_blue_bench": dict(PALETTE_BLUE_BENCH),
            "scene_styles": {
                "crib_door_scene": STYLE_REFERENCE_VIBRANT,
                "default":         STYLE_WARM_CREAM,
            },
            "fonts": {
                "kinetic":  str(ROOT / "assets/fonts/ArchivoBlack-Regular.ttf"),
                "fallback": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            },
            "timing": {
                "target_beat_s_min": 2.0,
                "target_beat_s_max": 4.0,
                "voice_wpm": 150.0,
            },
            "planner_model":    PLANNER_MODEL,
            "tts_mode":         "whole_vo",
            "visual_backend":   "svg_stickman_rig",
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
    """Resolve visual style — warm_cream is now the default (not cold teal)."""
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
    return STYLE_WARM_CREAM   # warm cream default (not cold teal)


def palette_rgb(
    style: str | None = None,
    *,
    layout: str | None = None,
) -> dict[str, tuple[int, int, int]]:
    """Return palette RGB map for any named style."""
    cfg = load_visual_config()
    resolved = resolve_style(style, layout=layout)

    _MAP = {
        STYLE_REFERENCE_VIBRANT: ("palette_vibrant",    PALETTE_VIBRANT_DEFAULT),
        STYLE_WARM_CREAM:        ("palette_warm_cream",  PALETTE_WARM_CREAM),
        STYLE_WARM_TAN_SOFA:     ("palette_warm_tan",   PALETTE_WARM_TAN_SOFA),
        STYLE_OUTDOOR_SPLIT:     ("palette_outdoor",    PALETTE_OUTDOOR_SPLIT),
        STYLE_BLUE_BENCH:        ("palette_blue_bench", PALETTE_BLUE_BENCH),
    }

    if resolved in _MAP:
        cfg_key, fallback = _MAP[resolved]
        raw = cfg.get(cfg_key) or fallback
        out = {k: _hex_to_rgb(str(v)) for k, v in raw.items()}
        for k, v in fallback.items():
            out.setdefault(k, _hex_to_rgb(v))
    else:
        raw = cfg.get("palette") or {}
        out = {k: _hex_to_rgb(str(v)) for k, v in raw.items()}

    # Guarantee keys referenced in renderer exist
    for k, v in PALETTE_WARM_CREAM.items():
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
