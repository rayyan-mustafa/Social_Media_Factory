"""Derivative product registry + ready-now image packs.

QUALITY FREEZE: only image reuse ships. Podcast/blog/ebook/shorts-video frozen.
See ``output/ops/CONTENT_REUSE.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Build order under freeze: image packs only; rest frozen.
DERIVATIVE_MODULE_ORDER: tuple[str, ...] = (
    "pinterest_pins",
    "image_reuse_stills",
    "youtube_shorts_clip",
    "podcast_audio",
    "blog_newsletter",
    "ebook_pdf",
    "tiktok_shorts_text",
)

FROZEN_MODULES: frozenset[str] = frozenset(
    {
        "podcast_audio",
        "blog_newsletter",
        "ebook_pdf",
        "tiktok_shorts_text",
        "youtube_shorts_clip",
        "seo_site",
        "social_distribution",
    }
)

ALLOWED_UNDER_FREEZE: frozenset[str] = frozenset(
    {
        "pinterest_pins",
        "image_reuse_stills",
    }
)


@dataclass(frozen=True)
class DerivativeModule:
    """Thin registry entry — no GPU."""

    id: str
    title: str
    needs_runpod: bool
    inputs: tuple[str, ...]
    status: str  # planned | stub | ready | frozen
    notes: str = ""


PLANNED_MODULES: tuple[DerivativeModule, ...] = (
    DerivativeModule(
        id="pinterest_pins",
        title="Pinterest pins (stills → YT)",
        needs_runpod=False,
        inputs=("images/scene_*.jpg", "script/script.json"),
        status="ready",
        notes="QUALITY FREEZE side track P0. Pack via image_reuse.build_image_reuse_pack.",
    ),
    DerivativeModule(
        id="image_reuse_stills",
        title="IG carousel / FB image / quote / Threads-X",
        needs_runpod=False,
        inputs=("images/scene_*.jpg",),
        status="ready",
        notes="Same packager as Pinterest; P1/P2 side traffic.",
    ),
    DerivativeModule(
        id="podcast_audio",
        title="Podcast / audio feed",
        needs_runpod=False,
        inputs=("audio/narration_full.wav", "audio/voice_manifest.json"),
        status="frozen",
        notes="Frozen until daily YT scorecard unfreeze gate.",
    ),
    DerivativeModule(
        id="blog_newsletter",
        title="Blog / newsletter",
        needs_runpod=False,
        inputs=("script/script.json", "script/narration.txt"),
        status="frozen",
        notes="Frozen — QUALITY FREEZE.",
    ),
    DerivativeModule(
        id="ebook_pdf",
        title="Ebook / PDF",
        needs_runpod=False,
        inputs=("script/script.json",),
        status="frozen",
        notes="Frozen — QUALITY FREEZE.",
    ),
    DerivativeModule(
        id="tiktok_shorts_text",
        title="TikTok / Shorts (text+audio)",
        needs_runpod=False,
        inputs=("script/script.json", "audio/"),
        status="frozen",
        notes="Frozen — video-heavy reuse blocked.",
    ),
    DerivativeModule(
        id="youtube_shorts_clip",
        title="YouTube Shorts (40s clip from longform final)",
        needs_runpod=False,
        inputs=("video/final.mp4", "audio/voice_manifest.json"),
        status="frozen",
        notes="Allowed only via --allow-video-reuse or smm.allow_youtube_shorts_clip (does not unfreeze podcast).",
    ),
)


def content_kernel_paths(job_dir: Path | str) -> dict[str, Path]:
    """Canonical Phase A assets other products may reuse."""
    root = Path(job_dir)
    return {
        "script_json": root / "script" / "script.json",
        "narration_txt": root / "script" / "narration.txt",
        "voice_manifest": root / "audio" / "voice_manifest.json",
        "narration_wav": root / "audio" / "narration_full.wav",
        "narration_raw_wav": root / "audio" / "narration_raw_full.wav",
        "images_dir": root / "images",
        "image_reuse_dir": root / "derivatives" / "image_reuse",
        "shorts_mp4": root / "derivatives" / "shorts" / "short_40s.mp4",
    }


def _allow_youtube_shorts_clip_flag() -> bool:
    try:
        from src.services.settings import CONFIG_DIR

        raw = (CONFIG_DIR / "agents_settings.json").read_text(encoding="utf-8")
        import json

        cfg = json.loads(raw)
        smm = cfg.get("smm") or {}
        return bool(smm.get("allow_youtube_shorts_clip"))
    except Exception:  # noqa: BLE001
        return False


def shorts_clip_allowed(*, allow_video_reuse: bool = False) -> bool:
    """True when CLI override or dedicated Shorts allowlist is on.

    Does **not** honor a global freeze_video_heavy=false requirement — podcast
    stays frozen unless that flag is flipped separately.
    """
    if allow_video_reuse:
        return True
    return _allow_youtube_shorts_clip_flag()


def is_module_allowed(module_id: str, *, freeze_video_heavy: bool = True) -> bool:
    if module_id == "youtube_shorts_clip":
        return shorts_clip_allowed()
    if not freeze_video_heavy:
        return True
    if module_id in ALLOWED_UNDER_FREEZE:
        return True
    return module_id not in FROZEN_MODULES


def describe_derivative_plan() -> dict[str, Any]:
    """Machine-readable reuse map for ops / CLI."""
    return {
        "quality_freeze": True,
        "reuse_freeze_video_heavy": True,
        "allowed_under_freeze": sorted(ALLOWED_UNDER_FREEZE),
        "frozen_modules": sorted(FROZEN_MODULES),
        "youtube_shorts_clip_allowed": shorts_clip_allowed(),
        "runpod_required_for": ["youtube_stills", "youtube_compose_path"],
        "red_day_ok": ["image_reuse_stills", "pinterest_pins", "phase_a_prep"],
        "green_only": ["youtube_visuals_burn"],
        "modules": [
            {
                "id": m.id,
                "title": m.title,
                "needs_runpod": m.needs_runpod,
                "inputs": list(m.inputs),
                "status": m.status,
                "notes": m.notes,
                "allowed_now": is_module_allowed(m.id),
            }
            for m in PLANNED_MODULES
        ],
        "hook": "ops job meta.ready_for_derivatives after Phase A park",
        "docs": "output/ops/CONTENT_REUSE.md",
    }
