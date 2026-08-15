"""Bootstrap flat-vector PNG assets for Weird Human Biology asset compositor.

Run once (or when art needs refresh):
  python -m src.services.weird_biology_assets

Assets are saved under assets/weird_biology/ as RGBA PNGs. Runtime compositor
loads these files only — no live ImageDraw for characters at render time.
"""

from __future__ import annotations

from pathlib import Path

from typing import Callable

from PIL import Image, ImageDraw

from src.services.settings import ROOT

ASSETS_ROOT = ROOT / "assets" / "weird_biology"

# Vibrant nursery palette (matches config/weird_biology_visual.json palette_vibrant)
CREAM = (0xF2, 0xDA, 0xAE, 255)
WALL_SHADE = (0xE8, 0xD0, 0x9A, 255)
FLOOR = (0xC8, 0xB0, 0x8A, 255)
FLOOR_BAND = (0xB0, 0x98, 0x70, 255)
FLOOR_LINE = (0x2A, 0x22, 0x18, 255)
BLACK = (0x11, 0x11, 0x11, 255)
WHITE = (0xFF, 0xFF, 0xFF, 255)
CRIB_GREY = (0x98, 0x98, 0x98, 255)
CRIB_SIDE = (0x6A, 0x6A, 0x6A, 255)
CRIB_POST = (0x5A, 0x5A, 0x5A, 255)
MATTRESS = (0xFF, 0xFF, 0xFF, 255)
MATTRESS_LIP = (0xD8, 0xD8, 0xD8, 255)
DOOR_RECESS = (0xD8, 0xC8, 0x98, 255)
CORAL = (0xD0, 0x94, 0x2C, 255)
CORNER_SHADOW = (0xE0, 0xC8, 0x98, 140)


def _new_rgba(w: int, h: int) -> Image.Image:
    return Image.new("RGBA", (w, h), (0, 0, 0, 0))


def _stroke_line(
    draw: ImageDraw.ImageDraw,
    p0: tuple[float, float],
    p1: tuple[float, float],
    *,
    width: float = 6.0,
    fill=BLACK,
) -> None:
    draw.line([p0, p1], fill=fill, width=int(width), joint="curve")


def _stroke_round_cap(
    draw: ImageDraw.ImageDraw,
    p0: tuple[float, float],
    p1: tuple[float, float],
    *,
    width: float = 6.0,
    fill=BLACK,
) -> None:
    draw.line([p0, p1], fill=fill, width=int(width))
    r = width / 2
    for p in (p0, p1):
        draw.ellipse(
            (p[0] - r, p[1] - r, p[0] + r, p[1] + r),
            fill=fill,
        )


def generate_nursery_room(*, w: int = 1280, h: int = 720) -> Image.Image:
    """Full-frame background: cream wall, beige floor, corner shadow."""
    img = Image.new("RGBA", (w, h), CREAM)
    draw = ImageDraw.Draw(img)
    floor_y = int(h * 0.82)
    draw.rectangle((0, int(h * 0.70), w, int(h * 0.78)), fill=WALL_SHADE)
    draw.rectangle((0, int(h * 0.78), w, h), fill=FLOOR)
    draw.rectangle((0, int(h * 0.90), w, h), fill=FLOOR_BAND)
    _stroke_round_cap(draw, (28, floor_y), (w - 28, floor_y), width=5.5, fill=FLOOR_LINE)
    # Left wall corner shadow band
    cx = int(w * 0.04)
    draw.rectangle((cx - 6, int(h * 0.06), cx + 22, floor_y), fill=CORNER_SHADOW)
    _stroke_round_cap(draw, (cx, int(h * 0.08)), (cx, floor_y), width=5.5)
    return img


