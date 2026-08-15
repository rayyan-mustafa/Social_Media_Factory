"""Animated campaign map overlays (Pillow + ffmpeg) — K&G / AHHub style v1.

Generates short map clips with pan, route arrows, date stamps, and HUD chrome.
Composable into the farm via ``scene_XXX_clip.mp4`` (same mux path as PD motion).

Remotion is optional P2 — see ``assets/remotion_scaffold/README.md``.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

_YEAR_RE = re.compile(r"\b((?:1[0-9]|20)\d{2})\b")
_PLACE_HINTS: list[tuple[re.Pattern[str], str, float, float]] = [
    # name matcher → label, x_norm, y_norm (0..1 on map)
    (re.compile(r"\bLondon\b", re.I), "London", 0.42, 0.38),
    (re.compile(r"\bParis\b", re.I), "Paris", 0.45, 0.48),
    (re.compile(r"\bVienna\b", re.I), "Vienna", 0.58, 0.46),
    (re.compile(r"\bRome\b", re.I), "Rome", 0.55, 0.62),
    (re.compile(r"\bConstantinople\b|\bIstanbul\b", re.I), "Constantinople", 0.68, 0.58),
    (re.compile(r"\bMoscow\b", re.I), "Moscow", 0.72, 0.28),
    (re.compile(r"\bEdinburgh\b|\bScotland\b", re.I), "Edinburgh", 0.40, 0.28),
    (re.compile(r"\bMadrid\b|\bSpain\b", re.I), "Madrid", 0.38, 0.62),
    (re.compile(r"\bCairo\b|\bEgypt\b", re.I), "Cairo", 0.66, 0.72),
    (re.compile(r"\bJerusalem\b", re.I), "Jerusalem", 0.70, 0.68),
    (re.compile(r"\bBerlin\b", re.I), "Berlin", 0.55, 0.36),
    (re.compile(r"\bAtlantic\b", re.I), "Atlantic", 0.22, 0.50),
    (re.compile(r"\bChannel\b|\bCalais\b", re.I), "Calais", 0.44, 0.42),
]


@dataclass
class MapWaypoint:
    label: str
    x: float  # 0..1
    y: float
    year: str | None = None


@dataclass
class MapMotionPlan:
    title: str
    date_stamp: str
    waypoints: list[MapWaypoint] = field(default_factory=list)
    duration_s: float = 4.0
    fps: int = 24
    width: int = 1280
    height: int = 720
    faction_a: str = "#8B1E1E"
    faction_b: str = "#1E3A5F"

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "date_stamp": self.date_stamp,
            "waypoints": [asdict(w) for w in self.waypoints],
            "duration_s": self.duration_s,
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "faction_a": self.faction_a,
            "faction_b": self.faction_b,
        }


def motion_mode_wants_maps(motion: dict[str, Any] | None) -> bool:
    """True when editing motion block requests animated maps."""
    m = motion or {}
    mode = str(m.get("motion_mode") or m.get("mode") or "").strip().lower()
    if mode in {"animated_maps", "maps", "campaign_maps"}:
        return True
    if m.get("animated_maps") is True:
        return True
    return False


def extract_waypoints_from_text(text: str, *, limit: int = 5) -> list[MapWaypoint]:
    """Heuristic location extraction from narration (no LLM)."""
    found: list[MapWaypoint] = []
    seen: set[str] = set()
    years = _YEAR_RE.findall(text or "")
    year = years[0] if years else None
    for pat, label, x, y in _PLACE_HINTS:
        if pat.search(text or "") and label not in seen:
            found.append(MapWaypoint(label=label, x=x, y=y, year=year))
            seen.add(label)
            if len(found) >= limit:
                break
    if not found:
        # Synthetic campaign spine so clips still look intentional.
        found = [
            MapWaypoint("Origin", 0.30, 0.55, year),
            MapWaypoint("Advance", 0.50, 0.45, year),
            MapWaypoint("Front", 0.70, 0.40, year),
        ]
    return found


def plan_from_script(
    script: dict[str, Any] | None,
    *,
    scene_index: int | None = None,
    duration_s: float = 4.0,
) -> MapMotionPlan:
    """Build a map plan from script title + scene (or whole script) text."""
    script = script or {}
    title = str(script.get("title") or script.get("topic") or "Campaign")[:64]
    scenes = list(script.get("scenes") or [])
    text_bits: list[str] = [title]
    date_stamp = ""
    if scene_index is not None:
        for sc in scenes:
            if int(sc.get("index", -1)) == int(scene_index):
                text_bits.append(str(sc.get("text") or ""))
                break
    else:
        for sc in scenes[:40]:
            text_bits.append(str(sc.get("text") or ""))
    blob = " ".join(text_bits)
    years = _YEAR_RE.findall(blob)
    date_stamp = years[0] if years else "—"
    waypoints = extract_waypoints_from_text(blob)
    return MapMotionPlan(
        title=title,
        date_stamp=date_stamp,
        waypoints=waypoints,
        duration_s=float(duration_s),
    )


def _hex_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (180, 40, 40)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _draw_arrow(
    draw: Any,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    *,
    color: tuple[int, int, int],
    width: int = 4,
) -> None:
    draw.line([(x0, y0), (x1, y1)], fill=color, width=width)
    ang = math.atan2(y1 - y0, x1 - x0)
    ah = 14
    for da in (2.6, -2.6):
        ax = int(x1 - ah * math.cos(ang + da))
        ay = int(y1 - ah * math.sin(ang + da))
        draw.line([(x1, y1), (ax, ay)], fill=color, width=width)


def render_map_frame(
    plan: MapMotionPlan,
    *,
    t: float,
    out_path: Path | None = None,
) -> Any:
    """Render one RGBA/RGB frame at time t (0..duration). Returns PIL Image."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Pillow required for map_motion") from exc

    w, h = plan.width, plan.height
    progress = 0.0 if plan.duration_s <= 0 else max(0.0, min(1.0, t / plan.duration_s))

    # Slow pan: base parchment + slight drift
    pan_x = int(24 * math.sin(progress * math.pi))
    pan_y = int(16 * math.cos(progress * math.pi * 0.5))

    img = Image.new("RGB", (w, h), (28, 24, 18))
    draw = ImageDraw.Draw(img)
    # Gradient parchment
    for y in range(h):
        u = y / max(h - 1, 1)
        r = int(42 + 38 * u)
        g = int(36 + 22 * (1 - u))
        b = int(28 + 10 * u)
        draw.line([(0, y), (w, y)], fill=(r, g, b))

    # Grid / longitude-latitude feel
    grid = (70, 60, 45)
    for gx in range(-40 + pan_x, w + 40, 80):
        draw.line([(gx, 0), (gx, h)], fill=grid, width=1)
    for gy in range(-40 + pan_y, h + 40, 60):
        draw.line([(0, gy), (w, gy)], fill=grid, width=1)

    # Soft landmass blobs
    land = (58, 72, 52)
    for cx, cy, rx, ry in (
        (0.45 + pan_x / w, 0.42 + pan_y / h, 0.28, 0.22),
        (0.62, 0.55, 0.18, 0.15),
        (0.35, 0.58, 0.12, 0.10),
    ):
        bbox = [
            int((cx - rx) * w),
            int((cy - ry) * h),
            int((cx + rx) * w),
            int((cy + ry) * h),
        ]
        draw.ellipse(bbox, fill=land, outline=(90, 100, 70), width=2)

    try:
        font_title = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28
        )
        font_label = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18
        )
        font_hud = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16
        )
    except OSError:
        font_title = ImageFont.load_default()
        font_label = font_title
        font_hud = font_title

    fa = _hex_rgb(plan.faction_a)
    fb = _hex_rgb(plan.faction_b)

    pts = plan.waypoints
    n = max(1, len(pts) - 1)
    # Reveal route progressively
    reveal = progress * n
    for i in range(len(pts) - 1):
        if reveal < i:
            break
        seg_t = 1.0 if reveal >= i + 1 else (reveal - i)
        a, b = pts[i], pts[i + 1]
        x0 = int(a.x * w) + pan_x // 2
        y0 = int(a.y * h) + pan_y // 2
        x1 = int(a.x * w + (b.x - a.x) * w * seg_t) + pan_x // 2
        y1 = int(a.y * h + (b.y - a.y) * h * seg_t) + pan_y // 2
        col = fa if i % 2 == 0 else fb
        _draw_arrow(draw, x0, y0, x1, y1, color=col, width=5)

    for i, wp in enumerate(pts):
        if reveal + 0.15 < i:
            continue
        px = int(wp.x * w) + pan_x // 2
        py = int(wp.y * h) + pan_y // 2
        r = 7
        draw.ellipse([px - r, py - r, px + r, py + r], fill=(235, 210, 140), outline=fa)
        draw.text((px + 10, py - 10), wp.label, fill=(235, 220, 180), font=font_label)

    # HUD chrome
    draw.rectangle([0, 0, w, 56], fill=(12, 14, 18))
    draw.rectangle([0, h - 48, w, h], fill=(12, 14, 18))
    draw.text((24, 14), plan.title[:70], fill=(235, 220, 180), font=font_title)
    draw.text((w - 160, 16), str(plan.date_stamp), fill=(196, 160, 88), font=font_hud)
    draw.rectangle([24, h - 36, 24 + int((w - 48) * progress), h - 28], fill=fa)
    draw.text((24, h - 22), "CAMPAIGN FRONT", fill=(160, 150, 130), font=font_hud)
    # Faction bars
    draw.rectangle([w - 220, h - 36, w - 120, h - 20], fill=fa)
    draw.rectangle([w - 110, h - 36, w - 24, h - 20], fill=fb)

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, quality=92)
    return img


