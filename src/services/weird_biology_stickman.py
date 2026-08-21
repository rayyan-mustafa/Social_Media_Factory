"""SVG joint-rig stickman for Weird Human Biology (procedural, no AI stills).

Skeleton → jittered SVG paths → cairosvg raster → RGB frames for ffmpeg.
Supports multi-character cast (baby/adult), richer props, and speech bubbles.
"""

from __future__ import annotations

import math
import random
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cairosvg
from PIL import Image, ImageDraw, ImageFont

from src.services.weird_biology_prop_registry import (
    DynamicPropsTuple,
    get_all_props,
    render_custom_prop,
)
from src.services.weird_biology_style import (
    BG_MODES,
    STYLE_BLUE_BENCH,
    STYLE_DEFAULT,
    STYLE_OUTDOOR_SPLIT,
    STYLE_REFERENCE_VIBRANT,
    STYLE_WARM_CREAM,
    STYLE_WARM_TAN_SOFA,
    STYLES,
    kinetic_font_path,
    load_visual_config,
    palette_rgb,
    render_scale,
    resolve_style,
)

POSES = (
    "stand",
    "point",
    "shrug",
    "look_at_arms",
    "face_zoom",
    "enter_door",
    "walk",  # adult mid-stride (motion lines)
    "think",
    "react",
    "arms_up",  # baby surprise / crib reach
)

# Poses that get speed/motion lines near legs/feet
_WALK_POSES = frozenset({"enter_door", "walk"})

# Dynamic scalable props wrapper (supports infinite custom synthesized props)
PROPS = DynamicPropsTuple()

ROLES = ("adult", "baby")

LAYOUTS = (
    "default",
    "crib_door_scene",
)

# Bone connections (parent → child) for limb paths
_LIMB_CHAINS: tuple[tuple[str, ...], ...] = (
    ("neck", "shoulder_l", "elbow_l", "wrist_l"),
    ("neck", "shoulder_r", "elbow_r", "wrist_r"),
    ("hip", "knee_l", "ankle_l"),
    ("hip", "knee_r", "ankle_r"),
)

# Role scale / joint presets (relative to adult baseline)
# Stroke widths tuned to reference: thick, confident, uniform black lines.
_ROLE_PRESETS: dict[str, dict[str, float]] = {
    "adult": {
        "scale": 1.0,
        "head_r": 54.0,
        "stroke_w": 7.0,
        "shoulder_w": 26.0,
        "arm_out": 68.0,
        "leg_spread": 26.0,
        "torso": 1.0,
    },
    "baby": {
        "scale": 0.42,
        "head_r": 40.0,  # smaller baby vs adult (reference scale)
        "stroke_w": 5.5,
        "shoulder_w": 14.0,
        "arm_out": 34.0,
        "leg_spread": 16.0,
        "torso": 0.55,
    },
}

# Crib grey fallbacks (solid fill — not teal wireframe). Overridden by palette_vibrant.
_CRIB_GREY = "#989898"
_CRIB_GREY_SIDE = "#6A6A6A"
_CRIB_GREY_POST = "#5A5A5A"
_MATTRESS_LIP = "#D8D8D8"
_MATTRESS_SIDE = "#C8C8C8"


@dataclass
class CharacterSpec:
    """One stick figure in a shot."""

    role: str = "adult"  # adult|baby
    pose: str = "stand"
    emotion: str = "neutral"  # neutral|surprise|calm|worry|smile
    look_at: str | None = None  # adult|baby|left|right|or role name
    x_frac: float | None = None  # optional horizontal anchor (0–1)
    y_frac: float | None = None  # optional head y (0–1)
    hair: bool | None = None  # None → auto (baby=True)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CharacterSpec:
        role = str(d.get("role") or "adult").strip().lower()
        if role not in ROLES:
            role = "adult"
        hair = d.get("hair")
        return cls(
            role=role,
            pose=str(d.get("pose") or "stand").strip().lower(),
            emotion=str(d.get("emotion") or "neutral").strip().lower(),
            look_at=(str(d["look_at"]).strip().lower() if d.get("look_at") else None),
            x_frac=float(d["x_frac"]) if d.get("x_frac") is not None else None,
            y_frac=float(d["y_frac"]) if d.get("y_frac") is not None else None,
            hair=bool(hair) if hair is not None else None,
        )


@dataclass
class ShotSpec:
    pose: str = "stand"
    emotion: str = "neutral"  # neutral|surprise|calm|worry|smile
    props: list[str] | None = None
    kinetic_text: str | None = None
    xray: bool = False
    internal: str = "nerve"  # nerve|vessel|muscle
    camera: str = "wide"  # wide|face_zoom
    seed: int = 0
    cast: list[CharacterSpec] | None = None
    bubble_bars: int = 0  # coral bars inside speech bubble (0 = off)
    bubble_text: str | None = None
    layout: str = "default"  # default|crib_door_scene
    bubble_anchor: str = "baby"  # which cast role the bubble points at
    # Empty → resolve via scene_styles (crib_door_scene → reference_vibrant)
    style: str = ""
    # Background rendering mode: default|outdoor_split|sofa|bench
    bg_mode: str = "default"

    @classmethod
    def from_dict(cls, d: dict[str, Any], *, seed: int = 0) -> ShotSpec:
        props = d.get("props") or []
        if isinstance(props, str):
            props = [props]
        props = [str(p).strip().lower() for p in props if str(p).strip()][:4]

        cast_raw = d.get("cast") or d.get("characters") or None
        cast: list[CharacterSpec] | None = None
        if isinstance(cast_raw, list) and cast_raw:
            cast = [
                CharacterSpec.from_dict(c) if isinstance(c, dict) else CharacterSpec()
                for c in cast_raw
            ]

        layout = str(d.get("layout") or "default").strip().lower()
        if layout not in LAYOUTS:
            layout = "default"

        style_raw = d.get("style")
        if style_raw is None or str(style_raw).strip() == "":
            style = ""  # resolve from layout at render time
        else:
            style = str(style_raw).strip().lower()
            if style not in STYLES:
                style = ""

        bubble_bars = int(d.get("bubble_bars") or 0)
        if bubble_bars < 0:
            bubble_bars = 0
        if bubble_bars > 5:
            bubble_bars = 5

        bg_mode_raw = str(d.get("bg_mode") or "default").strip().lower()
        bg_mode = bg_mode_raw if bg_mode_raw in BG_MODES else "default"

        return cls(
            pose=str(d.get("pose") or "stand").strip().lower(),
            emotion=str(d.get("emotion") or "neutral").strip().lower(),
            props=props,
            kinetic_text=(str(d["kinetic_text"]).strip() if d.get("kinetic_text") else None)
            or None,
            xray=bool(d.get("xray")),
            internal=str(d.get("internal") or "nerve").strip().lower(),
            camera=str(d.get("camera") or "wide").strip().lower(),
            seed=int(d.get("seed") or seed),
            cast=cast,
            bubble_bars=bubble_bars,
            bubble_text=(str(d["bubble_text"]).strip() if d.get("bubble_text") else None)
            or None,
            layout=layout,
            bubble_anchor=str(d.get("bubble_anchor") or "baby").strip().lower(),
            style=style,
            bg_mode=bg_mode,
        )

    def resolved_style(self) -> str:
        return resolve_style(self.style or None, layout=self.layout)

    def resolved_cast(self) -> list[CharacterSpec]:
        """Return cast list; apply layout presets when needed."""
        if self.layout == "crib_door_scene":
            if self.cast:
                return list(self.cast)
            return [
                CharacterSpec(
                    role="baby",
                    pose="arms_up",
                    emotion="surprise",
                    look_at="adult",
                    x_frac=0.22,
                    y_frac=0.48,  # sit inside crib rails
                    hair=True,
                ),
                CharacterSpec(
                    role="adult",
                    pose="enter_door",
                    emotion="smile",
                    look_at="baby",
                    x_frac=0.835,  # stand inside open doorway
                    y_frac=0.22,
                ),
            ]
        if self.cast:
            return list(self.cast)
        return [
            CharacterSpec(
                role="adult",
                pose=self.pose,
                emotion=self.emotion,
            )
        ]

    def resolved_props(self) -> list[str]:
        props = list(self.props or [])
        if self.layout == "crib_door_scene":
            for p in ("crib", "door", "room_corner"):
                if p not in props:
                    props.append(p)
            if self.bubble_bars > 0 and "bubble" not in props:
                props.append("bubble")
        if self.pose in {"enter_door", "walk"} and "door" not in props and not self.cast:
            props.append("door")
        if self.bubble_bars > 0 and "bubble" not in props:
            props.append("bubble")
        return props


@dataclass
class Joint:
    name: str
    x: float
    y: float


@dataclass
class Skeleton:
    """Joint-based stickman rig in canvas coordinates."""

    joints: dict[str, Joint] = field(default_factory=dict)
    head_r: float = 52.0
    stroke_w: float = 5.5
    role: str = "adult"

    def get(self, name: str) -> Joint:
        return self.joints[name]

    def xy(self, name: str) -> tuple[float, float]:
        j = self.joints[name]
        return j.x, j.y


