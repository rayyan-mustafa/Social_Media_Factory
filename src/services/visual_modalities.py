"""Visual modality ladder helpers (retention / video correction).

Flux cinematic remains the farm stills default. This module loads
``config/visual_modalities.json`` and provides:

- profile / prompt-suffix lookup for optional toon / cinematic packs
- free code-path infographic card rendering (PIL) for compose overlays
- chapter-title → 3–6 pattern-interrupt overlay plans for EditModule
- path helpers for Adobe manual exports (human-only; no cookie scrape)

No Meta/Adobe session scraping. No paid Firefly API.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "visual_modalities.json"
ADOBE_ASSETS = ROOT / "assets" / "adobe_manual"
INFOGRAPHIC_OUT = ROOT / "output" / "ops" / "infographic_samples"

_YEAR_RE = re.compile(r"\b((?:1[0-9]|20)\d{2})\b")


def load_modalities_config(path: Path | None = None) -> dict[str, Any]:
    p = path or CONFIG_PATH
    if not p.is_file():
        return {
            "default_still_backend": "flux_runpod",
            "ladder": [],
            "profiles": {},
            "reject": [],
        }
    return json.loads(p.read_text(encoding="utf-8"))


def list_reject_paths(cfg: dict[str, Any] | None = None) -> list[str]:
    cfg = cfg or load_modalities_config()
    return list(cfg.get("reject") or [])


def profile_enabled(name: str, cfg: dict[str, Any] | None = None) -> bool:
    cfg = cfg or load_modalities_config()
    prof = (cfg.get("profiles") or {}).get(name) or {}
    return bool(prof.get("enabled"))


def prompt_suffix(name: str, cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or load_modalities_config()
    prof = (cfg.get("profiles") or {}).get(name) or {}
    return str(prof.get("prompt_suffix") or "").strip()


def adobe_manual_dir(cfg: dict[str, Any] | None = None) -> Path:
    cfg = cfg or load_modalities_config()
    prof = (cfg.get("profiles") or {}).get("adobe_manual") or {}
    rel = str(prof.get("assets_dir") or "assets/adobe_manual")
    p = Path(rel)
    if not p.is_absolute():
        p = ROOT / p
    return p


def list_adobe_manual_assets(cfg: dict[str, Any] | None = None) -> list[Path]:
    d = adobe_manual_dir(cfg)
    if not d.is_dir():
        return []
    out: list[Path] = []
    for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
        out.extend(sorted(d.glob(ext)))
    return out


def render_infographic_card(
    title: str,
    lines: list[str],
    *,
    out_path: Path | None = None,
    width: int = 1280,
    height: int = 720,
    style: str = "timeline",
) -> Path:
    """Render a simple dark history infographic card with Pillow (free, code-first)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Pillow required for infographic cards") from exc

    INFOGRAPHIC_OUT.mkdir(parents=True, exist_ok=True)
    dest = out_path or (INFOGRAPHIC_OUT / "card.png")
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Atmosphere: deep slate + warm parchment accent (not flat single fill).
    img = Image.new("RGB", (width, height), (14, 18, 24))
    draw = ImageDraw.Draw(img)
    for y in range(0, height):
        t = y / max(height - 1, 1)
        r = int(14 + 22 * t)
        g = int(18 + 10 * (1 - t))
        b = int(24 + 28 * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    # Left accent rail (map / timeline cue).
    accent = (196, 160, 88) if style != "what_if" else (168, 112, 72)
    draw.rectangle([0, 0, 18, height], fill=accent)
    draw.rectangle([48, 40, width - 48, height - 40], outline=accent, width=2)

    # Soft header band
    draw.rectangle([48, 40, width - 48, 140], fill=(28, 32, 40))
    draw.line([(72, 148), (width - 72, 148)], fill=accent, width=2)

    try:
        font_kicker = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22
        )
        font_title = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 40
        )
        font_body = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28
        )
    except OSError:
        font_kicker = ImageFont.load_default()
        font_title = ImageFont.load_default()
        font_body = ImageFont.load_default()

    kicker = {
        "timeline": "TIMELINE",
        "map": "MAP BEAT",
        "what_if": "WHAT IF",
        "interrupt": "PATTERN INTERRUPT",
    }.get(style, "CHAPTER")
    draw.text((72, 56), kicker, fill=accent, font=font_kicker)
    draw.text((72, 88), (title or "Chapter")[:72], fill=(235, 220, 180), font=font_title)

    y = 180
    for i, line in enumerate(lines[:6]):
        bullet = "▸ " if style != "map" else "• "
        # Map-style: faux region row
        if style == "map" and i < 4:
            draw.rectangle(
                [72, y - 4, 72 + 28, y + 28],
                outline=accent,
                width=1,
            )
        draw.text(
            (112 if style == "map" else 72, y),
            f"{bullet}{line[:86]}",
            fill=(210, 210, 215),
            font=font_body,
        )
        y += 52

    # Footer timeline ticks
    base_y = height - 72
    draw.line([(96, base_y), (width - 96, base_y)], fill=(90, 96, 110), width=2)
    for i in range(5):
        x = 96 + int((width - 192) * (i / 4))
        draw.line([(x, base_y - 10), (x, base_y + 10)], fill=accent, width=2)

    img.save(dest, format="PNG", optimize=True)
    return dest


