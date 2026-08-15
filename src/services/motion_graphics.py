"""Motion-graphics plates for napstorian (timeline wipe, faction bars, map labels).

Historian stays soft: poetic chapter titles only (delegates to premium_overlays tone).
Pillow-first; Remotion is P2 (see assets/remotion_scaffold/README.md).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

ChannelTone = Literal["napstorian", "napping_historian"]

_YEAR_RE = re.compile(r"\b((?:1[0-9]|20)\d{2})\b")


def motion_mode_wants_graphics(motion: dict[str, Any] | None) -> bool:
    m = motion or {}
    mode = str(m.get("motion_mode") or m.get("mode") or "").strip().lower()
    return mode in {"motion_graphics", "mg", "hud"} or bool(m.get("motion_graphics"))


def render_timeline_wipe(
    *,
    years: list[str],
    progress: float,
    out_path: Path,
    width: int = 1280,
    height: int = 160,
    accent: tuple[int, int, int] = (196, 160, 88),
) -> Path:
    """Horizontal timeline rail with wipe progress (napstorian HUD)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Pillow required for motion_graphics") from exc

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (width, height), (10, 12, 16, 220))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22
        )
        font_sm = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16
        )
    except OSError:
        font = ImageFont.load_default()
        font_sm = font

    pad = 48
    y_mid = height // 2
    draw.line([(pad, y_mid), (width - pad, y_mid)], fill=(80, 70, 55), width=4)
    p = max(0.0, min(1.0, progress))
    wipe_x = pad + int((width - 2 * pad) * p)
    draw.line([(pad, y_mid), (wipe_x, y_mid)], fill=accent, width=6)
    draw.ellipse([wipe_x - 8, y_mid - 8, wipe_x + 8, y_mid + 8], fill=accent)

    labels = years[:6] or ["—"]
    for i, lab in enumerate(labels):
        x = pad + int((width - 2 * pad) * (i / max(len(labels) - 1, 1)))
        draw.rectangle([x - 2, y_mid - 12, x + 2, y_mid + 12], fill=accent)
        draw.text((x - 20, y_mid + 18), str(lab), fill=(230, 220, 190), font=font_sm)
    draw.text((pad, 16), "TIMELINE", fill=accent, font=font)
    img.save(out_path)
    return out_path


def render_faction_color_bars(
    *,
    factions: list[tuple[str, str]],
    out_path: Path,
    width: int = 1280,
    height: int = 72,
) -> Path:
    """Bottom faction legend bars (name + hex color)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Pillow required for motion_graphics") from exc

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (width, height), (8, 10, 14, 200))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18
        )
    except OSError:
        font = ImageFont.load_default()

    n = max(1, len(factions))
    slot = width // n
    for i, (name, hex_c) in enumerate(factions):
        h = hex_c.lstrip("#")
        try:
            rgb = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        except (ValueError, IndexError):
            rgb = (140, 40, 40)
        x0 = i * slot + 16
        draw.rectangle([x0, 18, x0 + 36, height - 18], fill=rgb)
        draw.text((x0 + 48, 22), name[:28], fill=(230, 220, 190), font=font)
    img.save(out_path)
    return out_path


def render_map_style_label(
    *,
    label: str,
    sublabel: str = "",
    out_path: Path,
    width: int = 480,
    height: int = 96,
    accent: tuple[int, int, int] = (196, 160, 88),
) -> Path:
    """On-map style callout label plate."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Pillow required for motion_graphics") from exc

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26
        )
        font_sm = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16
        )
    except OSError:
        font = ImageFont.load_default()
        font_sm = font
    draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=6, fill=(14, 16, 22, 210))
    draw.rectangle([0, 0, 10, height], fill=accent)
    draw.text((24, 18), (label or "REGION")[:40], fill=(235, 220, 180), font=font)
    if sublabel:
        draw.text((24, 54), sublabel[:48], fill=accent, font=font_sm)
    img.save(out_path)
    return out_path


def render_poetic_chapter_title(
    *,
    title: str,
    out_path: Path,
    width: int = 1280,
    height: int = 200,
) -> Path:
    """Soft historian chapter plate (no slam HUD)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Pillow required for motion_graphics") from exc

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (width, height), (12, 14, 18, 160))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", 36
        )
    except OSError:
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 34
            )
        except OSError:
            font = ImageFont.load_default()
    draw.text((64, 70), (title or "Chapter")[:64], fill=(220, 210, 195), font=font)
    draw.line([(64, 130), (320, 130)], fill=(160, 150, 130), width=1)
    img.save(out_path)
    return out_path


def plan_motion_plates_for_script(
    script: dict[str, Any] | None,
    *,
    channel: str = "napstorian",
    out_dir: Path,
    max_plates: int = 8,
) -> list[dict[str, Any]]:
    """Emit plate PNGs + plan rows for compose / tests."""
    script = script or {}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ch = (channel or "napstorian").strip().lower()
    rows: list[dict[str, Any]] = []

    if ch in {"napping_historian", "historian", "sleep"}:
        chapters = (script.get("outline") or {}).get("chapters") or []
        for i, ch_row in enumerate(chapters[:max_plates]):
            title = str(ch_row.get("title") or ch_row.get("name") or f"Chapter {i + 1}")
            path = out_dir / f"chapter_soft_{i:02d}.png"
            render_poetic_chapter_title(title=title, out_path=path)
            rows.append({"kind": "poetic_chapter", "path": str(path), "title": title})
        return rows

    # Napstorian: timeline + faction + labels
    blob = " ".join(
        str(s.get("text") or "") for s in (script.get("scenes") or [])[:60]
    )
    years = list(dict.fromkeys(_YEAR_RE.findall(blob)))[:6]
    wipe = out_dir / "timeline_wipe.png"
    render_timeline_wipe(years=years or ["1500", "1550", "1600"], progress=0.45, out_path=wipe)
    rows.append({"kind": "timeline_wipe", "path": str(wipe), "years": years})

    bars = out_dir / "faction_bars.png"
    render_faction_color_bars(
        factions=[("Crown", "#8B1E1E"), ("Rivals", "#1E3A5F"), ("Church", "#C4A058")],
        out_path=bars,
    )
    rows.append({"kind": "faction_bars", "path": str(bars)})

    title = str(script.get("title") or "Campaign")[:40]
    label = out_dir / "map_label.png"
    render_map_style_label(label=title, sublabel="THEATRE OF WAR", out_path=label)
    rows.append({"kind": "map_label", "path": str(label)})
    return rows