def _hex(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def _shade_rgb(
    rgb: tuple[int, int, int],
    factor: float = 0.88,
) -> tuple[int, int, int]:
    """Cel-shade: multiply RGB toward black (factor < 1) without photo gradients."""
    f = max(0.0, min(1.5, float(factor)))
    return (
        max(0, min(255, int(round(rgb[0] * f)))),
        max(0, min(255, int(round(rgb[1] * f)))),
        max(0, min(255, int(round(rgb[2] * f)))),
    )


def _pal_hex(
    pal: dict[str, tuple[int, int, int]],
    key: str,
    *,
    fallback_rgb: tuple[int, int, int] | None = None,
    shade_of: str | None = None,
    shade_factor: float = 0.88,
) -> str:
    if key in pal:
        return _hex(pal[key])
    if shade_of and shade_of in pal:
        return _hex(_shade_rgb(pal[shade_of], shade_factor))
    if fallback_rgb is not None:
        return _hex(fallback_rgb)
    return "#888888"


def _pose_joints(
    pose: str,
    *,
    camera: str,
    W: int,
    H: int,
    role: str = "adult",
    x_frac: float | None = None,
    y_frac: float | None = None,
) -> tuple[dict[str, tuple[float, float]], float, float]:
    """Return joint xy map + head radius + stroke width for a named pose/role."""
    preset = _ROLE_PRESETS.get(role, _ROLE_PRESETS["adult"])
    floor_y = H * 0.82
    cx = W * (x_frac if x_frac is not None else (0.40 if role == "adult" else 0.20))
    head_r = float(preset["head_r"])
    stroke_w = float(preset["stroke_w"])
    sw = float(preset["shoulder_w"])
    arm_out = float(preset["arm_out"])
    leg = float(preset["leg_spread"])
    torso = float(preset["torso"])

    if camera == "face_zoom" or pose == "face_zoom":
        head_r = 110.0 if role == "adult" else 78.0
        hx, hy = W * 0.50, H * 0.40
        return {
            "head": (hx, hy),
            "neck": (hx, hy + head_r * 0.95),
            "shoulder_l": (hx - 55, hy + head_r * 1.1),
            "shoulder_r": (hx + 55, hy + head_r * 1.1),
            "elbow_l": (hx - 95, hy + head_r * 1.5),
            "elbow_r": (hx + 95, hy + head_r * 1.5),
            "wrist_l": (hx - 120, hy + head_r * 1.85),
            "wrist_r": (hx + 120, hy + head_r * 1.85),
            "hip": (hx, H * 0.78),
            "knee_l": (hx - 30, H * 0.90),
            "knee_r": (hx + 30, H * 0.90),
            "ankle_l": (hx - 40, H * 0.98),
            "ankle_r": (hx + 40, H * 0.98),
        }, head_r, stroke_w

    hy = H * (y_frac if y_frac is not None else (0.26 if role == "adult" else 0.42))
    neck = (cx, hy + head_r * 0.95)
    hip_y = hy + head_r * 0.95 + (floor_y - hy - head_r) * 0.55 * torso
    if role == "baby":
        # Keep baby feet near mattress / above floor when in crib layouts
        hip_y = min(hip_y, floor_y - 95)
    hip = (cx, hip_y)
    ankle_y = min(floor_y, hip_y + (floor_y - hip_y) * 0.95)

    def base() -> dict[str, tuple[float, float]]:
        return {
            "head": (cx, hy),
            "neck": neck,
            "shoulder_l": (cx - sw, neck[1] + 8),
            "shoulder_r": (cx + sw, neck[1] + 8),
            "elbow_l": (cx - arm_out, hip[1] - 50 * torso),
            "elbow_r": (cx + arm_out, hip[1] - 50 * torso),
            "wrist_l": (cx - arm_out - 15, hip[1] - 10 * torso),
            "wrist_r": (cx + arm_out + 15, hip[1] - 10 * torso),
            "hip": hip,
            "knee_l": (cx - leg, (hip[1] + ankle_y) / 2),
            "knee_r": (cx + leg, (hip[1] + ankle_y) / 2),
            "ankle_l": (cx - leg - 8, ankle_y),
            "ankle_r": (cx + leg + 10, ankle_y),
        }

    j = base()
    if pose == "point":
        j["elbow_r"] = (cx + arm_out + 25, hy + 40)
        j["wrist_r"] = (cx + arm_out + 85, hy + 10)
        j["shoulder_r"] = (cx + sw + 7, neck[1] + 5)
    elif pose == "shrug":
        j["elbow_l"] = (cx - arm_out - 25, hy + 35)
        j["elbow_r"] = (cx + arm_out + 25, hy + 35)
        j["wrist_l"] = (cx - arm_out - 40, hy + 15)
        j["wrist_r"] = (cx + arm_out + 40, hy + 15)
        j["shoulder_l"] = (cx - sw - 12, neck[1] - 2)
        j["shoulder_r"] = (cx + sw + 12, neck[1] - 2)
    elif pose == "look_at_arms":
        j["head"] = (cx + 12, hy + 8)
        j["elbow_l"] = (cx - 15, hy + 95 * torso)
        j["elbow_r"] = (cx + 55, hy + 100 * torso)
        j["wrist_l"] = (cx + 5, hy + 130 * torso)
        j["wrist_r"] = (cx + 70, hy + 135 * torso)
    elif pose in {"enter_door", "walk"}:
        # Mid-stride into room: leading left foot, trailing right; arm reaches toward crib
        j["elbow_l"] = (cx - arm_out * 1.1, hip[1] - 70 * torso)
        j["elbow_r"] = (cx + arm_out * 0.55, hip[1] - 35 * torso)
        j["wrist_l"] = (cx - arm_out * 1.7, hip[1] - 35 * torso)
        j["wrist_r"] = (cx + arm_out * 0.7, hip[1] + 5 * torso)
        # Walk cycle: left leg forward/planted, right trailing with lifted feel
        j["knee_l"] = (cx - leg - 14, (hip[1] + ankle_y) / 2 + 6)
        j["knee_r"] = (cx + leg + 18, (hip[1] + ankle_y) / 2 - 10)
        j["ankle_l"] = (cx - leg - 28, ankle_y)
        j["ankle_r"] = (cx + leg + 34, ankle_y - 8)
    elif pose == "think":
        j["elbow_r"] = (cx + 50 * torso, hy + 25)
        j["wrist_r"] = (cx + 40 * torso, hy + 5)
    elif pose == "react":
        j["elbow_l"] = (cx - arm_out - 40, hy + 20)
        j["elbow_r"] = (cx + arm_out + 40, hy + 20)
        j["wrist_l"] = (cx - arm_out - 60, hy - 5)
        j["wrist_r"] = (cx + arm_out + 60, hy - 5)
        j["head"] = (cx, hy - 8)
    elif pose == "arms_up":
        # Baby sitting in crib — arms diagonal UP, legs forward/seated
        j["shoulder_l"] = (cx - sw, neck[1] + 4)
        j["shoulder_r"] = (cx + sw, neck[1] + 4)
        j["elbow_l"] = (cx - arm_out - 4, hy - 18)
        j["elbow_r"] = (cx + arm_out + 4, hy - 18)
        j["wrist_l"] = (cx - arm_out - 22, hy - 52)
        j["wrist_r"] = (cx + arm_out + 22, hy - 52)
        # Seated: shorter legs, feet near mattress
        j["knee_l"] = (cx - leg - 6, hip[1] + 22)
        j["knee_r"] = (cx + leg + 6, hip[1] + 22)
        j["ankle_l"] = (cx - leg - 18, hip[1] + 42)
        j["ankle_r"] = (cx + leg + 18, hip[1] + 42)
    return j, head_r, stroke_w


def build_skeleton(
    pose: str,
    *,
    camera: str,
    W: int,
    H: int,
    role: str = "adult",
    x_frac: float | None = None,
    y_frac: float | None = None,
) -> Skeleton:
    coords, head_r, stroke_w = _pose_joints(
        pose, camera=camera, W=W, H=H, role=role, x_frac=x_frac, y_frac=y_frac
    )
    return Skeleton(
        joints={n: Joint(n, x, y) for n, (x, y) in coords.items()},
        head_r=head_r,
        stroke_w=stroke_w,
        role=role,
    )


def _jittered_poly_path(
    points: list[tuple[float, float]],
    rng: random.Random,
    *,
    amp: float = 1.0,
    mid_amp: float = 1.6,
) -> str:
    """Hand-drawn polyline as cubic SVG path with light mid-segment jitter.

    Keep amp/mid_amp modest — reference strokes are confident, not jagged.
    """
    if len(points) < 2:
        return ""
    pts = [
        (p[0] + rng.uniform(-amp * 0.25, amp * 0.25), p[1] + rng.uniform(-amp * 0.25, amp * 0.25))
        for p in points
    ]
    d = [f"M {pts[0][0]:.2f} {pts[0][1]:.2f}"]
    for i in range(len(pts) - 1):
        ax, ay = pts[i]
        bx, by = pts[i + 1]
        mx = (ax + bx) / 2 + rng.uniform(-mid_amp, mid_amp)
        my = (ay + by) / 2 + rng.uniform(-mid_amp, mid_amp)
        d.append(f"Q {mx:.2f} {my:.2f} {bx:.2f} {by:.2f}")
    return " ".join(d)


def _imperfect_circle_path(
    cx: float,
    cy: float,
    r: float,
    rng: random.Random,
    *,
    n: int = 48,
    amp: float = 1.4,
) -> str:
    """Closed high-N imperfect circle — reads round, not chunky n-gon."""
    n = max(32, int(n))
    # Cap absolute wobble so large heads stay round
    amp = min(amp, r * 0.035)
    pts: list[tuple[float, float]] = []
    for i in range(n):
        ang = (2 * math.pi * i) / n
        # Smooth low-frequency radius wobble + tiny noise
        wobble = math.sin(ang * 2 + rng.uniform(0, 0.4)) * amp * 0.55
        rr = r + wobble + rng.uniform(-amp * 0.35, amp * 0.35)
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    # Cubic-ish smooth join via midpoints with very small jitter
    d = [f"M {pts[0][0]:.2f} {pts[0][1]:.2f}"]
    for i in range(len(pts)):
        a = pts[i]
        b = pts[(i + 1) % len(pts)]
        mx = (a[0] + b[0]) / 2 + rng.uniform(-0.35, 0.35)
        my = (a[1] + b[1]) / 2 + rng.uniform(-0.35, 0.35)
        d.append(f"Q {mx:.2f} {my:.2f} {b[0]:.2f} {b[1]:.2f}")
    d.append("Z")
    return " ".join(d)


def _jittered_line(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    rng: random.Random,
    *,
    stroke: str,
    width: float = 4.5,
) -> str:
    path = _jittered_poly_path([(x1, y1), (x2, y2)], rng, amp=0.6, mid_amp=1.0)
    return (
        f'<path d="{path}" fill="none" stroke="{stroke}" stroke-width="{width}" '
        f'stroke-linecap="round"/>'
    )


def _props_svg(
    props: list[str],
    pal: dict[str, tuple[int, int, int]],
    W: int,
    H: int,
    rng: random.Random,
    *,
    bubble_bars: int = 0,
    bubble_anchor_xy: tuple[float, float] | None = None,
    layout: str = "default",
    style: str = STYLE_DEFAULT,
) -> list[str]:
    floor_y = H * 0.82
    stroke = _hex(pal["prop_stroke"])
    fill = _hex(pal["prop_fill"])
    coral = _hex(pal["coral_accent"])
    white = _hex(pal["head_fill"])
    stick = _hex(pal["stick_stroke"])
    crib_fill = _hex(pal["crib_fill"]) if "crib_fill" in pal else _CRIB_GREY
    crib_side = (
        _hex(pal["crib_side"])
        if "crib_side" in pal
        else (_hex(_shade_rgb(pal["crib_fill"], 0.78)) if "crib_fill" in pal else _CRIB_GREY_SIDE)
    )
    crib_post = (
        _hex(pal["crib_post"])
        if "crib_post" in pal
        else (
            _hex(_shade_rgb(pal["crib_side"], 0.88))
            if "crib_side" in pal
            else _CRIB_GREY_POST
        )
    )
    door_fill = _hex(pal["door_fill"]) if "door_fill" in pal else fill
    # Cel: open door panel slightly darker than wall (flat value shift, not gradient)
    door_panel = _pal_hex(pal, "door_panel", shade_of="bg_teal", shade_factor=0.94)
    if style == STYLE_REFERENCE_VIBRANT:
        door_fill = door_panel
    parts: list[str] = []

    for p in props:
        if p in {"none", "floor_only", "", "bubble"}:
            # bubble drawn after loop using anchor
            continue
        if p == "room_corner":
            # Vertical wall corner (left) + soft cel shadow band
            cx = W * 0.04
            shadow = _pal_hex(
                pal, "wall_corner_shadow", shade_of="bg_teal", shade_factor=0.90
            )
            parts.append(
                f'<rect x="{cx - 6:.1f}" y="{H * 0.06:.1f}" width="28" height="{floor_y - H * 0.06:.1f}" '
                f'fill="{shadow}" fill-opacity="0.55"/>'
            )
            parts.append(
                _jittered_line(cx, H * 0.08, cx, floor_y, rng, stroke=stick, width=5.5)
            )
        elif p == "door":
            # Open doorway with visible door thickness/panel (reference craft)
            if layout == "crib_door_scene":
                x0, x1 = W * 0.74, W * 0.925
            else:
                x0, x1 = W * 0.72, W * 0.92
            y0 = H * 0.12
            y1 = floor_y
            # Soft doorway recess shade (flat band inside frame)
            recess = _pal_hex(pal, "door_recess", shade_of="bg_teal", shade_factor=0.86)
            parts.append(
                f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{x1 - x0:.1f}" height="{y1 - y0:.1f}" '
                f'fill="{recess}" fill-opacity="0.35"/>'
            )
            # Frame — thick confident strokes
            parts.append(
                _jittered_line(x0, y1, x0, y0, rng, stroke=stick, width=6.5)
            )
            parts.append(
                _jittered_line(x0, y0, x1, y0, rng, stroke=stick, width=6.5)
            )
            parts.append(
                _jittered_line(x1, y0, x1, y1, rng, stroke=stick, width=6.5)
            )
            # Open door panel swinging right — solid wall-matching fill with cel value shift
            ox = x1 + 52 + rng.uniform(-1, 1)
            thick = 12.0
            panel = (
                f"M {x1:.1f} {y0+3:.1f} "
                f"L {ox:.1f} {y0+16:.1f} "
                f"L {ox:.1f} {y1-4:.1f} "
                f"L {x1:.1f} {y1-2:.1f} Z"
            )
            opacity = "1.0" if style == STYLE_REFERENCE_VIBRANT else "0.55"
            parts.append(
                f'<path d="{panel}" fill="{door_fill}" fill-opacity="{opacity}" stroke="{stick}" '
                f'stroke-width="5.5" stroke-linejoin="round"/>'
            )
            # Thickness strip (leading edge of open door) — darker cel edge
            edge = (
                f"M {ox:.1f} {y0+16:.1f} "
                f"L {ox+thick:.1f} {y0+20:.1f} "
                f"L {ox+thick:.1f} {y1-8:.1f} "
                f"L {ox:.1f} {y1-4:.1f} Z"
            )
            edge_fill = _pal_hex(
                pal, "door_edge", shade_of="bg_slate", shade_factor=0.95
            )
            if style != STYLE_REFERENCE_VIBRANT:
                edge_fill = _CRIB_GREY_SIDE
            parts.append(
                f'<path d="{edge}" fill="{edge_fill}" fill-opacity="0.95" '
                f'stroke="{stick}" stroke-width="4.5" stroke-linejoin="round"/>'
            )
            # Inner panel line (door board detail) — slight value band
            mid_x = (x1 + ox) / 2
            panel_shade = _pal_hex(
                pal, "door_panel_shade", shade_of="bg_teal", shade_factor=0.90
            )
            parts.append(
                f'<path d="M {x1 + 8:.1f} {y0 + 22:.1f} L {ox - 6:.1f} {y0 + 32:.1f} '
                f'L {ox - 6:.1f} {y1 - 16:.1f} L {x1 + 8:.1f} {y1 - 12:.1f} Z" '
                f'fill="{panel_shade}" fill-opacity="0.45"/>'
            )
            parts.append(
                _jittered_line(
                    mid_x, y0 + 28, mid_x + 2, y1 - 18, rng, stroke=stick, width=3.5
                )
            )
        elif p == "crib":
            # Solid grey crib with 3D depth: posts, thick rails, mattress edge
            if layout == "crib_door_scene":
                x0, y0 = W * 0.06, H * 0.42
                x1, y1 = W * 0.40, floor_y - 4
            else:
                x0, y0 = W * 0.06, H * 0.46
                x1, y1 = W * 0.34, floor_y - 8
            cw, ch = x1 - x0, y1 - y0
            depth = 34.0  # stronger left foreshortening for 3D read
            # Back/side wall of crib (darker cel shade)
            side = (
                f"M {x0:.1f} {y0:.1f} "
                f"L {x0-depth:.1f} {y0+12:.1f} "
                f"L {x0-depth:.1f} {y1+6:.1f} "
                f"L {x0:.1f} {y1:.1f} Z"
            )
            parts.append(
                f'<path d="{side}" fill="{crib_side}" stroke="{stick}" '
                f'stroke-width="5.5" stroke-linejoin="round"/>'
            )
            # Front body — solid grey fill
            parts.append(
                f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{cw:.1f}" height="{ch:.1f}" '
                f'fill="{crib_fill}" stroke="{stick}" stroke-width="6.0" '
                f'stroke-linejoin="round"/>'
            )
            # Inner shade band (soft cel on front panel, flat)
            inner_shade = _pal_hex(
                pal, "crib_inner_shade", shade_of="crib_fill", shade_factor=0.92
            )
            if "crib_fill" not in pal:
                inner_shade = "#8A8A8A"
            parts.append(
                f'<rect x="{x0 + 10:.1f}" y="{y0 + 10:.1f}" width="{cw - 20:.1f}" '
                f'height="{ch * 0.22:.1f}" fill="{inner_shade}" fill-opacity="0.55"/>'
            )
            # Corner posts (visible 3D uprights)
            post_w = 14.0
            posts = (
                (x0 - post_w * 0.35, y0 - 10, post_w, ch + 18),  # front-left
                (x1 - post_w * 0.65, y0 - 10, post_w, ch + 18),  # front-right
                (x0 - depth - post_w * 0.25, y0 + 4, post_w * 0.85, ch + 14),  # back-left
            )
            for px, py, pw, ph in posts:
                parts.append(
                    f'<rect x="{px:.1f}" y="{py:.1f}" width="{pw:.1f}" height="{ph:.1f}" '
                    f'rx="3" fill="{crib_post}" stroke="{stick}" stroke-width="4.5" '
                    f'stroke-linejoin="round"/>'
                )
            # Thick top + bottom rails (overdraw for weight)
            parts.append(
                _jittered_line(x0 - 4, y0, x1 + 4, y0, rng, stroke=stick, width=10.0)
            )
            parts.append(
                _jittered_line(x0 - 4, y1, x1 + 4, y1, rng, stroke=stick, width=10.0)
            )
            # Secondary rail thickness (inner parallel) for chunkier read
            parts.append(
                _jittered_line(
                    x0 + 2, y0 + 8, x1 - 2, y0 + 8, rng, stroke=stick, width=5.0
                )
            )
            parts.append(
                _jittered_line(
                    x0 + 2, y1 - 8, x1 - 2, y1 - 8, rng, stroke=stick, width=5.0
                )
            )
            # Side top/bottom rails (depth)
            parts.append(
                _jittered_line(
                    x0, y0, x0 - depth, y0 + 12, rng, stroke=stick, width=8.0
                )
            )
            parts.append(
                _jittered_line(
                    x0, y1, x0 - depth, y1 + 6, rng, stroke=stick, width=8.0
                )
            )
            # Vertical bars
            n_bars = 9 if layout == "crib_door_scene" else 6
            for i in range(n_bars):
                t = (i + 0.5) / n_bars
                x = x0 + 14 + t * (cw - 28)
                jx = x + rng.uniform(-0.6, 0.6)
                parts.append(
                    _jittered_line(
                        jx, y0 + 10, jx + rng.uniform(-0.4, 0.4), y1 - 10,
                        rng, stroke=stick, width=5.0,
                    )
                )
            # Mattress: top plane + visible front edge + left depth lip
            mx0, my0 = x0 + 16, y1 - 52
            mw, mh = cw - 32, 30
            lip_h = 14.0
            # Top mattress plane
            parts.append(
                f'<rect x="{mx0:.1f}" y="{my0:.1f}" width="{mw:.1f}" height="{mh:.1f}" '
                f'fill="{white}" stroke="{stick}" stroke-width="3.5" stroke-linejoin="round"/>'
            )
            # Front mattress edge (visible thickness)
            lip = (
                f"M {mx0:.1f} {my0+mh:.1f} "
                f"L {mx0:.1f} {my0+mh+lip_h:.1f} "
                f"L {mx0+mw:.1f} {my0+mh+lip_h:.1f} "
                f"L {mx0+mw:.1f} {my0+mh:.1f} Z"
            )
            parts.append(
                f'<path d="{lip}" fill="{_MATTRESS_LIP}" stroke="{stick}" stroke-width="3.0" '
                f'stroke-linejoin="round"/>'
            )
            # Left mattress depth corner (reads as 3D slab)
            m_depth = 18.0
            side_mat = (
                f"M {mx0:.1f} {my0:.1f} "
                f"L {mx0 - m_depth:.1f} {my0 + 8:.1f} "
                f"L {mx0 - m_depth:.1f} {my0 + mh + 8:.1f} "
                f"L {mx0:.1f} {my0 + mh:.1f} Z"
            )
            parts.append(
                f'<path d="{side_mat}" fill="{_MATTRESS_SIDE}" stroke="{stick}" '
                f'stroke-width="2.8" stroke-linejoin="round"/>'
            )
        elif p == "arrow":
            ax, ay = W * 0.68, H * 0.34
            path = _jittered_poly_path(
                [(ax, ay), (ax + 60, ay - 20), (ax + 120, ay - 38)], rng, amp=1.0, mid_amp=1.5
            )
            parts.append(
                f'<path d="{path}" fill="none" stroke="{coral}" stroke-width="6" '
                f'stroke-linecap="round" stroke-linejoin="round"/>'
            )
            parts.append(
                f'<path d="M {ax+120:.1f} {ay-38:.1f} L {ax+95:.1f} {ay-58:.1f} L {ax+108:.1f} {ay-18:.1f} Z" '
                f'fill="{coral}"/>'
            )
        elif p == "thermometer":
            tx, ty = W * 0.78, H * 0.32
            parts.append(
                f'<rect x="{tx:.1f}" y="{ty:.1f}" width="18" height="140" rx="9" '
                f'fill="{white}" stroke="{stroke}" stroke-width="3"/>'
            )
            parts.append(
                f'<circle cx="{tx+9:.1f}" cy="{ty+140:.1f}" r="16" fill="{coral}" '
                f'stroke="{stroke}" stroke-width="3"/>'
            )
        elif p == "brain_icon":
            bx, by = W * 0.74, H * 0.26
            parts.append(
                f'<ellipse cx="{bx+45:.1f}" cy="{by+35:.1f}" rx="48" ry="36" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="4"/>'
            )

        # ── New competitor-style props ────────────────────────────────────────────────

        elif p == "sun_ray":
            # Bright circle + 8 irregular radiating spikes (top-right corner)
            sx, sy = W * 0.80, H * 0.16
            sr = 48.0
            # Sun body
            sun_path = _imperfect_circle_path(sx, sy, sr, rng, n=42, amp=1.6)
            # yellow fill: use coral_accent lightened or a fixed warm yellow
            sun_yellow = "#F5C842"
            parts.append(
                f'<path d="{sun_path}" fill="{sun_yellow}" '
                f'stroke="{stick}" stroke-width="5.5" stroke-linejoin="round"/>'
            )
            # Radial spikes: 8 rays at irregular lengths, jittered
            n_rays = 8
            for i in range(n_rays):
                ang = (2 * math.pi * i / n_rays) + rng.uniform(-0.08, 0.08)
                r_inner = sr + 10 + rng.uniform(-3, 3)
                r_outer = sr + 38 + rng.uniform(-8, 8)
                x0r = sx + r_inner * math.cos(ang)
                y0r = sy + r_inner * math.sin(ang)
                x1r = sx + r_outer * math.cos(ang)
                y1r = sy + r_outer * math.sin(ang)
                # Spike width tapers: draw as thin triangle
                perp_ang = ang + math.pi / 2
                hw = 6.0 + rng.uniform(-1, 1)  # half-width at base
                bx0 = x0r + hw * math.cos(perp_ang)
                by0 = y0r + hw * math.sin(perp_ang)
                bx1 = x0r - hw * math.cos(perp_ang)
                by1 = y0r - hw * math.sin(perp_ang)
                spike_d = (
                    f"M {bx0:.1f} {by0:.1f} "
                    f"L {x1r:.1f} {y1r:.1f} "
                    f"L {bx1:.1f} {by1:.1f} Z"
                )
                parts.append(
                    f'<path d="{spike_d}" fill="{sun_yellow}" '
                    f'stroke="{stick}" stroke-width="4.0" stroke-linejoin="round"/>'
                )

        elif p == "rain_drops":
            # 6 hand-drawn teardrop shapes falling at slight angles (top-right area)
            drop_positions = [
                (W * 0.70, H * 0.14),
                (W * 0.76, H * 0.10),
                (W * 0.82, H * 0.16),
                (W * 0.88, H * 0.12),
                (W * 0.73, H * 0.22),
                (W * 0.85, H * 0.24),
            ]
            drop_color = "#6AADE0"  # light blue matching reference sky scenes
            for dx_, dy_ in drop_positions:
                dx_ += rng.uniform(-4, 4)
                dy_ += rng.uniform(-4, 4)
                dw, dh = 10.0, 20.0
                # Teardrop: circle top + pointed bottom
                drop_d = (
                    f"M {dx_:.1f} {dy_:.1f} "
                    f"C {dx_ + dw:.1f} {dy_:.1f} "
                    f"{dx_ + dw:.1f} {dy_ + dh * 0.6:.1f} "
                    f"{dx_:.1f} {dy_ + dh:.1f} "
                    f"C {dx_ - dw:.1f} {dy_ + dh * 0.6:.1f} "
                    f"{dx_ - dw:.1f} {dy_:.1f} "
                    f"{dx_:.1f} {dy_:.1f} Z"
                )
                parts.append(
                    f'<path d="{drop_d}" fill="{drop_color}" '
                    f'stroke="{stick}" stroke-width="3.5" stroke-linejoin="round"/>'
                )

        elif p == "smell_cloud":
            # Loose squiggly spiral scent cloud (right side, mid-height)
            # Matches reference image 4 exactly: twin yellow squiggle clouds
            cloud_yellow = "#E8D94A"
            # Draw TWO clouds side by side (character smell = subject smell)
            for cloud_i, (cx_, cy_) in enumerate([
                (W * 0.56, H * 0.28),
                (W * 0.72, H * 0.28),
            ]):
                # Draw 3 wavy ascending loops to form the cloud column
                prev_x, prev_y = cx_, cy_ + 80
                squiggle_parts = [f"M {prev_x:.1f} {prev_y:.1f}"]
                loop_heights = [60, 50, 40]
                for li, lh in enumerate(loop_heights):
                    amp_x = 28 + rng.uniform(-4, 4)
                    mid_y = prev_y - lh * 0.5 + rng.uniform(-3, 3)
                    end_y = prev_y - lh
                    # Alternating left/right loops
                    side = 1 if li % 2 == 0 else -1
                    squiggle_parts.append(
                        f"C {prev_x + side*amp_x:.1f} {mid_y:.1f} "
                        f"{prev_x - side*amp_x*0.6:.1f} {mid_y:.1f} "
                        f"{prev_x:.1f} {end_y:.1f}"
                    )
                    prev_y = end_y
                parts.append(
                    f'<path d="{" ".join(squiggle_parts)}" fill="none" '
                    f'stroke="{cloud_yellow}" stroke-width="7.0" '
                    f'stroke-linecap="round" stroke-linejoin="round"/>'
                )
            # Equals sign between them (reuse equals_sign logic inline)
            eq_cx = W * 0.64
            eq_cy = H * 0.38
            eq_w, eq_gap = 36.0, 14.0
            for bar_dy in (-eq_gap / 2, eq_gap / 2):
                bar_y = eq_cy + bar_dy
                bar_path = _jittered_poly_path(
                    [(eq_cx - eq_w, bar_y), (eq_cx + eq_w, bar_y)],
                    rng, amp=1.0, mid_amp=1.5,
                )
                parts.append(
                    f'<path d="{bar_path}" fill="none" stroke="{stick}" '
                    f'stroke-width="7.0" stroke-linecap="round"/>'
                )

        elif p == "equals_sign":
            # Bold hand-drawn = sign, centered right of character
            eq_cx = W * 0.68
            eq_cy = H * 0.42
            eq_w, eq_gap = 44.0, 18.0
            for bar_dy in (-eq_gap / 2, eq_gap / 2):
                bar_y = eq_cy + bar_dy
                bar_path = _jittered_poly_path(
                    [(eq_cx - eq_w, bar_y), (eq_cx + eq_w, bar_y)],
                    rng, amp=1.2, mid_amp=2.0,
                )
                parts.append(
                    f'<path d="{bar_path}" fill="none" stroke="{stick}" '
                    f'stroke-width="8.5" stroke-linecap="round"/>'
                )

        elif p == "cheese_wedge":
            # Triangular cheese wedge (right side), yellow body + darker rind band
            # Matches reference image 4: pale yellow wedge on a plate
            cw_cx, cw_cy = W * 0.80, H * 0.52
            cw_w, cw_h = 130.0, 90.0
            cheese_yellow = "#E8DC7A"
            rind_brown = "#B8943A"
            # Wedge body (triangle)
            wedge_d = (
                f"M {cw_cx - cw_w*0.5:.1f} {cw_cy + cw_h*0.5:.1f} "
                f"L {cw_cx + cw_w*0.5:.1f} {cw_cy + cw_h*0.5:.1f} "
                f"L {cw_cx:.1f} {cw_cy - cw_h*0.5:.1f} Z"
            )
            parts.append(
                f'<path d="{wedge_d}" fill="{cheese_yellow}" '
                f'stroke="{stick}" stroke-width="5.5" stroke-linejoin="round"/>'
            )
            # Rind band along the bottom edge
            rind_h = 18.0
            rind_d = (
                f"M {cw_cx - cw_w*0.5:.1f} {cw_cy + cw_h*0.5:.1f} "
                f"L {cw_cx + cw_w*0.5:.1f} {cw_cy + cw_h*0.5:.1f} "
                f"L {cw_cx + cw_w*0.5:.1f} {cw_cy + cw_h*0.5 - rind_h:.1f} "
                f"L {cw_cx - cw_w*0.5:.1f} {cw_cy + cw_h*0.5 - rind_h:.1f} Z"
            )
            parts.append(
                f'<path d="{rind_d}" fill="{rind_brown}" '
                f'stroke="{stick}" stroke-width="3.5" stroke-linejoin="round"/>'
            )
            # Plate: flat oval under the wedge
            plate_y = cw_cy + cw_h * 0.5 + 12
            parts.append(
                f'<ellipse cx="{cw_cx:.1f}" cy="{plate_y:.1f}" rx="{cw_w*0.58:.1f}" ry="12" '
                f'fill="{white}" stroke="{stick}" stroke-width="4.0"/>'
            )

        elif p == "bacteria_blob":
            # Wobbly blob with short spikes around perimeter (gut bacteria)
            bb_cx, bb_cy = W * 0.76, H * 0.36
            bb_r = 44.0
            blob_color = "#7BC87B"  # muted green
            # Imperfect circle base
            blob_path = _imperfect_circle_path(bb_cx, bb_cy, bb_r, rng, n=38, amp=3.5)
            parts.append(
                f'<path d="{blob_path}" fill="{blob_color}" '
                f'stroke="{stick}" stroke-width="4.5" stroke-linejoin="round"/>'
            )
            # Short spikes around perimeter (flagella-like)
            n_spikes = 7
            for i in range(n_spikes):
                ang = (2 * math.pi * i / n_spikes) + rng.uniform(-0.2, 0.2)
                r_in = bb_r + 4 + rng.uniform(-2, 2)
                r_out = bb_r + 20 + rng.uniform(-5, 5)
                sx0 = bb_cx + r_in * math.cos(ang)
                sy0 = bb_cy + r_in * math.sin(ang)
                sx1 = bb_cx + r_out * math.cos(ang) + rng.uniform(-3, 3)
                sy1 = bb_cy + r_out * math.sin(ang) + rng.uniform(-3, 3)
                parts.append(
                    _jittered_line(sx0, sy0, sx1, sy1, rng, stroke=stick, width=3.5)
                )
            # Single eye dot (gives it personality like the reference style)
            parts.append(
                f'<circle cx="{bb_cx - 12:.1f}" cy="{bb_cy - 8:.1f}" r="5" fill="{stick}"/>'
            )
            parts.append(
                f'<circle cx="{bb_cx + 10:.1f}" cy="{bb_cy - 8:.1f}" r="5" fill="{stick}"/>'
            )

        elif p == "sneeze_burst":
            # Radial particle burst (sneeze spray) from upper-left
            # Origin roughly where a face would be when sneezing
            bx_, by_ = W * 0.38, H * 0.32
            n_rays = 12
            burst_color = "#A8D4F0"  # pale blue droplets
            for i in range(n_rays):
                ang = (math.pi * 0.15) + (math.pi * 0.55 * i / (n_rays - 1)) + rng.uniform(-0.06, 0.06)
                r1 = 30 + rng.uniform(-5, 5)
                r2 = 80 + rng.uniform(-15, 15)
                x0r = bx_ + r1 * math.cos(ang)
                y0r = by_ + r1 * math.sin(ang)
                x1r = bx_ + r2 * math.cos(ang)
                y1r = by_ + r2 * math.sin(ang)
                # Alternating lines and droplet dots
                if i % 3 == 2:
                    # Droplet dot at ray tip
                    parts.append(
                        f'<circle cx="{x1r:.1f}" cy="{y1r:.1f}" r="{5 + rng.uniform(-1,1):.1f}" '
                        f'fill="{burst_color}" stroke="{stick}" stroke-width="2.5"/>'
                    )
                else:
                    ray_path = _jittered_poly_path(
                        [(x0r, y0r), (x1r, y1r)], rng, amp=0.8, mid_amp=1.5
                    )
                    w = 3.5 if i % 2 == 0 else 2.5
                    parts.append(
                        f'<path d="{ray_path}" fill="none" stroke="{burst_color}" '
                        f'stroke-width="{w}" stroke-linecap="round"/>'
                    )

        elif p == "heat_waves":
            # 3 wavy orange lines rising from lower-right (warm/temperature scenes)
            # Matches reference image 3: orange squiggles above the cow
            heat_orange = "#E87A2A"
            wave_bases = [
                (W * 0.66, H * 0.62),
                (W * 0.72, H * 0.60),
                (W * 0.78, H * 0.63),
            ]
            for wx, wy in wave_bases:
                wx += rng.uniform(-4, 4)
                # Each wave: rising S-curve
                wave_h = 110.0 + rng.uniform(-10, 10)
                amp_x = 18.0 + rng.uniform(-4, 4)
                wave_pts = [
                    (wx, wy),
                    (wx + amp_x, wy - wave_h * 0.33),
                    (wx - amp_x, wy - wave_h * 0.66),
                    (wx, wy - wave_h),
                ]
                wave_path = _jittered_poly_path(wave_pts, rng, amp=1.5, mid_amp=4.0)
                parts.append(
                    f'<path d="{wave_path}" fill="none" stroke="{heat_orange}" '
                    f'stroke-width="6.5" stroke-linecap="round" stroke-linejoin="round"/>'
                )

        elif p in get_all_props():
            # Custom synthesized props are persisted outside this source file.
            render_custom_prop(p, pal, W, H, rng, parts=parts)

    # Speech bubble (coral bars / optional text) — accent only here
    if "bubble" in props or bubble_bars > 0:
        ax, ay = bubble_anchor_xy or (W * 0.22, H * 0.38)
        bx = ax + 78
        by = ay - 100
        br = 56.0 if style == STYLE_REFERENCE_VIBRANT else 52.0
        bubble_path = _imperfect_circle_path(bx, by, br, rng, n=40, amp=0.8)
        parts.append(
            f'<path d="{bubble_path}" fill="{white}" stroke="{stick}" '
            f'stroke-width="5.0" stroke-linejoin="round"/>'
        )
        # Tail toward baby head
        tail = (
            f"M {bx - 14:.1f} {by + br * 0.72:.1f} "
            f"L {ax + 12:.1f} {ay - 10:.1f} "
            f"L {bx + 16:.1f} {by + br * 0.62:.1f} Z"
        )
        parts.append(
            f'<path d="{tail}" fill="{white}" stroke="{stick}" stroke-width="4.5" '
            f'stroke-linejoin="round"/>'
        )
        parts.append(
            f'<ellipse cx="{bx:.1f}" cy="{by + 8:.1f}" rx="{br*0.72:.1f}" ry="{br*0.55:.1f}" '
            f'fill="{white}"/>'
        )
        # Orange/coral vertical bars (rounded, evenly spaced)
        n = max(1, bubble_bars or 3)
        gap = 20
        total_w = (n - 1) * gap
        start_x = bx - total_w / 2
        heights = (34, 42, 36, 40, 30)
        for i in range(n):
            bar_x = start_x + i * gap
            bar_h = heights[i % len(heights)]
            parts.append(
                f'<rect x="{bar_x-7:.1f}" y="{by - bar_h/2:.1f}" width="14" height="{bar_h:.1f}" '
                f'rx="3.5" fill="{coral}"/>'
            )
    return parts


def _motion_lines_svg(
    skel: Skeleton,
    stroke: str,
    rng: random.Random,
    *,
    pose: str,
    look_at: str | None = None,
) -> list[str]:
    """Speed lines near legs/feet for walk / enter_door so figures don't feel static."""
    if pose not in _WALK_POSES:
        return []
    parts: list[str] = []
    al = skel.xy("ankle_l")
    ar = skel.xy("ankle_r")
    # Motion direction: toward look target when possible, else left (into room from door)
    toward_left = True
    if look_at == "right":
        toward_left = False
    elif look_at == "left":
        toward_left = True
    # Trailing foot is usually the higher / rear ankle when walking leftward
    trail = ar if toward_left else al
    lead = al if toward_left else ar
    # Lines trail behind the figure (opposite of travel)
    dx = 18.0 if toward_left else -18.0
    for i, (ax, ay) in enumerate((trail, lead)):
        n_lines = 3 if i == 0 else 2
        base_y = ay - 4
        for k in range(n_lines):
            y = base_y - k * 7 + rng.uniform(-1.2, 1.2)
            length = 22 + k * 6 + rng.uniform(-2, 2)
            x0 = ax + (4 if toward_left else -4) + rng.uniform(-1, 1)
            x1 = x0 + dx * (length / 18.0)
            # Slight fan
            y1 = y + (k - 1) * 1.5
            parts.append(
                f'<path d="{_jittered_poly_path([(x0, y), (x1, y1)], rng, amp=0.4, mid_amp=0.6)}" '
                f'fill="none" stroke="{stroke}" stroke-width="{2.4 - k * 0.35:.1f}" '
                f'stroke-linecap="round" stroke-opacity="{0.85 - k * 0.12:.2f}"/>'
            )
    # Tiny ground scuff under trailing foot
    sx, sy = trail
    parts.append(
        f'<path d="{_jittered_poly_path([(sx + dx * 0.3, sy + 3), (sx + dx * 1.1, sy + 2)], rng, amp=0.3, mid_amp=0.4)}" '
        f'fill="none" stroke="{stroke}" stroke-width="2.2" stroke-linecap="round" '
        f'stroke-opacity="0.7"/>'
    )
    return parts


def _pupil_offset(
    emotion: str,
    look_at: str | None,
    *,
    self_role: str,
    cast_positions: dict[str, tuple[float, float]],
    eye_x: float,
) -> tuple[float, float]:
    """Shift pupils for storytelling (who looks at whom)."""
    ox, oy = 0.0, 0.0
    if emotion == "worry":
        ox, oy = -2.0, 1.0
    elif emotion == "surprise":
        oy = 1.5

    target = look_at
    look_amp = 6.0

    def _toward(tx: float) -> float:
        return look_amp if tx > eye_x else -look_amp

    if target in cast_positions and target != self_role:
        ox = _toward(cast_positions[target][0])
    elif target == "left":
        ox = -look_amp
    elif target == "right":
        ox = look_amp
    elif target == "adult" and "adult" in cast_positions and self_role != "adult":
        ox = _toward(cast_positions["adult"][0])
    elif target == "baby" and "baby" in cast_positions and self_role != "baby":
        ox = _toward(cast_positions["baby"][0])
    return ox, oy


def _face_svg(
    hx: float,
    hy: float,
    r: float,
    emotion: str,
    stroke: str,
    eye_white: str,
    pupil: str,
    rng: random.Random,
    *,
    look_at: str | None = None,
    self_role: str = "adult",
    cast_positions: dict[str, tuple[float, float]] | None = None,
    style: str = STYLE_DEFAULT,
) -> list[str]:
    """Expressive face: white sclera + directed pupils; brows for emotion."""
    parts: list[str] = []
    cast_positions = cast_positions or {}
    lx, ly = hx - r * 0.34, hy - r * 0.06
    rx, ry = hx + r * 0.34, hy - r * 0.06

    # Compact sclera for vibrant stickman; larger legacy brand eyes otherwise
    if style == STYLE_REFERENCE_VIBRANT:
        eye_r = max(7.5, r * 0.20)
        pupil_frac = 0.42
        stroke_w = 2.6
    else:
        eye_r = max(10.0, r * 0.32)
        pupil_frac = 0.28
        stroke_w = 3.0

    for ex, ey in ((lx, ly), (rx, ry)):
        er = eye_r + rng.uniform(-0.25, 0.25)
        parts.append(
            f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="{er:.1f}" fill="{eye_white}" '
            f'stroke="{stroke}" stroke-width="{stroke_w}"/>'
        )
        pr = max(2.2, er * pupil_frac)
        ox, oy = _pupil_offset(
            emotion,
            look_at,
            self_role=self_role,
            cast_positions=cast_positions,
            eye_x=ex,
        )
        # Stronger look-at offset on vibrant so direction reads at thumbnail size
        amp = 1.0 if style != STYLE_REFERENCE_VIBRANT else 1.15
        parts.append(
            f'<circle cx="{ex+ox*amp:.1f}" cy="{ey+oy*amp:.1f}" r="{pr:.1f}" fill="{pupil}"/>'
        )
        # Tiny highlight for liveliness (flat, not glossy gradient)
        hx_dot = ex + ox * amp - pr * 0.35
        hy_dot = ey + oy * amp - pr * 0.35
        parts.append(
            f'<circle cx="{hx_dot:.1f}" cy="{hy_dot:.1f}" r="{max(1.0, pr * 0.28):.1f}" '
            f'fill="{eye_white}" fill-opacity="0.85"/>'
        )

    # Brows — always for surprise/worry; adults also get calm/smile brows
    brow_y = ly - eye_r * 1.35
    need_brows = emotion in {"surprise", "react", "worry", "smile", "calm"} or self_role == "adult"
    if need_brows:
        if emotion in {"surprise", "react"}:
            brow_lift = 6.0
            tilt = -3.0  # raised outer
        elif emotion == "worry":
            brow_lift = 2.0
            tilt = 3.5  # knit inward
        elif emotion == "smile":
            brow_lift = 1.5
            tilt = -0.5
        else:
            brow_lift = 2.0
            tilt = 0.0
        for i, ex in enumerate((lx, rx)):
            bw = eye_r * 0.95
            by = brow_y - brow_lift + rng.uniform(-0.4, 0.4)
            x0, y0 = ex - bw, by + (tilt if i == 0 else -tilt) * 0.3
            x1, y1 = ex + bw, by + (-tilt if i == 0 else tilt) * 0.3
            # Worry: inner corners down toward center
            if emotion == "worry":
                if i == 0:
                    y1 = by + 3.5
                else:
                    y0 = by + 3.5
            parts.append(
                f'<path d="{_jittered_poly_path([(x0, y0), (x1, y1)], rng, amp=0.35, mid_amp=0.5)}" '
                f'fill="none" stroke="{stroke}" stroke-width="3.2" stroke-linecap="round"/>'
            )

    emo = emotion
    if emo in {"surprise", "react"}:
        parts.append(
            f'<ellipse cx="{hx:.1f}" cy="{hy+r*0.40:.1f}" rx="{r*0.13:.1f}" ry="{r*0.12:.1f}" '
            f'fill="none" stroke="{stroke}" stroke-width="3.6"/>'
        )
    elif emo in {"calm", "smile"}:
        parts.append(
            f'<path d="M {hx-r*0.26:.1f} {hy+r*0.34:.1f} Q {hx:.1f} {hy+r*0.50:.1f} '
            f'{hx+r*0.26:.1f} {hy+r*0.34:.1f}" fill="none" stroke="{stroke}" stroke-width="3.6" '
            f'stroke-linecap="round"/>'
        )
    elif emo == "worry":
        parts.append(
            f'<path d="M {hx-r*0.26:.1f} {hy+r*0.42:.1f} Q {hx:.1f} {hy+r*0.28:.1f} '
            f'{hx+r*0.26:.1f} {hy+r*0.42:.1f}" fill="none" stroke="{stroke}" stroke-width="3.2" '
            f'stroke-linecap="round"/>'
        )
    else:
        y = hy + r * 0.35 + rng.uniform(-0.4, 0.4)
        parts.append(
            f'<path d="{_jittered_poly_path([(hx-r*0.22, y), (hx+r*0.22, y)], rng, amp=0.5, mid_amp=0.8)}" '
            f'fill="none" stroke="{stroke}" stroke-width="3.2" stroke-linecap="round"/>'
        )
    return parts


def _hair_tufts_svg(
    hx: float,
    hy: float,
    r: float,
    stroke: str,
    rng: random.Random,
    *,
    n: int = 3,
) -> list[str]:
    """Three thin hair tufts sprouting from top of head (baby reference)."""
    parts: list[str] = []
    base_y = hy - r + 2
    offsets = [-12, 0, 12][:n]
    for i, ox in enumerate(offsets):
        tip_x = hx + ox + rng.uniform(-2, 2)
        tip_y = base_y - 16 - (i % 2) * 4 + rng.uniform(-2, 2)
        mid_x = hx + ox * 0.4 + rng.uniform(-1.5, 1.5)
        mid_y = base_y - 8
        path = (
            f"M {hx + ox * 0.3:.1f} {base_y:.1f} "
            f"Q {mid_x:.1f} {mid_y:.1f} {tip_x:.1f} {tip_y:.1f}"
        )
        parts.append(
            f'<path d="{path}" fill="none" stroke="{stroke}" stroke-width="2.8" '
            f'stroke-linecap="round"/>'
        )
    return parts


def _xray_internal_path(
    neck: tuple[float, float],
    hip: tuple[float, float],
    internal: str,
    rng: random.Random,
    *,
    phase: float,
) -> str:
    nx, ny = neck
    hx, hy = hip
    pts: list[tuple[float, float]] = []
    n = 12
    for i in range(n):
        t = i / (n - 1)
        if internal == "vessel":
            x = nx + (hx - nx) * t + math.sin(t * 6 + phase) * 14
        elif internal == "muscle":
            x = nx + (hx - nx) * t + (1 if i % 2 == 0 else -1) * 10
        else:
            x = nx + (hx - nx) * t + math.sin(t * 10 + phase * 2) * 9
        y = ny + (hy - ny) * t
        pts.append((x, y))
    return _jittered_poly_path(pts, rng, amp=1.0, mid_amp=2.0)


def _draw_character(
    layers: list[str],
    char: CharacterSpec,
    *,
    camera: str,
    W: int,
    H: int,
    pal: dict[str, tuple[int, int, int]],
    rng: random.Random,
    phase: float,
    xray: bool,
    internal: str,
    cast_positions: dict[str, tuple[float, float]],
    style: str = STYLE_DEFAULT,
) -> tuple[float, float, float]:
    """Append one character's SVG layers. Returns (head_x, head_y, head_r)."""
    stroke = _hex(pal["stick_stroke"])
    head_fill = _hex(pal["head_fill"])
    coral = _hex(pal["coral_accent"])
    xray_fill = _hex(pal["xray_fill"])
    xray_line = _hex(pal["xray_internal"])

    # Face-zoom only for single adult default shots
    cam = camera if char.role == "adult" else "wide"
    skel = build_skeleton(
        char.pose,
        camera=cam,
        W=W,
        H=H,
        role=char.role,
        x_frac=char.x_frac,
        y_frac=char.y_frac,
    )

    neck = skel.xy("neck")
    hip = skel.xy("hip")

    if xray and char.role == "adult":
        tx = (neck[0] + hip[0]) / 2
        ty = (neck[1] + hip[1]) / 2
        layers.append(
            f'<ellipse cx="{tx:.1f}" cy="{ty:.1f}" rx="34" ry="{abs(hip[1]-neck[1])*0.55:.1f}" '
            f'fill="{xray_fill}" fill-opacity="0.45" stroke="{stroke}" stroke-width="3" '
            f'stroke-opacity="0.75"/>'
        )
        ip = _xray_internal_path(neck, hip, internal, rng, phase=phase)
        layers.append(
            f'<path d="{ip}" fill="none" stroke="{xray_line}" stroke-width="3.5" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
        )
        if internal == "nerve":
            t = phase % 1.0
            px = neck[0] + (hip[0] - neck[0]) * t
            py = neck[1] + (hip[1] - neck[1]) * t
            layers.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="5.5" fill="{coral}"/>')
    else:
        spine = _jittered_poly_path([neck, hip], rng)
        layers.append(
            f'<path d="{spine}" fill="none" stroke="{stroke}" stroke-width="{skel.stroke_w}" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
        )

    for chain in _LIMB_CHAINS:
        pts = [skel.xy(n) for n in chain if n in skel.joints]
        if len(pts) < 2:
            continue
        d = _jittered_poly_path(pts, rng)
        layers.append(
            f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{skel.stroke_w}" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
        )

    # Speed lines behind walking / enter_door feet (before head so they sit under arms)
    layers.extend(
        _motion_lines_svg(
            skel,
            stroke,
            rng,
            pose=char.pose,
            look_at=char.look_at,
        )
    )

    hx, hy = skel.xy("head")
    head_path = _imperfect_circle_path(hx, hy, skel.head_r, rng, n=48, amp=1.2)
    head_sw = max(5.5, skel.stroke_w * 0.9)
    layers.append(
        f'<path d="{head_path}" fill="{head_fill}" stroke="{stroke}" stroke-width="{head_sw:.1f}" '
        f'stroke-linejoin="round"/>'
    )
    layers.extend(
        _face_svg(
            hx,
            hy,
            skel.head_r,
            char.emotion,
            stroke,
            _hex(pal["eye_white"]),
            _hex(pal["eye_pupil"]),
            rng,
            look_at=char.look_at,
            self_role=char.role,
            cast_positions=cast_positions,
            style=style,
        )
    )

    want_hair = char.hair if char.hair is not None else (char.role == "baby")
    if want_hair:
        layers.extend(_hair_tufts_svg(hx, hy, skel.head_r, stroke, rng, n=3))
    elif char.emotion in {"surprise", "react"} and char.pose in {
        "look_at_arms",
        "react",
        "stand",
    }:
        # Legacy single tuft for reactive adult
        hair = _jittered_poly_path(
            [
                (hx - 8, hy - skel.head_r + 4),
                (hx - 2, hy - skel.head_r - 14),
                (hx + 6, hy - skel.head_r + 2),
            ],
            rng,
            amp=1.0,
            mid_amp=2.0,
        )
        layers.append(
            f'<path d="{hair}" fill="none" stroke="{stroke}" stroke-width="3" stroke-linecap="round"/>'
        )

    return hx, hy, skel.head_r


def build_stickman_svg(shot: ShotSpec, *, phase: float = 0.0) -> str:
    """Build full-frame SVG markup for one stickman shot."""
    cfg = load_visual_config()
    W = int(cfg.get("width") or 1280)
    H = int(cfg.get("height") or 720)
    style = shot.resolved_style()
    pal = palette_rgb(style, layout=shot.layout)
    rng = random.Random(shot.seed + int(phase * 1000))

    bg = _hex(pal["bg_teal"])
    slate = _hex(pal["bg_slate"])
    floor = _hex(pal["floor_line"])
    floor_band = _pal_hex(pal, "floor_band", shade_of="bg_slate", shade_factor=0.88)
    wall_base = _pal_hex(pal, "wall_base_shade", shade_of="bg_teal", shade_factor=0.94)
    bg_mode = getattr(shot, "bg_mode", "default")

    cast = shot.resolved_cast()
    props = shot.resolved_props()

    # Precompute head positions for look-at storytelling
    cast_positions: dict[str, tuple[float, float]] = {}
    for ch in cast:
        cam = shot.camera if ch.role == "adult" else "wide"
        sk = build_skeleton(
            ch.pose,
            camera=cam,
            W=W,
            H=H,
            role=ch.role,
            x_frac=ch.x_frac,
            y_frac=ch.y_frac,
        )
        cast_positions[ch.role] = sk.xy("head")

    # ── Background layers ────────────────────────────────────────────────────
    if bg_mode == "outdoor_split":
        # Reference image 3: white sky top half, flat green ground bottom half
        sky_color  = _pal_hex(pal, "sky_white",   fallback_rgb=(240, 240, 240))
        grass_color = _pal_hex(pal, "grass_green", fallback_rgb=(78, 154, 64))
        horizon_y = H * 0.55   # where sky meets ground
        layers: list[str] = [
            # Sky
            f'<rect width="{W}" height="{horizon_y:.1f}" fill="{sky_color}"/>',
            # Ground
            f'<rect y="{horizon_y:.1f}" width="{W}" height="{H - horizon_y:.1f}" fill="{grass_color}"/>',
            # Hard horizon line
            f'<path d="{_jittered_poly_path([(0, horizon_y), (W, horizon_y)], rng, amp=0.3, mid_amp=0.6)}" '
            f'fill="none" stroke="{_pal_hex(pal, "floor_line", fallback_rgb=(42, 80, 24))}" '
            f'stroke-width="4.0" stroke-linecap="round"/>',
        ]
    elif bg_mode == "sofa":
        # Reference image 2: warm tan wall + teal sofa across lower third
        layers = [
            f'<rect width="{W}" height="{H}" fill="{bg}"/>',
            f'<rect y="{H*0.70:.1f}" width="{W}" height="{H*0.08:.1f}" fill="{wall_base}" fill-opacity="0.55"/>',
            f'<rect y="{H*0.78:.1f}" width="{W}" height="{H*0.22:.1f}" fill="{slate}"/>',
            f'<rect y="{H*0.90:.1f}" width="{W}" height="{H*0.10:.1f}" fill="{floor_band}" fill-opacity="0.65"/>',
            f'<path d="{_jittered_poly_path([(28, H*0.82), (W-28, H*0.82)], rng, amp=0.4, mid_amp=0.8)}" '
            f'fill="none" stroke="{floor}" stroke-width="5.5" stroke-linecap="round"/>',
        ]
        # Draw teal sofa spanning most of width — reference image 2 style
        sofa_fill  = _pal_hex(pal, "sofa_fill",  fallback_rgb=(90, 154, 138))
        sofa_side  = _pal_hex(pal, "sofa_side",  fallback_rgb=(74, 128, 112))
        sofa_back  = _pal_hex(pal, "sofa_back",  fallback_rgb=(58, 112, 96))
        sx0, sx1 = W * 0.04, W * 0.96
        sy_seat   = H * 0.62
        sy_floor  = H * 0.82
        sy_back_t = H * 0.38
        sofa_depth = 30.0
        # Sofa back
        layers.append(
            f'<rect x="{sx0:.1f}" y="{sy_back_t:.1f}" width="{sx1-sx0:.1f}" '
            f'height="{sy_seat - sy_back_t:.1f}" fill="{sofa_back}" '
            f'stroke="{_hex(pal["stick_stroke"])}" stroke-width="5.0" stroke-linejoin="round"/>'
        )
        # Sofa seat
        layers.append(
            f'<rect x="{sx0:.1f}" y="{sy_seat:.1f}" width="{sx1-sx0:.1f}" '
            f'height="{sy_floor - sy_seat:.1f}" fill="{sofa_fill}" '
            f'stroke="{_hex(pal["stick_stroke"])}" stroke-width="5.0" stroke-linejoin="round"/>'
        )
        # Sofa 3D depth strip (left arm)
        arm_w = 36.0
        layers.append(
            f'<rect x="{sx0:.1f}" y="{sy_back_t:.1f}" width="{arm_w:.1f}" '
            f'height="{sy_floor - sy_back_t:.1f}" fill="{sofa_side}" '
            f'stroke="{_hex(pal["stick_stroke"])}" stroke-width="4.0" stroke-linejoin="round"/>'
        )
        layers.append(
            f'<rect x="{sx1 - arm_w:.1f}" y="{sy_back_t:.1f}" width="{arm_w:.1f}" '
            f'height="{sy_floor - sy_back_t:.1f}" fill="{sofa_side}" '
            f'stroke="{_hex(pal["stick_stroke"])}" stroke-width="4.0" stroke-linejoin="round"/>'
        )
        # Sofa bottom depth band (front face)
        layers.append(
            f'<rect x="{sx0:.1f}" y="{sy_floor:.1f}" width="{sx1-sx0:.1f}" '
            f'height="{sofa_depth:.1f}" fill="{sofa_side}" '
            f'stroke="{_hex(pal["stick_stroke"])}" stroke-width="4.0" stroke-linejoin="round"/>'
        )
    elif bg_mode == "bench":
        # Reference image 4: blue-grey bg + rich blue bench
        layers = [
            f'<rect width="{W}" height="{H}" fill="{bg}"/>',
            f'<rect y="{H*0.70:.1f}" width="{W}" height="{H*0.08:.1f}" fill="{wall_base}" fill-opacity="0.55"/>',
            f'<rect y="{H*0.78:.1f}" width="{W}" height="{H*0.22:.1f}" fill="{slate}"/>',
            f'<rect y="{H*0.90:.1f}" width="{W}" height="{H*0.10:.1f}" fill="{floor_band}" fill-opacity="0.65"/>',
            f'<path d="{_jittered_poly_path([(28, H*0.82), (W-28, H*0.82)], rng, amp=0.4, mid_amp=0.8)}" '
            f'fill="none" stroke="{floor}" stroke-width="5.5" stroke-linecap="round"/>',
        ]
        # Blue bench spanning ~90% of width (reference image 4)
        bench_fill = _pal_hex(pal, "bench_fill", fallback_rgb=(74, 114, 168))
        bench_side = _pal_hex(pal, "bench_side", fallback_rgb=(58, 90, 144))
        bench_leg  = _pal_hex(pal, "bench_leg",  fallback_rgb=(42, 72, 120))
        bx0, bx1  = W * 0.03, W * 0.97
        by_top, by_bot = H * 0.68, H * 0.76
        depth = 22.0
        # Bench seat top face
        layers.append(
            f'<rect x="{bx0:.1f}" y="{by_top:.1f}" width="{bx1-bx0:.1f}" '
            f'height="{by_bot - by_top:.1f}" fill="{bench_fill}" '
            f'stroke="{_hex(pal["stick_stroke"])}" stroke-width="5.5" stroke-linejoin="round"/>'
        )
        # Front face depth band
        layers.append(
            f'<rect x="{bx0:.1f}" y="{by_bot:.1f}" width="{bx1-bx0:.1f}" '
            f'height="{depth:.1f}" fill="{bench_side}" '
            f'stroke="{_hex(pal["stick_stroke"])}" stroke-width="4.5" stroke-linejoin="round"/>'
        )
        # Bench legs (4)
        leg_w = 18.0
        leg_h = H * 0.82 - (by_bot + depth)
        for lx in (bx0 + 22, bx0 + 22 + (bx1-bx0)*0.31, bx1 - 22 - (bx1-bx0)*0.31, bx1 - 22):
            layers.append(
                f'<rect x="{lx:.1f}" y="{by_bot + depth:.1f}" width="{leg_w:.1f}" '
                f'height="{leg_h:.1f}" fill="{bench_leg}" '
                f'stroke="{_hex(pal["stick_stroke"])}" stroke-width="4.0" stroke-linejoin="round"/>'
            )
    else:
        # Standard default: teal/cream wall + slate floor
        layers = [
            f'<rect width="{W}" height="{H}" fill="{bg}"/>',
            f'<rect y="{H*0.70:.1f}" width="{W}" height="{H*0.08:.1f}" fill="{wall_base}" fill-opacity="0.55"/>',
            f'<rect y="{H*0.78:.1f}" width="{W}" height="{H*0.22:.1f}" fill="{slate}"/>',
            f'<rect y="{H*0.90:.1f}" width="{W}" height="{H*0.10:.1f}" fill="{floor_band}" fill-opacity="0.65"/>',
            f'<path d="{_jittered_poly_path([(28, H*0.82), (W-28, H*0.82)], rng, amp=0.4, mid_amp=0.8)}" '
            f'fill="none" stroke="{floor}" stroke-width="5.5" stroke-linecap="round"/>',
        ]

    bubble_anchor_xy = cast_positions.get(shot.bubble_anchor) or cast_positions.get("baby")
    if bubble_anchor_xy is None and cast_positions:
        bubble_anchor_xy = next(iter(cast_positions.values()))

    # Props first (crib/door behind characters); bubble drawn after characters for z-order
    props_no_bubble = [p for p in props if p != "bubble"]
    layers.extend(
        _props_svg(
            props_no_bubble,
            pal,
            W,
            H,
            rng,
            bubble_bars=0,
            layout=shot.layout,
            style=style,
        )
    )

    # Draw characters — baby first, then adult
    draw_order = sorted(cast, key=lambda c: 0 if c.role == "baby" else 1)
    head_by_role: dict[str, tuple[float, float, float]] = {}
    for ch in draw_order:
        hx, hy, hr = _draw_character(
            layers,
            ch,
            camera=shot.camera,
            W=W,
            H=H,
            pal=pal,
            rng=rng,
            phase=phase,
            xray=shot.xray and len(cast) == 1,
            internal=shot.internal,
            cast_positions=cast_positions,
            style=style,
        )
        head_by_role[ch.role] = (hx, hy, hr)

    # Speech bubble on top
    if shot.bubble_bars > 0 or "bubble" in props:
        anchor = head_by_role.get(shot.bubble_anchor) or head_by_role.get("baby")
        if anchor:
            ax, ay, _ = anchor
        elif bubble_anchor_xy:
            ax, ay = bubble_anchor_xy
        else:
            ax, ay = W * 0.22, H * 0.40
        layers.extend(
            _props_svg(
                ["bubble"],
                pal,
                W,
                H,
                rng,
                bubble_bars=shot.bubble_bars or 3,
                bubble_anchor_xy=(ax, ay),
                layout=shot.layout,
                style=style,
            )
        )

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}">\n'
        + "\n".join(layers)
        + "\n</svg>\n"
    )
    return svg


