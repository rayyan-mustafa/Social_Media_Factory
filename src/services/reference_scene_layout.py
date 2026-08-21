from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw


@dataclass
class FigureSpec:
    cx: float
    cy: float
    head_r: float
    torso_len: float
    arm_len: float
    leg_len: float
    adult: bool = False
    head_tilt: float = 0.0


@dataclass
class ChairSpec:
    x: float
    y: float
    width: float
    height: float
    back_height: float


@dataclass
class SceneLayout:
    width: int = 1280
    height: int = 720
    wall_color: tuple[int, int, int] = (236, 235, 229)
    floor_color: tuple[int, int, int] = (226, 214, 195)
    table: dict[str, Any] = field(
        default_factory=lambda: {
            "x": 0.5,
            "y": 0.71,
            "width": 0.66,
            "height": 0.17,
            "radius": 20,
            "fill": (174, 118, 74),
        }
    )
    paper: dict[str, Any] = field(
        default_factory=lambda: {
            "x": 0.5,
            "y": 0.57,
            "width": 0.28,
            "height": 0.12,
            "fill": (250, 246, 242),
        }
    )
    chairs: list[ChairSpec] = field(
        default_factory=lambda: [
            ChairSpec(x=0.24, y=0.78, width=0.08, height=0.18, back_height=0.12),
            ChairSpec(x=0.76, y=0.78, width=0.08, height=0.18, back_height=0.12),
        ]
    )
    child: FigureSpec = field(
        default_factory=lambda: FigureSpec(cx=0.33, cy=0.47, head_r=0.055, torso_len=0.14, arm_len=0.11, leg_len=0.15, adult=False)
    )
    adult: FigureSpec = field(
        default_factory=lambda: FigureSpec(cx=0.73, cy=0.4, head_r=0.075, torso_len=0.18, arm_len=0.14, leg_len=0.18, adult=True, head_tilt=-8.0)
    )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["chairs"] = [asdict(item) for item in self.chairs]
        return data


def infer_layout(reference_path: str | None = None, *, width: int = 1280, height: int = 720) -> SceneLayout:
    """Create a parameterized layout from a reference image when available.

    This is intentionally conservative: it preserves the same composition style
    and balances the proportions for a warm nursery / discussion scene without
    trying to recreate a proprietary image pixel-for-pixel.
    """
    layout = SceneLayout(width=width, height=height)
    if not reference_path:
        return layout

    ref = Path(reference_path)
    if not ref.exists():
        return layout

    try:
        with Image.open(ref) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            ref_w, ref_h = img.size
            if ref_w and ref_h:
                ratio = ref_w / ref_h
                if ratio > 1.4:
                    layout.table["x"] = 0.5
                    layout.table["y"] = 0.7
                    layout.table["width"] = 0.72
                    layout.table["height"] = 0.17
                    layout.child.cx = 0.31
                    layout.adult.cx = 0.72
                    layout.child.head_r = 0.048
                    layout.adult.head_r = 0.068
    except Exception:
        pass
    return layout


def _draw_stick_figure(draw: ImageDraw.ImageDraw, fig: FigureSpec, *, width: int, height: int) -> None:
    cx = int(fig.cx * width)
    cy = int(fig.cy * height)
    head_r = int(fig.head_r * min(width, height))
    torso_len = int(fig.torso_len * min(width, height))
    arm_len = int(fig.arm_len * min(width, height))
    leg_len = int(fig.leg_len * min(width, height))
    shoulder_y = cy + head_r + 8
    torso_end_y = shoulder_y + torso_len
    # head
    draw.ellipse((cx - head_r, cy - head_r, cx + head_r, cy + head_r), outline=(26, 26, 24), width=4, fill=(255, 255, 255))
    # eyes
    eye_y = cy + 4
    eye_x = head_r // 3
    draw.ellipse((cx - eye_x - 4, eye_y - 2, cx - eye_x + 2, eye_y + 4), fill=(26, 26, 24))
    draw.ellipse((cx + eye_x - 2, eye_y - 2, cx + eye_x + 4, eye_y + 4), fill=(26, 26, 24))
    draw.arc((cx - 12, cy + 10, cx + 12, cy + 28), start=190, end=350, fill=(26, 26, 24), width=3)
    # torso
    draw.line((cx, shoulder_y, cx, torso_end_y), fill=(26, 26, 24), width=5)
    # arms
    draw.line((cx, shoulder_y + 18, cx - arm_len, shoulder_y + 30), fill=(26, 26, 24), width=5)
    draw.line((cx, shoulder_y + 18, cx + arm_len, shoulder_y + 30), fill=(26, 26, 24), width=5)
    # legs
    draw.line((cx, torso_end_y, cx - int(leg_len * 0.7), torso_end_y + leg_len), fill=(26, 26, 24), width=5)
    draw.line((cx, torso_end_y, cx + int(leg_len * 0.7), torso_end_y + leg_len), fill=(26, 26, 24), width=5)

    if fig.adult:
        # adult chair and a bit more grounded pose with visible shoulders
        draw.line((cx - 30, shoulder_y + 35, cx + 40, shoulder_y + 30), fill=(26, 26, 24), width=4)
    else:
        # child figure with smaller body and more upright seat
        draw.line((cx - 10, shoulder_y + 10, cx + 18, shoulder_y + 18), fill=(26, 26, 24), width=4)