def generate_door_frame(*, w: int = 420, h: int = 520) -> Image.Image:
    """Open doorway + swinging door panel (transparent canvas)."""
    img = _new_rgba(w, h)
    draw = ImageDraw.Draw(img)
    x0, y0 = 20, 10
    x1, y1 = w - 30, h - 10
    draw.rectangle((x0, y0, x1, y1), fill=(*DOOR_RECESS[:3], 90))
    _stroke_round_cap(draw, (x0, y1), (x0, y0), width=6.5)
    _stroke_round_cap(draw, (x0, y0), (x1, y0), width=6.5)
    _stroke_round_cap(draw, (x1, y0), (x1, y1), width=6.5)
    # Open door panel
    ox = x1 + 52
    thick = 12
    panel = [(x1, y0 + 3), (ox, y0 + 16), (ox, y1 - 4), (x1, y1 - 2)]
    draw.polygon(panel, fill=CREAM, outline=BLACK, width=5)
    edge = [
        (ox, y0 + 16),
        (ox + thick, y0 + 20),
        (ox + thick, y1 - 8),
        (ox, y1 - 4),
    ]
    draw.polygon(edge, fill=FLOOR_BAND, outline=BLACK, width=4)
    mid_x = (x1 + ox) / 2
    _stroke_round_cap(draw, (mid_x, y0 + 28), (mid_x + 2, y1 - 18), width=3.5)
    return img


def generate_crib_back(*, w: int = 480, h: int = 320) -> Image.Image:
    """Crib body, side depth, mattress (no front bars)."""
    img = _new_rgba(w, h)
    draw = ImageDraw.Draw(img)
    x0, y0 = 60, 20
    x1, y1 = w - 20, h - 40
    depth = 34
    cw, ch = x1 - x0, y1 - y0
    side = [(x0, y0), (x0 - depth, y0 + 12), (x0 - depth, y1 + 6), (x0, y1)]
    draw.polygon(side, fill=CRIB_SIDE, outline=BLACK, width=5)
    draw.rounded_rectangle((x0, y0, x1, y1), radius=4, fill=CRIB_GREY, outline=BLACK, width=6)
    draw.rectangle((x0 + 10, y0 + 10, x1 - 10, y0 + 10 + ch * 0.22), fill=(0x8A, 0x8A, 0x8A, 140))
    for px, py, pw, ph in (
        (x0 - 5, y0 - 10, 14, ch + 18),
        (x1 - 9, y0 - 10, 14, ch + 18),
        (x0 - depth - 3, y0 + 4, 12, ch + 14),
    ):
        draw.rounded_rectangle((px, py, px + pw, py + ph), radius=3, fill=CRIB_POST, outline=BLACK, width=4)
    _stroke_round_cap(draw, (x0 - 4, y0), (x1 + 4, y0), width=10)
    _stroke_round_cap(draw, (x0 - 4, y1), (x1 + 4, y1), width=10)
    _stroke_round_cap(draw, (x0, y0), (x0 - depth, y0 + 12), width=8)
    _stroke_round_cap(draw, (x0, y1), (x0 - depth, y1 + 6), width=8)
    mx0, my0 = x0 + 16, y1 - 52
    mw, mh = cw - 32, 30
    draw.rounded_rectangle((mx0, my0, mx0 + mw, my0 + mh), radius=2, fill=MATTRESS, outline=BLACK, width=3)
    lip = [(mx0, my0 + mh), (mx0, my0 + mh + 14), (mx0 + mw, my0 + mh + 14), (mx0 + mw, my0 + mh)]
    draw.polygon(lip, fill=MATTRESS_LIP, outline=BLACK, width=3)
    m_depth = 18
    side_mat = [(mx0, my0), (mx0 - m_depth, my0 + 8), (mx0 - m_depth, my0 + mh + 8), (mx0, my0 + mh)]
    draw.polygon(side_mat, fill=(0xC8, 0xC8, 0xC8, 255), outline=BLACK, width=2)
    return img


def generate_crib_front(*, w: int = 480, h: int = 320) -> Image.Image:
    """Front vertical crib bars only (overlay in front of baby)."""
    img = _new_rgba(w, h)
    draw = ImageDraw.Draw(img)
    x0, y0 = 60, 20
    x1, y1 = w - 20, h - 40
    cw = x1 - x0
    n_bars = 9
    for i in range(n_bars):
        t = (i + 0.5) / n_bars
        x = x0 + 14 + t * (cw - 28)
        _stroke_round_cap(draw, (x, y0 + 10), (x, y1 - 10), width=5)
    return img