@dataclass
class InfographicOverlay:
    """One pattern-interrupt card burned onto a scene clip."""

    scene_index: int
    chapter_id: int | None
    title: str
    lines: list[str]
    style: str
    card_path: str
    overlay_s: float = 3.5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _short_teaching_line(text: str, *, limit: int = 72) -> str:
    t = " ".join(str(text or "").split())
    if not t:
        return ""
    # Drop "Show the…" / "Establish…" lecture openers — cards teach, not narrate.
    for prefix in (
        "show the ",
        "establish the ",
        "detail the ",
        "broaden to ",
        "fast-forward ",
        "address historical ",
        "pose a complex ",
        "a detailed segment ",
    ):
        if t.lower().startswith(prefix):
            t = t[len(prefix) :].lstrip()
            t = t[:1].upper() + t[1:] if t else t
            break
    if len(t) <= limit:
        return t
    cut = t[: limit - 1].rsplit(" ", 1)[0]
    return (cut or t[: limit - 1]).rstrip(",.;:") + "…"


def _teaching_lines_for_chapter(ch: dict[str, Any], *, style: str) -> list[str]:
    """Build 3–5 short teachable bullets (forks / timeline / map), not prose spam."""
    title = str(ch.get("title") or "").strip()
    pts = [str(p).strip() for p in (ch.get("key_points") or ch.get("beats") or []) if str(p).strip()]
    goal = str(ch.get("goal") or "").strip()
    lines: list[str] = []

    if style == "what_if" and title:
        lines.append(_short_teaching_line(f"Fork: {title}", limit=70))
    elif style == "timeline":
        ym = _YEAR_RE.search(title + " " + goal + " " + " ".join(pts[:2]))
        if ym:
            lines.append(_short_teaching_line(f"{ym.group(1)} — the hinge year", limit=70))
    elif style == "map":
        lines.append(_short_teaching_line("Where power moves on the map", limit=70))

    for p in pts:
        s = _short_teaching_line(p)
        if s and s not in lines:
            lines.append(s)
        if len(lines) >= 5:
            break

    if len(lines) < 3 and goal:
        g = _short_teaching_line(goal, limit=70)
        if g and g not in lines:
            lines.append(g)

    while len(lines) < 3:
        filler = {
            "what_if": "One choice rewrites the next decade",
            "timeline": "Cause → shock → new normal",
            "map": "Borders, fleets, and thrones shift",
            "interrupt": "Stay for the next turn",
        }.get(style, "Follow the thread")
        if filler not in lines:
            lines.append(filler)
        else:
            break
    return lines[:5]