def _draw_chair(draw: ImageDraw.ImageDraw, chair: ChairSpec, *, width: int, height: int) -> None:
    x = int(chair.x * width)
    y = int(chair.y * height)
    chair_w = int(chair.width * width)
    chair_h = int(chair.height * height)
    back_h = int(chair.back_height * height)
    draw.rounded_rectangle((x - chair_w // 2, y - chair_h, x + chair_w // 2, y), radius=12, fill=(115, 84, 56), outline=(36, 28, 22), width=3)
    draw.rectangle((x - chair_w // 2, y, x + chair_w // 2, y + back_h), fill=(130, 98, 66), outline=(36, 28, 22), width=3)


def render_scene_layout(layout: SceneLayout | dict[str, Any], out_path: str | Path | None = None) -> Image.Image:
    """Render a warm nursery scene using a parameterized layout."""
    if isinstance(layout, dict):
        layout = SceneLayout(**layout)

    width = int(layout.width)
    height = int(layout.height)
    img = Image.new("RGB", (width, height), layout.wall_color)
    draw = ImageDraw.Draw(img)

    # warm room lighting
    for y in range(height):
        shade = max(0, min(255, 235 - int((y / max(1, height)) * 28)))
        draw.line((0, y, width, y), fill=(shade, shade, shade))

    # floor area
    floor_y = int(height * 0.72)
    draw.rectangle((0, floor_y, width, height), fill=layout.floor_color)

    # table
    table = layout.table
    table_x = int(table["x"] * width)
    table_y = int(table["y"] * height)
    table_w = int(table["width"] * width)
    table_h = int(table["height"] * height)
    draw.rounded_rectangle((table_x - table_w // 2, table_y - table_h // 2, table_x + table_w // 2, table_y + table_h // 2), radius=int(table["radius"]), fill=tuple(table["fill"]), outline=(52, 38, 24), width=4)

    # table legs
    table_left = table_x - table_w // 2 + 55
    table_right = table_x + table_w // 2 - 55
    leg_y = table_y + table_h // 2 + 12
    for leg_x in (table_left, table_right):
        draw.rectangle((leg_x - 18, leg_y, leg_x + 18, height), fill=(130, 90, 54), outline=(52, 38, 24), width=3)

    # paper and crayons
    paper = layout.paper
    paper_x = int(paper["x"] * width)
    paper_y = int(paper["y"] * height)
    paper_w = int(paper["width"] * width)
    paper_h = int(paper["height"] * height)
    draw.rounded_rectangle((paper_x - paper_w // 2, paper_y - paper_h // 2, paper_x + paper_w // 2, paper_y + paper_h // 2), radius=8, fill=tuple(paper["fill"]), outline=(30, 30, 30), width=3)
    for x in [paper_x - paper_w // 2 + 25, paper_x - paper_w // 2 + 60, paper_x - paper_w // 2 + 95, paper_x + paper_w // 2 - 25, paper_x + paper_w // 2 - 60]:
        draw.rounded_rectangle((x, paper_y + 15, x + 18, paper_y + 40), radius=5, fill=(238, 84, 66), outline=(0, 0, 0), width=2)

    # chairs
    for chair in layout.chairs:
        _draw_chair(draw, chair, width=width, height=height)

    # figures
    _draw_stick_figure(draw, layout.child, width=width, height=height)
    _draw_stick_figure(draw, layout.adult, width=width, height=height)

    # reference-style warm glow and shadow accents
    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shdraw = ImageDraw.Draw(shadow)
    shdraw.ellipse((int(width * 0.18), int(height * 0.16), int(width * 0.82), int(height * 0.72)), fill=(248, 214, 161, 50))
    img = Image.alpha_composite(img.convert("RGBA"), shadow).convert("RGB")

    if out_path is not None:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out)

    return img


def save_layout_json(layout: SceneLayout, out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(layout.to_dict(), indent=2), encoding="utf-8")
    return out


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate a reference-inspired Pillow scene layout from a source image.")
    parser.add_argument("--reference", type=str, default=None, help="Optional reference image used to infer the scene layout.")
    parser.add_argument("--out", type=str, default="output/reference_style_match.png", help="Output PNG path.")
    parser.add_argument("--layout-json", type=str, default="output/reference_scene_layout.json", help="Optional JSON layout export path.")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    args = parser.parse_args()

    layout = infer_layout(args.reference, width=args.width, height=args.height)
    render_scene_layout(layout, out_path=args.out)
    save_layout_json(layout, args.layout_json)
    print(f"rendered {args.out}")
    print(f"layout {args.layout_json}")


if __name__ == "__main__":
    _cli()
