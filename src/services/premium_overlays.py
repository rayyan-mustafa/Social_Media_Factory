"""$0 premium documentary overlays (PIL + FFmpeg chrome).

Builds on code infographic cards: chapter plates, map arrows, quote/letter,
History-vs-What-If splits, cold-open title, year stamps, progress rail,
lower-thirds, fog drift, pulse markers, soft vignette/letterbox.

Channel tone:
- napstorian: sharper plates, What-If split OK, stronger Ken Burns
- napping_historian: calmer fades, slower zoom, fewer slam cards
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from src.services.visual_modalities import (
    InfographicOverlay,
    _chapter_rows,
    _first_scene_per_chapter,
    _short_teaching_line,
    _teaching_lines_for_chapter,
    _YEAR_RE,
    plan_infographic_overlays,
    profile_enabled,
    render_infographic_card,
)

ChannelTone = Literal["napstorian", "napping_historian"]

_ENTITY_CUES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\bAnne Boleyn\b", re.I), "Anne Boleyn", "Queen Consort"),
    (re.compile(r"\bHenry\s+(?:VIII|the Eighth)\b", re.I), "Henry VIII", "King of England"),
    (re.compile(r"\bMary Tudor\b", re.I), "Mary Tudor", "Princess of England"),
    (re.compile(r"\bCatherine of Aragon\b|\bKatherine of Aragon\b", re.I), "Catherine of Aragon", "Queen of England"),
    (re.compile(r"\bJames\s+(?:V|the Fifth)\b", re.I), "James V", "King of Scots"),
    (re.compile(r"\bElizabeth\s+I\b|\bElizabeth Tudor\b", re.I), "Elizabeth I", "Queen of England"),
    (re.compile(r"\bThomas Cromwell\b", re.I), "Thomas Cromwell", "Chief Minister"),
    (re.compile(r"\bThomas More\b", re.I), "Thomas More", "Lord Chancellor"),
    (re.compile(r"\bWolsey\b", re.I), "Cardinal Wolsey", "Lord Chancellor"),
    (re.compile(r"\bJulius Caesar\b", re.I), "Julius Caesar", "Dictator"),
    (re.compile(r"\bAugustus\b", re.I), "Augustus", "First Emperor"),
    (re.compile(r"\bNapoleon\b", re.I), "Napoleon", "Emperor of the French"),
]

_QUOTE_HINT = re.compile(
    r"\b(letter|sealed|cipher|parchment|wrote|whisper|said|declared|swore|oath)\b",
    re.I,
)
_MAP_HINT = re.compile(
    r"\b(map|border|fleet|march|siege|campaign|empire|continent|strait|river|vienna|rome)\b",
    re.I,
)


@dataclass
class ChannelMotionStyle:
    """Ken Burns + plate pacing knobs by channel."""

    zoom_end: float = 1.12
    zoom_end_phase_b: float = 1.16
    plate_overlay_s: float = 3.5
    cold_open_s: float = 2.2
    fade_soft: bool = False
    allow_what_if_split: bool = True
    max_slam_cards: int = 20
    fog_opacity: float = 0.30
    letterbox_on_plates: bool = True


def channel_motion_style(channel: str | None) -> ChannelMotionStyle:
    ch = (channel or "napstorian").strip().lower()
    if ch == "napping_historian":
        return ChannelMotionStyle(
            zoom_end=1.06,
            zoom_end_phase_b=1.08,
            plate_overlay_s=4.0,
            cold_open_s=2.2,
            fade_soft=True,
            allow_what_if_split=False,
            max_slam_cards=8,
            fog_opacity=0.22,
            letterbox_on_plates=True,
        )
    return ChannelMotionStyle()


def resolve_channel(script: dict[str, Any] | None, *, fallback: str = "napstorian") -> str:
    if not script:
        return fallback
    meta = script.get("meta") if isinstance(script.get("meta"), dict) else {}
    for key in ("channel", "sheet_channel", "sheet_tab"):
        v = str(meta.get(key) or "").strip().lower()
        if v in ("napstorian", "napping_historian"):
            return v
    title = str(script.get("title") or script.get("topic") or "").lower()
    # Sleep epics rarely use What-If in title; default napstorian for What-If farm.
    if "what if" not in title and "sleep" in title:
        return "napping_historian"
    return fallback


@dataclass
class PremiumSceneChrome:
    """Per-scene chrome burned after Ken Burns (no Flux reburn)."""

    scene_index: int
    year_stamp: str | None = None
    progress: float = 0.0  # 0..1
    chapter_id: int | None = None
    lower_third_name: str | None = None
    lower_third_role: str | None = None
    fog: bool = False
    pulse: bool = False
    card_path: str | None = None
    card_style: str | None = None
    card_overlay_s: float = 3.5
    vignette: bool = True
    letterbox: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PremiumOverlayPlan:
    """Full premium pack plan for one compose."""

    channel: str
    pack: str = "premium_overlays_v1"
    cold_open_path: str | None = None
    cold_open_s: float = 2.0
    fog_png: str | None = None
    motion: dict[str, Any] = field(default_factory=dict)
    scene_chrome: list[PremiumSceneChrome] = field(default_factory=list)
    infographic_overlays: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "pack": self.pack,
            "cold_open_path": self.cold_open_path,
            "cold_open_s": self.cold_open_s,
            "fog_png": self.fog_png,
            "motion": self.motion,
            "scene_chrome": [c.to_dict() for c in self.scene_chrome],
            "infographic_overlays": self.infographic_overlays,
        }

    def chrome_by_scene(self) -> dict[int, PremiumSceneChrome]:
        return {int(c.scene_index): c for c in self.scene_chrome}


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Pillow required for premium overlays") from exc
    return Image, ImageDraw, ImageFont


def _fonts(sizes: tuple[int, ...]):
    Image, ImageDraw, ImageFont = _pil()
    out = []
    for sz in sizes:
        try:
            out.append(
                ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                    if sz >= 28
                    else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                    sz,
                )
            )
        except OSError:
            out.append(ImageFont.load_default())
    return out


def render_fog_layer(
    out_path: Path,
    *,
    width: int = 1280,
    height: int = 720,
    opacity: float = 0.28,
) -> Path:
    """Soft translucent cloud band for FFmpeg drift overlay."""
    Image, ImageDraw, _ImageFont = _pil()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    alpha = max(8, min(90, int(255 * opacity)))
    # Wide soft ellipses — cheap “fog” without external assets.
    blobs = [
        (-200, 80, 500, 280),
        (200, 40, 900, 260),
        (700, 100, 1400, 320),
        (-100, 400, 600, 620),
        (400, 380, 1100, 640),
        (900, 420, 1500, 700),
    ]
    for box in blobs:
        draw.ellipse(box, fill=(220, 225, 235, alpha))
    # Second pass lighter wisps
    for box in blobs:
        inset = (box[0] + 40, box[1] + 30, box[2] - 40, box[3] - 30)
        draw.ellipse(inset, fill=(245, 248, 255, max(4, alpha // 2)))
    img.save(out_path, format="PNG")
    return out_path


def render_map_card(
    title: str,
    labels: list[str],
    *,
    out_path: Path,
    width: int = 1280,
    height: int = 720,
    channel: str = "napstorian",
) -> Path:
    """Map-style plate with labels + simple direction arrows."""
    Image, ImageDraw, ImageFont = _pil()
    tone = channel_motion_style(channel)
    accent = (196, 160, 88) if not tone.fade_soft else (168, 148, 110)
    img = Image.new("RGB", (width, height), (12, 16, 22))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / max(height - 1, 1)
        draw.line(
            [(0, y), (width, y)],
            fill=(int(12 + 18 * t), int(16 + 8 * (1 - t)), int(22 + 20 * t)),
        )
    # Faux map frame
    draw.rectangle([40, 40, width - 40, height - 40], outline=accent, width=2)
    draw.rectangle([40, 40, width - 40, 120], fill=(24, 28, 36))
    font_k, font_t, font_b = _fonts((20, 36, 26))
    draw.text((64, 52), "MAP", fill=accent, font=font_k)
    draw.text((64, 78), (title or "Campaign")[:64], fill=(235, 220, 180), font=font_t)

    # Node positions across a faux arc
    nodes = labels[:5] or ["North", "Center", "South"]
    coords: list[tuple[int, int]] = []
    for i, _lab in enumerate(nodes):
        x = 160 + int((width - 320) * (i / max(len(nodes) - 1, 1)))
        y = 280 + int(80 * math.sin(i * 0.9))
        coords.append((x, y))
        r = 14
        draw.ellipse([x - r, y - r, x + r, y + r], outline=accent, width=3)
        draw.ellipse([x - 4, y - 4, x + 4, y + 4], fill=accent)
        draw.text((x - 40, y + 22), _short_teaching_line(_lab, limit=28), fill=(210, 210, 215), font=font_b)

    # Arrows between nodes
    for i in range(len(coords) - 1):
        x0, y0 = coords[i]
        x1, y1 = coords[i + 1]
        draw.line([(x0 + 16, y0), (x1 - 16, y1)], fill=accent, width=3)
        # Arrow head
        ang = math.atan2(y1 - y0, x1 - x0)
        ah = 16
        p1 = (x1 - 16 - ah * math.cos(ang - 0.4), y1 - ah * math.sin(ang - 0.4))
        p2 = (x1 - 16 - ah * math.cos(ang + 0.4), y1 - ah * math.sin(ang + 0.4))
        draw.polygon([(x1 - 14, y1), p1, p2], fill=accent)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG", optimize=True)
    return out_path


def render_quote_card(
    quote: str,
    *,
    out_path: Path,
    kicker: str = "LETTER",
    width: int = 1280,
    height: int = 720,
    channel: str = "napstorian",
) -> Path:
    Image, ImageDraw, ImageFont = _pil()
    tone = channel_motion_style(channel)
    accent = (196, 160, 88) if not tone.fade_soft else (168, 148, 110)
    img = Image.new("RGB", (width, height), (18, 16, 14))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / max(height - 1, 1)
        draw.line([(0, y), (width, y)], fill=(int(18 + 10 * t), int(16 + 6 * t), int(14 + 4 * t)))
    draw.rectangle([80, 100, width - 80, height - 100], outline=accent, width=2)
    font_k, font_q = _fonts((22, 38))
    draw.text((110, 130), kicker, fill=accent, font=font_k)
    # Wrap quote
    words = (quote or "").split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        if len(trial) > 42:
            if cur:
                lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    y = 220
    for line in lines[:5]:
        draw.text((110, y), f"“{line}”" if y == 220 else line, fill=(235, 225, 200), font=font_q)
        y += 56
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG", optimize=True)
    return out_path


def render_history_vs_what_if(
    history_line: str,
    what_if_line: str,
    *,
    out_path: Path,
    width: int = 1280,
    height: int = 720,
) -> Path:
    Image, ImageDraw, ImageFont = _pil()
    img = Image.new("RGB", (width, height), (14, 18, 24))
    draw = ImageDraw.Draw(img)
    mid = width // 2
    draw.rectangle([0, 0, mid - 2, height], fill=(28, 32, 40))
    draw.rectangle([mid + 2, 0, width, height], fill=(40, 28, 22))
    draw.line([(mid, 40), (mid, height - 40)], fill=(196, 160, 88), width=3)
    font_k, font_b = _fonts((24, 32))
    draw.text((48, 60), "HISTORY", fill=(160, 170, 190), font=font_k)
    draw.text((mid + 48, 60), "WHAT IF", fill=(196, 160, 88), font=font_k)
    draw.text((48, 160), _short_teaching_line(history_line, limit=36), fill=(220, 220, 225), font=font_b)
    draw.text((mid + 48, 160), _short_teaching_line(what_if_line, limit=36), fill=(235, 220, 180), font=font_b)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG", optimize=True)
    return out_path


def render_cold_open_title(
    title: str,
    *,
    out_path: Path,
    subtitle: str = "",
    width: int = 1280,
    height: int = 720,
    channel: str = "napstorian",
) -> Path:
    Image, ImageDraw, ImageFont = _pil()
    tone = channel_motion_style(channel)
    accent = (196, 160, 88) if not tone.fade_soft else (168, 148, 110)
    img = Image.new("RGB", (width, height), (8, 10, 14))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / max(height - 1, 1)
        draw.line([(0, y), (width, y)], fill=(int(8 + 20 * t), int(10 + 8 * t), int(14 + 18 * t)))
    # Soft letterbox bars
    bar = 48 if tone.fade_soft else 36
    draw.rectangle([0, 0, width, bar], fill=(0, 0, 0))
    draw.rectangle([0, height - bar, width, height], fill=(0, 0, 0))
    font_k, font_t, font_s = _fonts((20, 44, 24))
    kicker = "A WHAT-IF DOCUMENTARY" if not tone.fade_soft else "A QUIET HISTORY"
    draw.text((80, height // 2 - 90), kicker, fill=accent, font=font_k)
    # Wrap title
    words = (title or "Untitled").split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        if len(trial) > 34:
            if cur:
                lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    y = height // 2 - 40
    for line in lines[:3]:
        draw.text((80, y), line, fill=(240, 230, 200), font=font_t)
        y += 56
    if subtitle:
        draw.text((80, y + 12), _short_teaching_line(subtitle, limit=60), fill=(180, 180, 190), font=font_s)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG", optimize=True)
    return out_path


def render_lower_third_png(
    name: str,
    role: str,
    *,
    out_path: Path,
    width: int = 1280,
    height: int = 720,
    channel: str = "napstorian",
) -> Path:
    """Transparent lower-third bar (RGBA)."""
    Image, ImageDraw, ImageFont = _pil()
    tone = channel_motion_style(channel)
    accent = (196, 160, 88) if not tone.fade_soft else (168, 148, 110)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bar_h = 78
    y0 = height - 140
    draw.rectangle([60, y0, 620, y0 + bar_h], fill=(12, 14, 18, 200))
    draw.rectangle([60, y0, 68, y0 + bar_h], fill=(*accent, 255))
    font_n, font_r = _fonts((28, 20))
    draw.text((84, y0 + 12), (name or "")[:40], fill=(240, 230, 200, 255), font=font_n)
    draw.text((84, y0 + 46), (role or "")[:48], fill=(*accent, 255), font=font_r)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")
    return out_path


def render_pulse_marker_png(
    out_path: Path,
    *,
    width: int = 1280,
    height: int = 720,
    x: int = 640,
    y: int = 360,
) -> Path:
    """Static pulse ring (FFmpeg opacity pulse applied at burn time)."""
    Image, ImageDraw, _F = _pil()
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for r, a in ((28, 40), (18, 90), (8, 200)):
        draw.ellipse([x - r, y - r, x + r, y + r], outline=(196, 160, 88, a), width=3)
    draw.ellipse([x - 4, y - 4, x + 4, y + 4], fill=(196, 160, 88, 230))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")
    return out_path


def extract_entity_cue(text: str) -> tuple[str, str] | None:
    t = text or ""
    for pat, name, role in _ENTITY_CUES:
        if pat.search(t):
            return name, role
    return None


def chapter_year(ch: dict[str, Any] | None, scenes: list[dict[str, Any]] | None = None) -> str | None:
    blob = ""
    if isinstance(ch, dict):
        blob = " ".join(
            [
                str(ch.get("title") or ""),
                str(ch.get("goal") or ""),
                " ".join(str(p) for p in (ch.get("key_points") or [])[:4]),
            ]
        )
    if scenes:
        for sc in scenes[:3]:
            blob += " " + str((sc or {}).get("text") or "")
    m = _YEAR_RE.search(blob)
    return m.group(1) if m else None


def _pick_quote_line(script: dict[str, Any] | None) -> tuple[int, str] | None:
    if not script:
        return None
    for sc in script.get("scenes") or []:
        if not isinstance(sc, dict):
            continue
        text = str(sc.get("text") or "").strip()
        if not text or not _QUOTE_HINT.search(text):
            continue
        if len(text) < 24:
            continue
        return int(sc.get("index", 0)), _short_teaching_line(text, limit=78)
    # Fallback: hook
    hook = str(script.get("hook") or "").strip()
    if hook:
        scenes = script.get("scenes") or []
        idx = int(scenes[0].get("index", 0)) if scenes and isinstance(scenes[0], dict) else 0
        return idx, _short_teaching_line(hook, limit=78)
    return None


def plan_premium_overlays(
    script: dict[str, Any] | None,
    *,
    out_dir: Path,
    channel: str | None = None,
    min_cards: int = 8,
    max_cards: int = 20,
    overlay_s: float | None = None,
    fog_enabled: bool = True,
    vignette_enabled: bool = True,
    letterbox_enabled: bool = True,
    year_stamp_enabled: bool = True,
    progress_rail_enabled: bool = True,
    lower_thirds_enabled: bool = True,
    cold_open_enabled: bool = True,
    pulse_enabled: bool = True,
    scene_indices: list[int] | None = None,
    target_interval_s: float = 75.0,
    estimated_duration_s: float | None = None,
) -> PremiumOverlayPlan:
    """Plan premium pack assets + per-scene chrome. Default-on for farm compose."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ch = resolve_channel(script, fallback=channel or "napstorian")
    tone = channel_motion_style(ch)
    ov_s = float(overlay_s if overlay_s is not None else tone.plate_overlay_s)
    max_cards = min(int(max_cards), int(tone.max_slam_cards))

    plan = PremiumOverlayPlan(
        channel=ch,
        cold_open_s=float(tone.cold_open_s),
        motion={
            "zoom_end": tone.zoom_end,
            "zoom_end_phase_b": tone.zoom_end_phase_b,
            "fade_soft": tone.fade_soft,
            "plate_overlay_s": ov_s,
            "fog_opacity": tone.fog_opacity,
            "target_interval_s": float(target_interval_s),
        },
    )

    if not script:
        return plan

    # Fog asset (shared)
    if fog_enabled:
        fog_path = out_dir / "fog_drift.png"
        render_fog_layer(fog_path, opacity=tone.fog_opacity)
        plan.fog_png = str(fog_path.resolve())

    # Cold-open title
    if cold_open_enabled:
        title = str(script.get("title") or script.get("topic") or "Documentary")
        hook = str(script.get("hook") or "")
        cold = out_dir / "cold_open_title.png"
        render_cold_open_title(
            title, out_path=cold, subtitle=hook, channel=ch
        )
        plan.cold_open_path = str(cold.resolve())

    wanted: set[int] | None = (
        {int(i) for i in scene_indices} if scene_indices is not None else None
    )

    # Base infographic chapter cards (selected pack) — Netflix density
    if profile_enabled("infographic"):
        base = plan_infographic_overlays(
            script,
            out_dir=out_dir / "cards",
            min_cards=min_cards,
            max_cards=max_cards,
            overlay_s=ov_s,
            target_interval_s=float(target_interval_s),
            estimated_duration_s=estimated_duration_s,
            scene_indices=list(wanted) if wanted is not None else None,
        )
    else:
        base = []

    # Upgrade map-style cards with arrows; inject quote + what-if split
    first_scene = _first_scene_per_chapter(script)
    if wanted is not None:
        remapped: dict[int, int] = {}
        for s in script.get("scenes") or []:
            if not isinstance(s, dict):
                continue
            try:
                idx = int(s.get("index", -1))
                cid = int(s.get("chapter_id") or 0)
            except (TypeError, ValueError):
                continue
            if idx not in wanted or cid <= 0:
                continue
            if cid not in remapped or idx < remapped[cid]:
                remapped[cid] = idx
        if remapped:
            first_scene = remapped
    chapters = {int(c["id"]): c for c in _chapter_rows(script)}
    card_by_scene: dict[int, InfographicOverlay] = {}

    for i, ov in enumerate(base):
        if wanted is not None and int(ov.scene_index) not in wanted:
            continue
        style = ov.style
        raw = chapters.get(int(ov.chapter_id or 0), {}).get("raw") or {
            "title": ov.title,
            "key_points": ov.lines,
        }
        # Detect map beats
        blob = " ".join(
            [ov.title]
            + list(ov.lines)
            + [str(p) for p in (raw.get("key_points") or [])[:4]]
        )
        if _MAP_HINT.search(blob):
            style = "map"
            path = Path(ov.card_path)
            labels = [_short_teaching_line(x, limit=28) for x in ov.lines[:5]]
            render_map_card(ov.title, labels, out_path=path, channel=ch)
            ov = InfographicOverlay(
                scene_index=ov.scene_index,
                chapter_id=ov.chapter_id,
                title=ov.title,
                lines=ov.lines,
                style="map",
                card_path=str(path.resolve()),
                overlay_s=ov.overlay_s,
            )
        card_by_scene[int(ov.scene_index)] = ov

    # Quote / letter card
    q = _pick_quote_line(script)
    if q is not None:
        q_idx, q_text = q
        if wanted is not None and int(q_idx) not in wanted:
            # Snap quote plate to earliest in-cut scene when truncated.
            q_idx = min(wanted) if wanted else q_idx
        if (wanted is None or int(q_idx) in wanted) and (
            q_idx not in card_by_scene or len(card_by_scene) < max_cards
        ):
            q_path = out_dir / f"quote_s{q_idx:03d}.png"
            render_quote_card(q_text, out_path=q_path, channel=ch)
            card_by_scene[q_idx] = InfographicOverlay(
                scene_index=q_idx,
                chapter_id=None,
                title="Letter",
                lines=[q_text],
                style="quote",
                card_path=str(q_path.resolve()),
                overlay_s=ov_s,
            )

    # History vs What If (napstorian / what_if titles)
    title_l = str(script.get("title") or "").lower()
    if tone.allow_what_if_split and ("what if" in title_l or "what_if" in title_l):
        # Place on divergence-ish chapter if present
        split_idx = None
        for cid, crow in chapters.items():
            t = str(crow.get("title") or "").lower()
            if "diverg" in t or "what if" in t or "fork" in t or "aftermath" in t:
                split_idx = first_scene.get(cid)
                break
        if split_idx is None:
            scenes = [
                s
                for s in (script.get("scenes") or [])
                if isinstance(s, dict)
                and (wanted is None or int(s.get("index", -1)) in wanted)
            ]
            if scenes:
                split_idx = int(scenes[min(len(scenes) - 1, len(scenes) // 3)].get("index", 0))
        if split_idx is not None and (wanted is None or int(split_idx) in wanted):
            hist = "The record as written"
            wif = _short_teaching_line(str(script.get("title") or "One choice rewrites fate"), limit=40)
            # Prefer chapter goals
            for crow in chapters.values():
                raw = crow.get("raw") if isinstance(crow.get("raw"), dict) else {}
                goal = str((raw or {}).get("goal") or crow.get("title") or "")
                if goal and "hook" not in goal.lower():
                    hist = _short_teaching_line(goal, limit=40)
                    break
            # Prefer upgrading an existing what_if plate; otherwise overwrite divergence beat.
            upgrade_idx = int(split_idx)
            for sc_i, ov in card_by_scene.items():
                if ov.style == "what_if":
                    upgrade_idx = int(sc_i)
                    break
            sp = out_dir / f"split_s{upgrade_idx:03d}.png"
            render_history_vs_what_if(hist, wif, out_path=sp)
            card_by_scene[upgrade_idx] = InfographicOverlay(
                scene_index=upgrade_idx,
                chapter_id=None,
                title="History vs What If",
                lines=[hist, wif],
                style="history_vs_what_if",
                card_path=str(sp.resolve()),
                overlay_s=ov_s,
            )

    # Drop any plates outside the compose cut, then cap.
    if wanted is not None:
        card_by_scene = {
            idx: ov for idx, ov in card_by_scene.items() if int(idx) in wanted
        }
    if len(card_by_scene) > max_cards:
        kept = sorted(card_by_scene.items(), key=lambda kv: kv[0])[:max_cards]
        card_by_scene = dict(kept)

    plan.infographic_overlays = [ov.to_dict() for ov in card_by_scene.values()]

    # Per-scene chrome
    scenes = [s for s in (script.get("scenes") or []) if isinstance(s, dict)]
    if scene_indices is not None:
        wanted = set(int(i) for i in scene_indices)
        scenes = [s for s in scenes if int(s.get("index", -1)) in wanted]
    n = max(len(scenes), 1)
    chapter_years: dict[int, str | None] = {}
    for cid, crow in chapters.items():
        raw = crow.get("raw") if isinstance(crow.get("raw"), dict) else {
            "title": crow.get("title"),
            "goal": "",
            "key_points": [],
        }
        ch_scenes = [s for s in scenes if int(s.get("chapter_id") or 0) == cid]
        chapter_years[cid] = chapter_year(raw, ch_scenes) if year_stamp_enabled else None

    used_entities: set[str] = set()
    lower_budget = 8 if not tone.fade_soft else 4
    pulse_budget = 5 if pulse_enabled else 0
    chapter_first_done: set[int] = set()

    for pos, sc in enumerate(scenes):
        idx = int(sc.get("index", pos))
        cid = int(sc.get("chapter_id") or 0) or None
        text = str(sc.get("text") or "")
        vis = str(sc.get("visual_prompt") or "")
        progress = (pos + 1) / n if progress_rail_enabled else 0.0
        year = chapter_years.get(cid or -1) if year_stamp_enabled else None
        if year is None and year_stamp_enabled:
            m = _YEAR_RE.search(text)
            year = m.group(1) if m else None

        fog = bool(
            fog_enabled
            and (
                (idx in card_by_scene and card_by_scene[idx].style in {"map", "timeline"})
                or bool(_MAP_HINT.search(text + " " + vis))
                or (idx in card_by_scene)
            )
        )

        lt_name = lt_role = None
        if lower_thirds_enabled and len(used_entities) < lower_budget:
            ent = extract_entity_cue(text)
            if ent and ent[0] not in used_entities:
                lt_name, lt_role = ent
                used_entities.add(ent[0])
                # Materialize lower-third PNG next to plan (composer may also drawtext)
                lt_path = out_dir / f"lower_third_s{idx:03d}.png"
                render_lower_third_png(lt_name, lt_role, out_path=lt_path, channel=ch)

        pulse = False
        if pulse_budget > 0 and fog and "march" in (text + vis).lower():
            pulse = True
            pulse_budget -= 1
        elif pulse_budget > 0 and idx in card_by_scene and card_by_scene[idx].style in {
            "map",
            "history_vs_what_if",
            "interrupt",
        }:
            pulse = True
            pulse_budget -= 1

        is_chapter_first = bool(cid and cid not in chapter_first_done)
        if is_chapter_first and cid:
            chapter_first_done.add(cid)
        # Netflix-doc chrome: progress rail ~every 4 beats + chapter/plate anchors.
        has_card = idx in card_by_scene
        progress_tick = (pos % 4 == 0) or is_chapter_first or pos == 0 or pos == n - 1
        apply = bool(
            has_card
            or fog
            or pulse
            or lt_name
            or (year and is_chapter_first)
            or progress_tick
            or pos == 0  # cold-open host scene
        )
        if not apply:
            continue

        card = card_by_scene.get(idx)
        use_letterbox = bool(
            letterbox_enabled
            and (
                (tone.letterbox_on_plates and (has_card or is_chapter_first or pos == 0))
                or (tone.fade_soft and (pos == 0 or is_chapter_first))
            )
        )
        chrome = PremiumSceneChrome(
            scene_index=idx,
            year_stamp=year if (is_chapter_first or has_card) else None,
            progress=float(progress) if progress_tick else 0.0,
            chapter_id=cid,
            lower_third_name=lt_name,
            lower_third_role=lt_role,
            fog=fog,
            pulse=pulse,
            card_path=card.card_path if card else None,
            card_style=card.style if card else None,
            card_overlay_s=float(card.overlay_s) if card else ov_s,
            vignette=bool(vignette_enabled),
            letterbox=use_letterbox,
        )
        plan.scene_chrome.append(chrome)

    # Persist plan for ops / Live detection
    (out_dir / "premium_plan.json").write_text(
        json.dumps(plan.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return plan