def _chapter_rows(script: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not script:
        return []
    outline = script.get("outline") or {}
    chapters = outline.get("chapters") if isinstance(outline, dict) else None
    if not chapters:
        chapters = script.get("chapters") or []
    rows: list[dict[str, Any]] = []
    for i, ch in enumerate(chapters):
        if not isinstance(ch, dict):
            continue
        title = str(ch.get("title") or ch.get("name") or ch.get("heading") or "").strip()
        if not title:
            continue
        cid = ch.get("id")
        try:
            cid_i = int(cid) if cid is not None else i + 1
        except (TypeError, ValueError):
            cid_i = i + 1
        rows.append(
            {
                "id": cid_i,
                "title": title,
                "raw": ch,
            }
        )
    return rows


def _first_scene_per_chapter(script: dict[str, Any] | None) -> dict[int, int]:
    """Map chapter_id → first scene index."""
    out: dict[int, int] = {}
    if not script:
        return out
    for sc in script.get("scenes") or []:
        if not isinstance(sc, dict):
            continue
        try:
            idx = int(sc.get("index"))
            cid = int(sc.get("chapter_id") or 0)
        except (TypeError, ValueError):
            continue
        if cid and cid not in out:
            out[cid] = idx
    return out


def _pick_styles(n: int) -> list[str]:
    cycle = ["interrupt", "timeline", "what_if", "map", "timeline", "interrupt"]
    return [cycle[i % len(cycle)] for i in range(n)]


def plan_infographic_overlays(
    script: dict[str, Any] | None,
    *,
    out_dir: Path,
    min_cards: int = 8,
    max_cards: int = 20,
    overlay_s: float = 3.5,
    skip_hook_chapter: bool = True,
    target_interval_s: float = 75.0,
    estimated_duration_s: float | None = None,
    scene_indices: list[int] | None = None,
) -> list[InfographicOverlay]:
    """Pick dense teaching cards and render PNGs into ``out_dir``.

    Netflix Turning Point pacing: aim for a graphic beat about every
    ``target_interval_s`` (default 75s). Skips the hook chapter by default;
    fills remaining budget with mid-chapter / evenly spaced scene cards.

    When ``scene_indices`` is set (truncated / Live-test composes), only those
    scene indexes receive cards — never plan plates past the available cut.
    """
    if not profile_enabled("infographic"):
        return []

    chapters = _chapter_rows(script)
    if not chapters:
        return []

    wanted: set[int] | None = (
        {int(i) for i in scene_indices} if scene_indices is not None else None
    )

    first_scene = _first_scene_per_chapter(script)
    if wanted is not None:
        # Remap chapter anchors to the earliest scene of that chapter still in-cut.
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
        first_scene = remapped or first_scene

    candidates = list(chapters)
    if skip_hook_chapter and candidates:
        # Drop first chapter if it looks like a hook
        t0 = candidates[0]["title"].lower()
        if "hook" in t0 or candidates[0]["id"] == 1:
            candidates = candidates[1:]

    if not candidates:
        candidates = chapters

    # Prefer chapters that still have an in-cut scene when truncating.
    if wanted is not None and first_scene:
        in_cut = [c for c in candidates if int(c["id"]) in first_scene]
        if in_cut:
            candidates = in_cut

    scenes = [s for s in (script.get("scenes") or []) if isinstance(s, dict)] if script else []
    if wanted is not None:
        scenes = [s for s in scenes if int(s.get("index", -1)) in wanted]
    n_scenes = max(len(scenes), 1)
    # Infer duration from scene count when not provided (~11s avg farm hold).
    dur = float(estimated_duration_s) if estimated_duration_s and estimated_duration_s > 0 else float(
        n_scenes * 11.0
    )
    interval = max(45.0, float(target_interval_s or 75.0))
    target_n = int(round(dur / interval)) if dur > 0 else min_cards
    n = max(int(min_cards), min(int(max_cards), max(target_n, len(candidates))))
    n = min(n, int(max_cards))

    # Prefer evenly spaced mid chapters for the chapter-start layer
    chapter_n = min(n, len(candidates))
    if len(candidates) <= chapter_n:
        chosen = list(candidates)
    else:
        step = (len(candidates) - 1) / max(chapter_n - 1, 1)
        idxs = sorted({int(round(i * step)) for i in range(chapter_n)})
        while len(idxs) < chapter_n:
            for j in range(len(candidates)):
                if j not in idxs:
                    idxs.append(j)
                if len(idxs) >= chapter_n:
                    break
        chosen = [candidates[i] for i in sorted(idxs)[:chapter_n]]

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    overlays: list[InfographicOverlay] = []
    used_scenes: set[int] = set()

    styles = _pick_styles(max(n, 1))
    for i, ch in enumerate(chosen):
        cid = int(ch["id"])
        scene_idx = first_scene.get(cid)
        if scene_idx is None or (wanted is not None and int(scene_idx) not in wanted):
            if scenes:
                scene_idx = int(
                    scenes[min(len(scenes) - 1, (i + 1) * len(scenes) // (chapter_n + 1))].get(
                        "index", i
                    )
                )
            else:
                scene_idx = i + 1
        if wanted is not None and int(scene_idx) not in wanted:
            continue
        style = styles[i % len(styles)]
        raw = ch.get("raw") if isinstance(ch.get("raw"), dict) else {
            "title": ch.get("title"),
            "key_points": ch.get("key_points") or [],
            "goal": ch.get("goal") or "",
        }
        lines = _teaching_lines_for_chapter(raw, style=style)
        card_path = out_dir / f"infographic_{i:02d}_ch{cid}_s{scene_idx:03d}.png"
        render_infographic_card(
            ch["title"],
            lines,
            out_path=card_path,
            style=style,
        )
        overlays.append(
            InfographicOverlay(
                scene_index=int(scene_idx),
                chapter_id=cid,
                title=ch["title"],
                lines=lines,
                style=style,
                card_path=str(card_path.resolve()),
                overlay_s=float(overlay_s),
            )
        )
        used_scenes.add(int(scene_idx))

    # Mid-chapter / evenly spaced density fillers (Netflix ~60–90s beats)
    need = n - len(overlays)
    if need > 0 and scenes:
        # Candidate mid scenes: not already used, prefer years / map / letter cues
        scored: list[tuple[int, int, dict[str, Any]]] = []
        for pos, sc in enumerate(scenes):
            try:
                idx = int(sc.get("index", pos))
            except (TypeError, ValueError):
                continue
            if idx in used_scenes or idx == 0:
                continue
            text = str(sc.get("text") or "")
            vis = str(sc.get("visual_prompt") or "")
            blob = f"{text} {vis}".lower()
            score = 0
            if _YEAR_RE.search(text):
                score += 3
            if any(k in blob for k in ("map", "border", "fleet", "scotland", "england", "letter", "seal", "court")):
                score += 2
            # Prefer mid-video spacing relative to already-used
            score += 1
            scored.append((score, idx, sc))
        scored.sort(key=lambda t: (-t[0], t[1]))

        # Also force evenly spaced picks across timeline for Netflix cadence
        even_idxs: list[int] = []
        if n_scenes > need:
            step = n_scenes / (need + 1)
            for k in range(1, need + 1):
                even_idxs.append(int(round(k * step)))
        even_scene_ids = []
        for ei in even_idxs:
            sc = scenes[min(ei, n_scenes - 1)]
            try:
                even_scene_ids.append(int(sc.get("index", ei)))
            except (TypeError, ValueError):
                continue

        pick_order: list[tuple[int, dict[str, Any]]] = []
        seen: set[int] = set(used_scenes)
        for sid in even_scene_ids:
            if sid in seen:
                continue
            sc = next((s for s in scenes if int(s.get("index", -1)) == sid), None)
            if sc:
                pick_order.append((sid, sc))
                seen.add(sid)
            if len(pick_order) >= need:
                break
        for _score, idx, sc in scored:
            if idx in seen:
                continue
            pick_order.append((idx, sc))
            seen.add(idx)
            if len(pick_order) >= need:
                break

        for j, (scene_idx, sc) in enumerate(pick_order[:need]):
            style = styles[(len(overlays) + j) % len(styles)]
            text = str(sc.get("text") or "").strip()
            title = _short_teaching_line(text, limit=48) or f"Beat {scene_idx}"
            ym = _YEAR_RE.search(text)
            if ym:
                title = f"{ym.group(1)} — {title}" if title else ym.group(1)
            lines = [
                _short_teaching_line(text, limit=70) or "Follow the fork",
                "Cause → shock → new normal",
                "Stay for the next turn",
            ]
            if style == "map":
                lines[1] = "Where power moves on the map"
            elif style == "what_if":
                lines[1] = "One choice rewrites the next decade"
            cid = None
            try:
                cid = int(sc.get("chapter_id")) if sc.get("chapter_id") is not None else None
            except (TypeError, ValueError):
                cid = None
            card_path = out_dir / (
                f"infographic_{len(overlays):02d}_mid_s{int(scene_idx):03d}.png"
            )
            render_infographic_card(title, lines, out_path=card_path, style=style)
            overlays.append(
                InfographicOverlay(
                    scene_index=int(scene_idx),
                    chapter_id=cid,
                    title=title,
                    lines=lines,
                    style=style,
                    card_path=str(card_path.resolve()),
                    overlay_s=float(overlay_s),
                )
            )
            used_scenes.add(int(scene_idx))

    return overlays[: int(max_cards)]


def load_script_dict(script_path: Path | None) -> dict[str, Any] | None:
    if script_path is None or not Path(script_path).is_file():
        return None
    try:
        data = json.loads(Path(script_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def modalities_status() -> dict[str, Any]:
    cfg = load_modalities_config()
    return {
        "ok": True,
        "config": str(CONFIG_PATH),
        "default_still_backend": cfg.get("default_still_backend"),
        "ladder": cfg.get("ladder") or [],
        "reject": list_reject_paths(cfg),
        "profiles": {
            name: {
                "enabled": bool((meta or {}).get("enabled")),
                "backend": (meta or {}).get("backend"),
            }
            for name, meta in (cfg.get("profiles") or {}).items()
        },
        "adobe_manual_assets": [str(p) for p in list_adobe_manual_assets(cfg)],
        "infographic_enabled": profile_enabled("infographic", cfg),
        "premium_overlays_v1_enabled": profile_enabled("premium_overlays_v1", cfg),
    }