def _draw_head(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    r: float,
    *,
    emotion: str = "neutral",
    look_left: bool = False,
    hair: bool = False,
) -> None:
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=WHITE, outline=BLACK, width=int(max(5, r * 0.12)))
    eye_r = max(7.5, r * 0.20)
    ox = -6 if look_left else 6
    for ex in (cx - r * 0.34, cx + r * 0.34):
        draw.ellipse((ex - eye_r, cy - r * 0.06 - eye_r, ex + eye_r, cy - r * 0.06 + eye_r), fill=WHITE, outline=BLACK, width=2)
        pr = max(2.2, eye_r * 0.42)
        draw.ellipse((ex + ox - pr, cy - r * 0.06 + 1.5 - pr, ex + ox + pr, cy - r * 0.06 + 1.5 + pr), fill=BLACK)
    if emotion == "surprise":
        draw.ellipse(
            (cx - r * 0.13, cy + r * 0.28, cx + r * 0.13, cy + r * 0.52),
            outline=BLACK,
            width=3,
        )
        brow_y = cy - r * 0.06 - eye_r * 1.35 - 6
        for ex in (cx - r * 0.34, cx + r * 0.34):
            draw.line([(ex - eye_r, brow_y), (ex + eye_r, brow_y - 3)], fill=BLACK, width=3)
    elif emotion == "smile":
        draw.arc(
            (cx - r * 0.26, cy + r * 0.18, cx + r * 0.26, cy + r * 0.50),
            start=200,
            end=340,
            fill=BLACK,
            width=3,
        )
        brow_y = cy - r * 0.06 - eye_r * 1.35 - 1
        for ex in (cx - r * 0.34, cx + r * 0.34):
            draw.line([(ex - eye_r, brow_y), (ex + eye_r, brow_y)], fill=BLACK, width=3)
    if hair:
        base_y = cy - r + 2
        for ox in (-12, 0, 12):
            draw.line([(cx + ox * 0.3, base_y), (cx + ox, base_y - 18)], fill=BLACK, width=3)


def generate_head(*, role: str, emotion: str) -> Image.Image:
    r = 54 if role == "adult" else 40
    pad = int(r + 20)
    size = pad * 2
    img = _new_rgba(size, size)
    draw = ImageDraw.Draw(img)
    cx, cy = pad, pad
    look_left = role == "adult" and emotion == "smile"
    _draw_head(draw, cx, cy, r, emotion=emotion, look_left=look_left, hair=(role == "baby"))
    return img


def generate_torso(*, role: str, pose: str) -> Image.Image:
    w, h = (80, 200) if role == "adult" else (60, 140)
    img = _new_rgba(w, h)
    draw = ImageDraw.Draw(img)
    cx = w // 2
    sw = 6.5 if role == "adult" else 5.5
    neck_y = 8
    if role == "baby" and pose == "arms_up":
        hip_y = h - 50
    else:
        hip_y = h - 20
    _stroke_round_cap(draw, (cx, neck_y), (cx, hip_y), width=sw)
    return img


def generate_limb(
    *,
    role: str,
    limb: str,
    side: str,
    pose: str,
) -> Image.Image:
    """Single arm or leg segment PNG with anchor at shoulder/hip end."""
    sw = 6.5 if role == "adult" else 5.5
    w, h = 160, 160
    img = _new_rgba(w, h)
    draw = ImageDraw.Draw(img)
    anchor = (80, 20)
    if role == "adult" and pose == "enter_door":
        if limb == "arm" and side == "l":
            pts = [anchor, (40, 70), (15, 110)]
        elif limb == "arm" and side == "r":
            pts = [anchor, (105, 55), (120, 85)]
        elif limb == "leg" and side == "l":
            pts = [(80, 80), (50, 120), (30, 150)]
        else:
            pts = [(80, 80), (115, 105), (130, 130)]
    elif role == "baby" and pose == "arms_up":
        if limb == "arm" and side == "l":
            pts = [anchor, (55, 50), (35, 20)]
        elif limb == "arm" and side == "r":
            pts = [anchor, (105, 50), (125, 20)]
        elif limb == "leg" and side == "l":
            pts = [(80, 80), (55, 110), (40, 130)]
        else:
            pts = [(80, 80), (105, 110), (120, 130)]
    else:
        pts = [anchor, (anchor[0], anchor[1] + 60)]
    for i in range(len(pts) - 1):
        _stroke_round_cap(draw, pts[i], pts[i + 1], width=sw)
    return img