def generate_map_clip(
    plan: MapMotionPlan,
    *,
    out_path: Path,
    ffmpeg_bin: str = "ffmpeg",
) -> Path:
    """Write animated map MP4 from plan (Pillow frames → ffmpeg)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_frames = max(1, int(round(plan.duration_s * plan.fps)))
    with tempfile.TemporaryDirectory(prefix="map_motion_") as tmp:
        tmp_dir = Path(tmp)
        for i in range(n_frames):
            t = i / plan.fps
            frame_path = tmp_dir / f"frame_{i:04d}.jpg"
            render_map_frame(plan, t=t, out_path=frame_path)
        cmd = [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-framerate",
            str(plan.fps),
            "-i",
            str(tmp_dir / "frame_%04d.jpg"),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-t",
            f"{plan.duration_s:.3f}",
            str(out_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
    plan_sidecar = out_path.with_suffix(".map.json")
    plan_sidecar.write_text(
        json.dumps(plan.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return out_path


def generate_map_clips_for_job(
    script: dict[str, Any],
    *,
    out_dir: Path,
    scene_indices: list[int] | None = None,
    max_clips: int = 6,
    duration_s: float = 4.0,
) -> list[dict[str, Any]]:
    """Generate map clips for selected scenes; returns manifest rows."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    scenes = list(script.get("scenes") or [])
    if scene_indices is None:
        # Spread across chapters / length
        n = len(scenes)
        if n == 0:
            return []
        k = min(max_clips, max(1, n // 20 or 1), n)
        if k == 1:
            scene_indices = [int(scenes[0].get("index", 0))]
        else:
            scene_indices = sorted(
                {
                    int(scenes[round(i * (n - 1) / (k - 1))].get("index", 0))
                    for i in range(k)
                }
            )
    rows: list[dict[str, Any]] = []
    for idx in scene_indices[:max_clips]:
        plan = plan_from_script(script, scene_index=idx, duration_s=duration_s)
        clip = out_dir / f"scene_{int(idx):03d}_clip.mp4"
        generate_map_clip(plan, out_path=clip)
        rows.append(
            {
                "scene_index": int(idx),
                "path": str(clip.resolve()),
                "kind": "animated_map",
                "date_stamp": plan.date_stamp,
                "waypoints": [asdict(w) for w in plan.waypoints],
            }
        )
    manifest = out_dir / "map_motion_manifest.json"
    manifest.write_text(
        json.dumps({"clips": rows, "engine": "pillow_ffmpeg"}, indent=2) + "\n",
        encoding="utf-8",
    )
    return rows


def ffmpeg_available(bin_name: str = "ffmpeg") -> bool:
    return shutil.which(bin_name) is not None
