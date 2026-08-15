"""SMM thumbnail redesign — see competitor thumbs, update channel style + prompt guidance.

Fetches free YouTube thumbnail URLs (i.ytimg.com from competitor video_ids),
analyzes style (OpenRouter vision when available, else Pillow heuristics), then
patches ``thumbnail_style`` / ``thumb_prompt_addendum`` and the channel
``thumbnail_template.txt`` (after coding snapshot). Logs to
``output/ops/smm_thumb_redesign_log.jsonl`` + premium_editor_log.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.store import OPS_DIR
from src.services.editing_overrides import (
    thumbnail_style_preset,
    write_channel_overrides,
)
from src.services.settings import CONFIG_DIR

log = logging.getLogger(__name__)

REDESIGN_LOG = OPS_DIR / "smm_thumb_redesign_log.jsonl"
THUMB_CACHE = OPS_DIR / "competitor_thumbs"
CHANNELS = ("napstorian", "napping_historian")

_REDESIGN_MARKER_RE = re.compile(
    r"\n*# === SMM THUMB REDESIGN [^\n]*===\n.*?# === END SMM THUMB REDESIGN ===\n?",
    re.DOTALL,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def youtube_thumb_urls(video_id: str) -> list[str]:
    """Free CDN URLs — no paid API. Prefer maxres, fall back to hq/mq."""
    vid = (video_id or "").strip()
    if not vid:
        return []
    return [
        f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
        f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
    ]


def inspiration_path_for_channel(channel: str) -> Path:
    if channel == "napping_historian":
        return OPS_DIR / "last_inspiration_napping_historian.json"
    return OPS_DIR / "last_inspiration.json"


def collect_top_competitor_refs(
    channel: str,
    *,
    limit: int = 5,
    ops_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Top recent competitor videos with thumbnail URL candidates."""
    ops = ops_dir or OPS_DIR
    if channel == "napping_historian":
        insp_path = ops / "last_inspiration_napping_historian.json"
    else:
        insp_path = ops / "last_inspiration.json"
    if not insp_path.is_file():
        return []
    try:
        insp = json.loads(insp_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    items = [
        it
        for it in (insp.get("items") or [])
        if isinstance(it, dict)
        and it.get("kind") == "competitor_video"
        and it.get("video_id")
    ]
    items.sort(key=lambda x: float(x.get("video_views") or 0), reverse=True)
    out: list[dict[str, Any]] = []
    for it in items[: max(1, limit)]:
        vid = str(it.get("video_id"))
        urls = youtube_thumb_urls(vid)
        out.append(
            {
                "video_id": vid,
                "title": str(it.get("blocked_title") or it.get("title") or "")[:120],
                "channel_label": it.get("channel_label"),
                "video_views": it.get("video_views"),
                "thumb_urls": urls,
                "thumb_url": urls[0] if urls else None,
            }
        )
    return out


def download_competitor_thumb(
    video_id: str,
    *,
    channel: str,
    urls: list[str] | None = None,
    cache_dir: Path | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Download first working ytimg URL into ops cache. Escalates on network block."""
    import httpx

    dest_dir = cache_dir or (THUMB_CACHE / channel)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{video_id}.jpg"
    if dest.is_file() and dest.stat().st_size > 2000:
        return {"ok": True, "path": str(dest), "cached": True, "video_id": video_id}

    candidates = urls or youtube_thumb_urls(video_id)
    last_err: str | None = None
    for url in candidates:
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                r = client.get(url)
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                raw = r.content
                # maxres sometimes returns a tiny placeholder
                if len(raw) < 2000:
                    continue
                dest.write_bytes(raw)
                return {
                    "ok": True,
                    "path": str(dest),
                    "cached": False,
                    "url": url,
                    "video_id": video_id,
                    "bytes": len(raw),
                }
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)[:240]
            continue
    return {
        "ok": False,
        "video_id": video_id,
        "error": last_err or "no_working_thumb_url",
        "escalation": (
            "APPROVAL NEEDED (parent chat):\n"
            f"- Command/call: httpx GET i.ytimg.com/vi/{video_id}/…\n"
            "- Why: download competitor thumbnails for SMM vision redesign\n"
            "- Approval: network"
        ),
    }


def analyze_thumb_pillow(image_path: str | Path) -> dict[str, Any]:
    """Heuristic style analysis: face vs map, text density, contrast, mood."""
    from PIL import Image, ImageFilter, ImageStat

    path = Path(image_path)
    im = Image.open(path).convert("RGB")
    w, h = im.size
    # Downsample for speed
    small = im.resize((min(320, w), max(1, int(min(320, w) * h / max(w, 1)))), Image.Resampling.BILINEAR)
    sw, sh = small.size
    pixels = list(small.getdata())
    n = max(len(pixels), 1)

    # Skin-ish warm tones → face bias
    skin = 0
    warm = 0
    cool = 0
    for r, g, b in pixels:
        if r > 95 and g > 40 and b > 20 and (r - g) > 15 and r > b:
            skin += 1
        if r + 20 > g and r + 20 > b:
            warm += 1
        if b > r and b > g:
            cool += 1

    skin_ratio = skin / n
    warm_ratio = warm / n
    cool_ratio = cool / n

    # Edge density (proxy for maps / text / busy collage)
    edges = small.convert("L").filter(ImageFilter.FIND_EDGES)
    estat = ImageStat.Stat(edges)
    edge_mean = float(estat.mean[0]) / 255.0

    # Text density: high-contrast edges in top + bottom bands
    band = max(1, sh // 4)
    top = edges.crop((0, 0, sw, band))
    bot = edges.crop((0, sh - band, sw, sh))
    mid = edges.crop((0, band, sw, sh - band))
    top_e = float(ImageStat.Stat(top).mean[0]) / 255.0
    bot_e = float(ImageStat.Stat(bot).mean[0]) / 255.0
    mid_e = float(ImageStat.Stat(mid).mean[0]) / 255.0
    text_density = round(min(1.0, (top_e + bot_e) * 1.4), 3)

    # Global contrast
    gray = small.convert("L")
    gstat = ImageStat.Stat(gray)
    # approximate p5/p95 via histogram
    hist = gray.histogram()
    total = sum(hist) or 1
    cum = 0
    p5 = 0
    p95 = 255
    for i, c in enumerate(hist):
        cum += c
        if cum / total <= 0.05:
            p5 = i
        if cum / total >= 0.95:
            p95 = i
            break
    contrast = round((p95 - p5) / 255.0, 3)

    # Map bias: mid-frame high edges + low skin
    map_score = mid_e * (1.0 - min(skin_ratio * 3, 1.0))
    face_score = skin_ratio * 2.5 + (0.15 if warm_ratio > 0.35 else 0.0)
    if face_score >= map_score and skin_ratio >= 0.04:
        map_vs_face = "face"
    elif map_score > face_score and mid_e > 0.08:
        map_vs_face = "map"
    else:
        map_vs_face = "artifact" if skin_ratio < 0.03 else "either"

    if cool_ratio > warm_ratio and contrast < 0.55:
        mood = "moody_calm"
    elif contrast >= 0.65 and warm_ratio >= 0.3:
        mood = "dramatic"
    elif warm_ratio >= cool_ratio:
        mood = "warm_dramatic"
    else:
        mood = "cool_documentary"

    return {
        "analyzer": "pillow",
        "map_vs_face": map_vs_face,
        "text_density": text_density,
        "color_contrast": contrast,
        "mood": mood,
        "skin_ratio": round(skin_ratio, 4),
        "edge_mean": round(edge_mean, 4),
        "warm_ratio": round(warm_ratio, 4),
        "cool_ratio": round(cool_ratio, 4),
        "width": w,
        "height": h,
    }


def analyze_thumb_vision(
    image_paths: list[str | Path],
    *,
    channel: str,
) -> dict[str, Any] | None:
    """Optional OpenRouter vision critique of competitor thumbs."""
    try:
        from src.services.settings import get_settings
        from src.services.vision_judge import _b64_data_url, _openrouter_vision
    except Exception:  # noqa: BLE001
        return None

    settings = get_settings()
    urls: list[str] = []
    for p in image_paths[:3]:
        data = _b64_data_url(Path(p))
        if data:
            urls.append(data)
    if not urls:
        return None

    if channel == "napping_historian":
        genre = (
            "History Calling–style mystery documentary thumbnails "
            "(secret documents, soft portraits, moody calm — NOT What-If punch faces)"
        )
    else:
        genre = (
            "dramatic What-If / AlternateHistoryHub-style punch thumbnails "
            "(commanding face or bold map, high contrast, short yellow promise text)"
        )

    system = (
        "Return ONLY raw JSON with keys: "
        "map_vs_face (face|map|artifact|either), text_density (0-1), "
        "color_contrast (0-1), mood (string), focal_subject (string), "
        "style_notes (short string), word_count_max (int)."
    )
    user = (
        f"You are a YouTube thumbnail strategist for a {genre} channel. "
        "Analyze these top competitor thumbnails. Infer shared style patterns "
        "our next thumbs should match (without copying logos/faces literally)."
    )
    parsed = _openrouter_vision(
        settings=settings, system=system, user_text=user, image_urls=urls
    )
    if not parsed or not isinstance(parsed, dict):
        return None
    return {
        "analyzer": "vision",
        "map_vs_face": str(parsed.get("map_vs_face") or "either"),
        "text_density": float(parsed.get("text_density") or 0.4),
        "color_contrast": float(parsed.get("color_contrast") or 0.5),
        "mood": str(parsed.get("mood") or "dramatic"),
        "focal_subject": parsed.get("focal_subject"),
        "style_notes": str(parsed.get("style_notes") or "")[:400],
        "word_count_max": int(parsed.get("word_count_max") or 5),
    }


def extract_style_cues_from_analyses(
    analyses: list[dict[str, Any]],
    *,
    channel: str,
) -> dict[str, Any]:
    """Aggregate per-image analyses into channel thumbnail style cues (unit-testable)."""
    if not analyses:
        # Channel-faithful defaults when no images
        if channel == "napping_historian":
            return {
                "map_vs_face": "artifact",
                "mood": "moody_calm",
                "text_density": 0.35,
                "color_contrast": 0.5,
                "word_count_max": 4,
                "preset": "historian_soft",
                "focal_subject": "artifact_or_portrait",
                "text_color": "soft_white",
                "stroke": "soft_dark",
                "contrast_boost": 1.0,
                "n_samples": 0,
            }
        return {
            "map_vs_face": "face",
            "mood": "dramatic",
            "text_density": 0.45,
            "color_contrast": 0.7,
            "word_count_max": 5,
            "preset": "napstorian_punch",
            "focal_subject": "face_or_map",
            "text_color": "yellow",
            "stroke": "black_sharp",
            "contrast_boost": 1.15,
            "n_samples": 0,
        }

    faces = Counter(str(a.get("map_vs_face") or "either") for a in analyses)
    moods = Counter(str(a.get("mood") or "dramatic") for a in analyses)
    map_vs_face = faces.most_common(1)[0][0]
    mood = moods.most_common(1)[0][0]
    text_density = sum(float(a.get("text_density") or 0) for a in analyses) / len(analyses)
    contrast = sum(float(a.get("color_contrast") or 0) for a in analyses) / len(analyses)

    # Respect channel brand even when competitors diverge
    if channel == "napping_historian":
        # History Calling: prefer artifact/document; soften punchy face-only clusters
        if map_vs_face == "face":
            map_vs_face = "artifact"
        if mood in {"dramatic", "warm_dramatic"} and contrast < 0.75:
            mood = "moody_calm"
        word_count_max = 3 if text_density > 0.55 else 4
        contrast_boost = 1.05 if contrast >= 0.6 else 1.0
        cues = {
            "preset": "historian_soft",
            "focal_subject": "artifact_or_portrait",
            "map_vs_face": map_vs_face if map_vs_face in {"artifact", "map", "either"} else "artifact",
            "word_count_max": word_count_max,
            "text_color": "soft_white",
            "stroke": "soft_dark",
            "contrast_boost": contrast_boost,
            "mood": mood if "calm" in mood or "moody" in mood else "moody_calm",
        }
    else:
        # What-If punch: keep face/map energy and high contrast
        if map_vs_face == "artifact":
            map_vs_face = "face" if faces.get("face", 0) >= faces.get("map", 0) else "map"
        word_count_max = 4 if text_density > 0.6 else 5
        contrast_boost = 1.2 if contrast >= 0.55 else 1.15
        cues = {
            "preset": "napstorian_punch",
            "focal_subject": "face_or_map",
            "map_vs_face": map_vs_face if map_vs_face in {"face", "map", "either"} else "face",
            "word_count_max": word_count_max,
            "text_color": "yellow",
            "stroke": "black_sharp",
            "contrast_boost": contrast_boost,
            "mood": "dramatic" if "calm" in mood else mood,
        }

    vision_notes = [
        str(a.get("style_notes") or "").strip()
        for a in analyses
        if a.get("analyzer") == "vision" and a.get("style_notes")
    ]
    cues.update(
        {
            "text_density": round(text_density, 3),
            "color_contrast": round(contrast, 3),
            "n_samples": len(analyses),
            "vote_map_vs_face": dict(faces),
            "vote_mood": dict(moods),
            "vision_notes": vision_notes[:2],
        }
    )
    return cues


def build_prompt_addendum(channel: str, cues: dict[str, Any]) -> str:
    """Human-readable thumbnail prompt guidance from aggregated cues."""
    mvf = cues.get("map_vs_face") or "either"
    mood = cues.get("mood") or "dramatic"
    wc = int(cues.get("word_count_max") or 5)
    dens = cues.get("text_density")
    contrast = cues.get("color_contrast")
    notes = cues.get("vision_notes") or []

    if channel == "napping_historian":
        lines = [
            "SMM competitor-vision redesign (History Calling–like mystery):",
            f"- Focal: Secret Document / artifact dominates; map_vs_face={mvf}.",
            "- Watcher face stays secondary silhouette — parchment/seal is the click magnet.",
            f"- Mood: {mood}. Soft white title text, soft dark stroke, low collage clutter.",
            f"- Gold/title promise ≤ {wc} words; estimated competitor text density≈{dens}.",
            f"- Contrast target≈{contrast}; keep nocturnal candle key + deep vignette.",
        ]
    else:
        lines = [
            "SMM competitor-vision redesign (What-If punch):",
            f"- Focal: commanding {mvf} — one dominant face OR bold map, never collage grid.",
            "- Portrait LEFT / text RIGHT when face; map can fill with clean title zone.",
            f"- Mood: {mood}. Yellow promise text, black sharp stroke, high punch contrast.",
            f"- Title promise ≤ {wc} words; competitor text density≈{dens}.",
            f"- Contrast target≈{contrast}; Rembrandt key + deep cinematic shadows.",
        ]
    if notes:
        lines.append(f"- Vision notes: {'; '.join(notes)[:280]}")
    return "\n".join(lines)


def thumbnail_template_path(channel: str) -> Path:
    if channel == "napping_historian":
        return CONFIG_DIR / "prompts" / "napping_historian" / "thumbnail_template.txt"
    return CONFIG_DIR / "prompts" / "thumbnail_template.txt"


def patch_thumbnail_template(
    channel: str,
    addendum: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Replace (or insert) the SMM THUMB REDESIGN block in the channel template."""
    path = thumbnail_template_path(channel)
    if not path.is_file():
        return {"ok": False, "reason": "template_missing", "path": str(path)}
    text = path.read_text(encoding="utf-8")
    block = (
        f"\n\n# === SMM THUMB REDESIGN {channel} ===\n"
        f"{addendum.rstrip()}\n"
        f"# === END SMM THUMB REDESIGN ===\n"
    )
    if _REDESIGN_MARKER_RE.search(text):
        new_text = _REDESIGN_MARKER_RE.sub(block, text, count=1)
        action = "replaced"
    else:
        new_text = text.rstrip() + block
        action = "appended"
    if dry_run:
        return {"ok": True, "dry_run": True, "action": action, "path": str(path)}
    path.write_text(new_text, encoding="utf-8")
    return {"ok": True, "action": action, "path": str(path)}


def style_before_after(
    channel: str,
    new_cues: dict[str, Any],
) -> dict[str, Any]:
    before = dict(thumbnail_style_preset(channel) or {})
    after = {
        "preset": new_cues.get("preset") or before.get("preset"),
        "focal_subject": new_cues.get("focal_subject") or before.get("focal_subject"),
        "map_vs_face": new_cues.get("map_vs_face"),
        "word_count_max": new_cues.get("word_count_max"),
        "text_color": new_cues.get("text_color") or before.get("text_color"),
        "stroke": new_cues.get("stroke") or before.get("stroke"),
        "contrast_boost": new_cues.get("contrast_boost"),
        "mood": new_cues.get("mood"),
        "text_density_est": new_cues.get("text_density"),
        "color_contrast_est": new_cues.get("color_contrast"),
        "source": "smm_thumb_redesign",
        "updated_at": _now(),
    }
    return {"before": before, "after": after}


def redesign_channel_thumbs(
    channel: str,
    *,
    dry_run: bool = False,
    limit: int = 5,
    use_vision: bool = True,
    fetch_images: bool = True,
    ops_dir: Path | None = None,
) -> dict[str, Any]:
    """Fetch → analyze → snapshot → patch style + prompt guidance for one channel."""
    ch = (channel or "").strip().lower()
    if ch not in CHANNELS:
        return {"ok": False, "reason": "unknown_channel", "channel": channel}

    refs = collect_top_competitor_refs(ch, limit=limit, ops_dir=ops_dir)
    out: dict[str, Any] = {
        "ok": True,
        "channel": ch,
        "dry_run": dry_run,
        "n_refs": len(refs),
        "competitor_refs": [
            {
                "video_id": r.get("video_id"),
                "title": r.get("title"),
                "thumb_url": r.get("thumb_url"),
                "video_views": r.get("video_views"),
            }
            for r in refs
        ],
    }
    if not refs:
        out["skipped"] = True
        out["reason"] = "no_competitor_refs"
        return out

    local_paths: list[Path] = []
    download_errors: list[dict[str, Any]] = []
    if fetch_images and not dry_run:
        for r in refs:
            dl = download_competitor_thumb(
                str(r["video_id"]),
                channel=ch,
                urls=list(r.get("thumb_urls") or []),
            )
            if dl.get("ok") and dl.get("path"):
                local_paths.append(Path(str(dl["path"])))
            else:
                download_errors.append(dl)
    elif fetch_images and dry_run:
        # Prefer cached files for dry analysis
        for r in refs:
            cached = THUMB_CACHE / ch / f"{r['video_id']}.jpg"
            if cached.is_file():
                local_paths.append(cached)

    analyses: list[dict[str, Any]] = []
    for p in local_paths:
        try:
            analyses.append({**analyze_thumb_pillow(p), "path": str(p)})
        except Exception as exc:  # noqa: BLE001
            analyses.append({"analyzer": "pillow", "error": str(exc)[:200], "path": str(p)})

    vision: dict[str, Any] | None = None
    if use_vision and local_paths and not dry_run:
        try:
            vision = analyze_thumb_vision(local_paths[:3], channel=ch)
            if vision:
                analyses.append(vision)
        except Exception as exc:  # noqa: BLE001
            out["vision_error"] = str(exc)[:200]

    # If downloads failed entirely, still derive soft cues from titles via pipeline helper
    if not analyses:
        try:
            from src.services.smm_pipeline_ab import _title_thumb_style_cues

            for r in refs:
                style = _title_thumb_style_cues(str(r.get("title") or ""))
                analyses.append(
                    {
                        "analyzer": "title_fallback",
                        "map_vs_face": style["map_vs_face"],
                        "mood": "dramatic" if style["mood"] == "dramatic" else "moody_calm",
                        "text_density": min(1.0, float(style["word_count"]) / 12.0),
                        "color_contrast": 0.55,
                    }
                )
        except Exception:  # noqa: BLE001
            pass

    cues = extract_style_cues_from_analyses(
        [a for a in analyses if not a.get("error")],
        channel=ch,
    )
    addendum = build_prompt_addendum(ch, cues)
    ba = style_before_after(ch, cues)
    out["style_cues"] = cues
    out["prompt_addendum"] = addendum
    out["before"] = ba["before"]
    out["after"] = ba["after"]
    out["n_analyzed"] = len([a for a in analyses if not a.get("error")])
    out["download_errors"] = download_errors
    if download_errors and not local_paths:
        out["network_escalation"] = download_errors[0].get("escalation")

    snapshot_row: dict[str, Any] | None = None
    template_patch: dict[str, Any] | None = None
    overrides_row: dict[str, Any] | None = None

    if not dry_run:
        tpl = thumbnail_template_path(ch)
        try:
            from src.services.smm_pipeline_ab import snapshot_coding

            paths = [tpl]
            ov = CONFIG_DIR / "channel_editing_overrides.json"
            if ov.is_file():
                paths.append(ov)
            snapshot_row = snapshot_coding(
                paths,
                reason=f"pre_thumb_redesign_{ch}",
                competitor_cue=f"thumbs:{','.join(r['video_id'] for r in refs[:3])}",
            )
        except Exception as exc:  # noqa: BLE001
            snapshot_row = {"error": str(exc)[:200]}

        overrides_patch = {
            "thumbnail_style": ba["after"],
            "thumb_prompt_addendum": addendum,
        }
        overrides_row = write_channel_overrides(ch, overrides_patch=overrides_patch)
        template_patch = patch_thumbnail_template(ch, addendum, dry_run=False)

    out["snapshot"] = snapshot_row
    out["template_patch"] = template_patch
    out["overrides_updated"] = bool(overrides_row)
    out["ts"] = _now()

    log_row = {
        "ts": out["ts"],
        "event": "thumb_redesign",
        "channel": ch,
        "dry_run": dry_run,
        "before": ba["before"],
        "after": ba["after"],
        "prompt_addendum": addendum,
        "competitor_refs": out["competitor_refs"],
        "n_analyzed": out["n_analyzed"],
        "style_cues": {
            k: cues.get(k)
            for k in (
                "map_vs_face",
                "mood",
                "text_density",
                "color_contrast",
                "word_count_max",
                "n_samples",
            )
        },
        "snapshot_id": (snapshot_row or {}).get("id"),
        "template_patch": template_patch,
    }
    if not dry_run:
        _append_jsonl(REDESIGN_LOG, log_row)
        try:
            from src.services.premium_editor_smm import PREMIUM_LOG

            _append_jsonl(
                PREMIUM_LOG,
                {
                    "ts": out["ts"],
                    "channel": ch,
                    "lever": "thumbnail_redesign",
                    "value": {
                        "map_vs_face": cues.get("map_vs_face"),
                        "mood": cues.get("mood"),
                        "word_count_max": cues.get("word_count_max"),
                    },
                    "before_preset": (ba["before"] or {}).get("preset"),
                    "after_preset": (ba["after"] or {}).get("preset"),
                    "competitor_video_ids": [r["video_id"] for r in refs],
                    "prompt_addendum_summary": addendum.split("\n")[0][:160],
                    "dry_run": False,
                },
            )
        except Exception as exc:  # noqa: BLE001
            out["premium_log_error"] = str(exc)[:160]

    out["log_path"] = str(REDESIGN_LOG)
    return out


def run_thumb_redesign_beat(
    *,
    dry_run: bool = False,
    channels: tuple[str, ...] | list[str] | None = None,
    limit: int = 5,
    use_vision: bool = True,
    fetch_images: bool = True,
) -> dict[str, Any]:
    """Premium-editor / CEO hook: redesign thumbs for both Brand channels."""
    target = tuple(
        c.strip().lower()
        for c in (channels or CHANNELS)
        if str(c).strip()
    ) or CHANNELS
    results: dict[str, Any] = {
        "ok": True,
        "module": "smm_thumb_redesign",
        "dry_run": dry_run,
        "channels": {},
    }
    for ch in target:
        try:
            results["channels"][ch] = redesign_channel_thumbs(
                ch,
                dry_run=dry_run,
                limit=limit,
                use_vision=use_vision,
                fetch_images=fetch_images,
            )
        except Exception as exc:  # noqa: BLE001
            results["channels"][ch] = {"ok": False, "error": str(exc)[:300]}
            results["ok"] = False
    # Surface first network escalation for parent chat
    for ch, payload in (results.get("channels") or {}).items():
        if isinstance(payload, dict) and payload.get("network_escalation"):
            results["network_escalation"] = payload["network_escalation"]
            break
    results["log_path"] = str(REDESIGN_LOG)
    return results