def generate_speech_bubble(*, bars: int = 3) -> Image.Image:
    w, h = 180, 180
    img = _new_rgba(w, h)
    draw = ImageDraw.Draw(img)
    bx, by, br = 90, 70, 56
    draw.ellipse((bx - br, by - br, bx + br, by + br), fill=WHITE, outline=BLACK, width=5)
    tail = [(bx - 14, by + br * 0.72), (30, h - 20), (bx + 16, by + br * 0.62)]
    draw.polygon(tail, fill=WHITE, outline=BLACK)
    draw.ellipse((bx - br * 0.72, by - br * 0.55 + 8, bx + br * 0.72, by + br * 0.55 + 8), fill=WHITE)
    gap = 20
    n = max(1, bars)
    total_w = (n - 1) * gap
    start_x = bx - total_w / 2
    heights = (34, 42, 36, 40, 30)
    for i in range(n):
        bar_x = start_x + i * gap
        bar_h = heights[i % len(heights)]
        draw.rounded_rectangle(
            (bar_x - 7, by - bar_h / 2, bar_x + 7, by + bar_h / 2),
            radius=3,
            fill=CORAL,
        )
    return img


def _save(img: Image.Image, rel: str) -> Path:
    path = ASSETS_ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
    return path


def bootstrap_assets(*, force: bool = False) -> list[Path]:
    """Generate all compositor PNGs; skip existing unless force=True."""
    manifest: list[tuple[str, Callable[[], Image.Image]]] = [
        ("backgrounds/nursery_room.png", lambda: generate_nursery_room()),
        ("props/door_frame.png", generate_door_frame),
        ("props/crib_back.png", generate_crib_back),
        ("props/crib_front.png", generate_crib_front),
        ("props/speech_bubble_3bars.png", lambda: generate_speech_bubble(bars=3)),
        ("parts/adult_head_smile.png", lambda: generate_head(role="adult", emotion="smile")),
        ("parts/baby_head_surprise.png", lambda: generate_head(role="baby", emotion="surprise")),
        ("characters/adult_torso_walk.png", lambda: generate_torso(role="adult", pose="enter_door")),
        ("characters/baby_torso_seated.png", lambda: generate_torso(role="baby", pose="arms_up")),
        ("parts/adult_arm_l_walk.png", lambda: generate_limb(role="adult", limb="arm", side="l", pose="enter_door")),
        ("parts/adult_arm_r_walk.png", lambda: generate_limb(role="adult", limb="arm", side="r", pose="enter_door")),
        ("parts/adult_leg_l_walk.png", lambda: generate_limb(role="adult", limb="leg", side="l", pose="enter_door")),
        ("parts/adult_leg_r_walk.png", lambda: generate_limb(role="adult", limb="leg", side="r", pose="enter_door")),
        ("parts/baby_arm_l_up.png", lambda: generate_limb(role="baby", limb="arm", side="l", pose="arms_up")),
        ("parts/baby_arm_r_up.png", lambda: generate_limb(role="baby", limb="arm", side="r", pose="arms_up")),
        ("parts/baby_leg_l.png", lambda: generate_limb(role="baby", limb="leg", side="l", pose="arms_up")),
        ("parts/baby_leg_r.png", lambda: generate_limb(role="baby", limb="leg", side="r", pose="arms_up")),
    ]
    written: list[Path] = []
    for rel, fn in manifest:
        out = ASSETS_ROOT / rel
        if out.exists() and not force:
            written.append(out)
            continue
        written.append(_save(fn(), rel))
    return written


if __name__ == "__main__":
    paths = bootstrap_assets(force="--force" in __import__("sys").argv)
    print(f"Bootstrap complete: {len(paths)} assets under {ASSETS_ROOT}")