def _overlay_kinetic_text(img: Image.Image, text: str) -> Image.Image:
    if not text:
        return img
    pal = palette_rgb()
    draw = ImageDraw.Draw(img)
    font_path = kinetic_font_path()
    size = 64
    font = ImageFont.load_default()
    while size >= 28:
        try:
            font = ImageFont.truetype(str(font_path), size=size)
        except OSError:
            break
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= img.width - 80:
            break
        size -= 4
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    x = (img.width - tw) // 2
    y = int(img.height * 0.08)
    for ox, oy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
        draw.text((x + ox, y + oy), text, font=font, fill=pal["stick_stroke"])
    draw.text((x, y), text, font=font, fill=pal["coral_accent"])
    return img


def svg_to_image(svg: str, *, scale: float = 2.0) -> Image.Image:
    """Rasterize SVG; default 2x then LANCZOS downscale for smoother strokes."""
    from io import BytesIO

    cfg = load_visual_config()
    W = int(cfg.get("width") or 1280)
    H = int(cfg.get("height") or 720)
    scale = max(1.0, float(scale))
    out_w = int(round(W * scale))
    out_h = int(round(H * scale))
    png = cairosvg.svg2png(
        bytestring=svg.encode("utf-8"),
        output_width=out_w,
        output_height=out_h,
    )
    img = Image.open(BytesIO(png)).convert("RGB")
    if scale != 1.0 and (img.width != W or img.height != H):
        img = img.resize((W, H), Image.Resampling.LANCZOS)
    return img


def render_stickman_frame(shot: ShotSpec, *, phase: float = 0.0) -> Image.Image:
    svg = build_stickman_svg(shot, phase=phase)
    img = svg_to_image(svg, scale=render_scale())
    if shot.kinetic_text:
        img = _overlay_kinetic_text(img, shot.kinetic_text.upper())
    return img


def render_stickman_svg_file(shot: ShotSpec, out_path: Path, *, phase: float = 0.0) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_stickman_svg(shot, phase=phase), encoding="utf-8")
    return out_path


def render_beat_still(shot: ShotSpec, out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frame = render_stickman_frame(shot, phase=0.0)
    render_stickman_svg_file(shot, out_path.with_suffix(".svg"), phase=0.0)
    frame.save(out_path, quality=92)
    return out_path


def _copy_shot(shot: ShotSpec, *, kinetic_text: str | None) -> ShotSpec:
    return ShotSpec(
        pose=shot.pose,
        emotion=shot.emotion,
        props=list(shot.props) if shot.props else None,
        kinetic_text=kinetic_text,
        xray=shot.xray,
        internal=shot.internal,
        camera=shot.camera,
        seed=shot.seed,
        cast=list(shot.cast) if shot.cast else None,
        bubble_bars=shot.bubble_bars,
        bubble_text=shot.bubble_text,
        layout=shot.layout,
        bubble_anchor=shot.bubble_anchor,
        style=shot.style,
    )


def render_beat_clip(
    shot: ShotSpec,
    out_path: Path,
    *,
    duration_s: float,
    fps: int | None = None,
) -> Path:
    cfg = load_visual_config()
    fps = int(fps or cfg.get("fps") or 12)
    duration_s = max(0.5, float(duration_s))
    n_frames = max(1, int(round(duration_s * fps)))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_path.parent / f".frames_{out_path.stem}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    for i in range(n_frames):
        phase = (i / max(1, n_frames - 1)) if shot.xray else 0.0
        spec = shot
        if shot.kinetic_text and i < min(2, max(1, n_frames // 4)):
            spec = _copy_shot(shot, kinetic_text=None)
        frame = render_stickman_frame(spec, phase=phase)
        frame.save(tmp_dir / f"f_{i:04d}.jpg", quality=90)

    pattern = str(tmp_dir / f"f_%04d.jpg")
    cmd = [
        "ffmpeg",
        "-y",
        "-framerate",
        str(fps),
        "-i",
        pattern,
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    for p in tmp_dir.glob("*.jpg"):
        p.unlink(missing_ok=True)
    try:
        tmp_dir.rmdir()
    except OSError:
        pass
    return out_path
