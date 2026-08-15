"""Generate YouTube packaging (title, description, tags, thumbnail) for a job."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from src.domain.models import ScriptResult
from src.services.openrouter import OpenRouterClient, OpenRouterError
from src.services.settings import ROOT, Settings, get_settings
from src.services.thumbnail_image import ThumbnailImageError, generate_thumbnail_image
from src.services.thumbnail_prompt import (
    ThumbnailPromptMode,
    ThumbnailPromptResult,
    write_thumbnail_prompt,
)

ThumbnailPromptModeArg = Literal["rules", "openrouter", "auto"]


def _resolve_packaging_channel(
    job_dir: Path | None = None,
    *,
    channel: str | None = None,
) -> str | None:
    """Prefer explicit channel, then SCRIPT_CHANNEL, then job meta/manifest."""
    ch = (channel or os.getenv("SCRIPT_CHANNEL") or "").strip()
    if ch:
        return ch
    if job_dir is None:
        return None
    jd = Path(job_dir)
    for rel in ("pipeline_manifest.json", "publish_manifest.json"):
        path = jd / rel
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        for key in ("channel", "sheet_tab", "youtube_channel"):
            val = (data.get(key) or meta.get(key) or "").strip()
            if val:
                return val
    return None



class YoutubeMetaGenerateError(RuntimeError):
    pass


@dataclass
class YoutubePackResult:
    meta_dir: Path
    title_path: Path
    description_path: Path
    tags_path: Path
    thumbnail_prompt_path: Path | None = None
    thumbnail_path: Path | None = None
    thumbnail_prompt_mode: str | None = None
    meta_source: str = "openrouter"
    manifest_path: Path | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class ThumbnailTestResult:
    out_dir: Path
    script_path: Path
    thumbnail_prompt_path: Path
    thumbnail_path: Path | None
    thumbnail_prompt_mode: str
    manifest_path: Path
    api_status: dict[str, str]
    warnings: list[str] = field(default_factory=list)


def generate_thumbnail_only(
    script_path: Path | str,
    out_dir: Path | str,
    *,
    seed_title: str | None = None,
    thumbnail_prompt_mode: ThumbnailPromptMode = "auto",
    settings: Settings | None = None,
    force: bool = False,
    channel: str | None = None,
) -> ThumbnailTestResult:
    """Generate thumbnail prompt + image only into a custom output directory."""
    sp = Path(script_path)
    if not sp.exists():
        raise YoutubeMetaGenerateError(f"script.json missing: {sp}")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    script = ScriptResult.model_validate(json.loads(sp.read_text(encoding="utf-8")))
    s = settings or get_settings()
    warnings: list[str] = []
    api_status: dict[str, str] = {}

    thumb_prompt_path = out_dir / "thumbnail_prompt.txt"
    thumb_img_path = out_dir / "thumbnail.jpg"

    # Thumbnail prompt
    try:
        prompt_result = write_thumbnail_prompt(
            script,
            thumb_prompt_path,
            seed_title=seed_title,
            mode=thumbnail_prompt_mode,
            settings=s,
            channel=_resolve_packaging_channel(channel=channel),
        )
        api_status["thumbnail_prompt"] = prompt_result.mode
    except OpenRouterError as exc:
        api_status["thumbnail_prompt"] = f"error: {exc}"
        raise YoutubeMetaGenerateError(f"thumbnail prompt failed: {exc}") from exc

    # Thumbnail image via WaveSpeed Seedream
    thumb_path: Path | None = None
    try:
        generate_thumbnail_image(
            prompt_result.prompt,
            thumb_img_path,
            settings=s,
            skip_if_exists=not force,
        )
        thumb_path = thumb_img_path
        api_status["thumbnail_image"] = "ok"
    except ThumbnailImageError as exc:
        api_status["thumbnail_image"] = f"error: {exc}"
        if thumb_img_path.exists():
            thumb_path = thumb_img_path
            warnings.append(f"kept existing thumbnail ({exc})")
        else:
            warnings.append(f"thumbnail image failed ({exc})")

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "script_path": str(sp.resolve()),
        "out_dir": str(out_dir.resolve()),
        "thumbnail_prompt_mode": prompt_result.mode,
        "seed_title": seed_title,
        "api_status": api_status,
        "files": {
            "thumbnail_prompt": str(thumb_prompt_path.resolve()),
            "thumbnail": str(thumb_path.resolve()) if thumb_path else None,
        },
        "thumbnail_bytes": thumb_path.stat().st_size if thumb_path else None,
        "warnings": warnings,
    }
    manifest_path = out_dir / "test_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return ThumbnailTestResult(
        out_dir=out_dir,
        script_path=sp,
        thumbnail_prompt_path=thumb_prompt_path,
        thumbnail_path=thumb_path,
        thumbnail_prompt_mode=prompt_result.mode,
        manifest_path=manifest_path,
        api_status=api_status,
        warnings=warnings,
    )


def generate_youtube_pack(
    job_dir: Path | str,
    *,
    script_path: Path | str | None = None,
    seed_title: str | None = None,
    skip_meta: bool = False,
    skip_thumbnail_prompt: bool = False,
    skip_thumbnail_image: bool = False,
    thumbnail_prompt_mode: ThumbnailPromptMode = "auto",
    settings: Settings | None = None,
    force: bool = False,
    channel: str | None = None,
) -> YoutubePackResult:
    """Generate youtube_meta/ for a job: meta → thumbnail prompt → thumbnail image."""
    job_dir = Path(job_dir)
    if not job_dir.is_dir():
        raise YoutubeMetaGenerateError(f"job dir missing: {job_dir}")

    sp = Path(script_path) if script_path else job_dir / "script" / "script.json"
    if not sp.exists():
        raise YoutubeMetaGenerateError(f"script.json missing: {sp}")

    script = ScriptResult.model_validate(
        json.loads(sp.read_text(encoding="utf-8"))
    )
    meta_dir = job_dir / "youtube_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    s = settings or get_settings()
    pack_channel = _resolve_packaging_channel(job_dir, channel=channel)

    warnings: list[str] = []
    result = YoutubePackResult(
        meta_dir=meta_dir,
        title_path=meta_dir / "title.txt",
        description_path=meta_dir / "description.txt",
        tags_path=meta_dir / "tags.json",
    )

    # 1) OpenRouter metadata
    if skip_meta:
        warnings.append("skipped OpenRouter metadata (--skip-meta)")
        for p in (result.title_path, result.description_path, result.tags_path):
            if not p.exists():
                warnings.append(f"missing {p.name} (skip-meta)")
    else:
        try:
            _write_openrouter_meta(
                script,
                meta_dir,
                seed_title=seed_title,
                settings=s,
                force=force,
            )
            result.meta_source = "openrouter"
        except OpenRouterError as exc:
            if not _meta_files_exist(meta_dir):
                raise YoutubeMetaGenerateError(
                    f"OpenRouter metadata failed and no existing meta: {exc}"
                ) from exc
            warnings.append(f"OpenRouter metadata skipped ({exc}); kept existing files")
            result.meta_source = "existing"

    # 2) Thumbnail prompt (rules → OpenRouter fallback in auto mode)
    thumb_prompt_path = meta_dir / "thumbnail_prompt.txt"
    prompt_result: ThumbnailPromptResult | None = None
    if skip_thumbnail_prompt:
        warnings.append("skipped thumbnail prompt (--skip-thumbnail-prompt)")
        if thumb_prompt_path.exists():
            result.thumbnail_prompt_path = thumb_prompt_path
    else:
        write_if = force or not thumb_prompt_path.exists()
        if write_if:
            prompt_result = write_thumbnail_prompt(
                script,
                thumb_prompt_path,
                seed_title=seed_title or _read_seed_title(meta_dir),
                mode=thumbnail_prompt_mode,
                settings=s,
                channel=pack_channel,
            )
        else:
            prompt_result = ThumbnailPromptResult(
                prompt=thumb_prompt_path.read_text(encoding="utf-8"),
                mode="existing",
            )
        result.thumbnail_prompt_path = thumb_prompt_path
        result.thumbnail_prompt_mode = prompt_result.mode

    # 3) Thumbnail image via WaveSpeed Seedream
    thumb_img_path = meta_dir / "thumbnail.jpg"
    if skip_thumbnail_image:
        warnings.append("skipped thumbnail image (--skip-thumbnail-image)")
        if thumb_img_path.exists():
            result.thumbnail_path = thumb_img_path
    else:
        prompt_text = (
            prompt_result.prompt
            if prompt_result
            else thumb_prompt_path.read_text(encoding="utf-8")
        )
        try:
            generate_thumbnail_image(
                prompt_text,
                thumb_img_path,
                settings=s,
                skip_if_exists=not force,
            )
            result.thumbnail_path = thumb_img_path
        except ThumbnailImageError as exc:
            if thumb_img_path.exists():
                warnings.append(f"thumbnail image kept existing ({exc})")
                result.thumbnail_path = thumb_img_path
            else:
                warnings.append(f"thumbnail image skipped ({exc})")

    result.warnings = warnings
    _write_chapter_timestamps(job_dir, script, meta_dir, warnings)
    _ensure_description_disclosure(meta_dir, s)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "job_dir": str(job_dir.resolve()),
        "script_path": str(sp.resolve()),
        "meta_dir": str(meta_dir.resolve()),
        "meta_source": result.meta_source,
        "thumbnail_prompt_mode": result.thumbnail_prompt_mode,
        "seed_title": seed_title,
        "files": {
            "title": str(result.title_path) if result.title_path.exists() else None,
            "description": str(result.description_path)
            if result.description_path.exists()
            else None,
            "tags": str(result.tags_path) if result.tags_path.exists() else None,
            "thumbnail_prompt": str(result.thumbnail_prompt_path)
            if result.thumbnail_prompt_path and result.thumbnail_prompt_path.exists()
            else None,
            "thumbnail": str(result.thumbnail_path)
            if result.thumbnail_path and result.thumbnail_path.exists()
            else None,
        },
        "warnings": warnings,
    }
    manifest_path = meta_dir / "pack_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    result.manifest_path = manifest_path
    return result


def _meta_files_exist(meta_dir: Path) -> bool:
    return (meta_dir / "title.txt").exists()


def _read_seed_title(meta_dir: Path) -> str | None:
    title_path = meta_dir / "title.txt"
    if title_path.exists():
        t = title_path.read_text(encoding="utf-8").strip()
        return t or None
    return None


def _write_openrouter_meta(
    script: ScriptResult,
    meta_dir: Path,
    *,
    seed_title: str | None,
    settings: Settings,
    force: bool,
) -> None:
    title_path = meta_dir / "title.txt"
    desc_path = meta_dir / "description.txt"
    tags_path = meta_dir / "tags.json"

    if not force and title_path.exists() and desc_path.exists() and tags_path.exists():
        return

    client = OpenRouterClient(settings)
    chapters = "\n".join(
        f"- {ch.title}: {', '.join(ch.key_points[:3])}"
        for ch in script.outline.chapters[:10]
    )
    seed = seed_title or script.title
    user = f"""Generate YouTube packaging for a historical alternate-history documentary.

Audience: history nerds, what-if / counterfactual history lovers.
Style: high CTR title, clickable description, strong SEO tags.
Channels: napstorian and napping_historian share the SAME description structure
(sections below). Brand voice may differ in wording only — not layout.

Video topic: {script.topic}
Script title: {script.title}
Hook: {script.hook}
Seed title example (match this energy): {seed}

Chapters (for context only — do NOT paste timestamp lists):
{chapters}

Return JSON only:
{{
  "triple_keywords": ["3 to 5 core SEO keywords/phrases used everywhere"],
  "title": "string, max 100 chars, curiosity gap, caps sparingly",
  "description": "string — follow DESCRIPTION STRUCTURE exactly",
  "tags": ["high-intent SEO tags — see rules below"]
}}

DESCRIPTION STRUCTURE (required — same format for every channel):
1) Opening hook (1–2 sentences; triple keywords early)
2) Short body (1–2 paragraphs, documentary tone)
3) Soft CTA (one line — invite subscribe and/or a counterfactual comment)
4) Hashtags line (reuse triple keywords where natural)
5) Do NOT add a "Chapters:" / timestamp block (pipeline appends it)
6) Do NOT add an AI / synthetic-media disclosure footer (pipeline appends it)
Never claim the script is AI-written. The script is original and human-written.
Do not invent legal claims beyond ordinary YouTube packaging.

TRIPLE KEYWORD RULES (strict — most important):
- Choose 3–5 core keywords/phrases for this video (e.g. topic names, dynasty, what-if phrase).
- Put those SAME keywords into ALL three places:
  1) TITLE — include the triple keywords naturally in the title
  2) DESCRIPTION — repeat the same triple keywords early in the description (first 1–2 sentences) and again in hashtags if useful
  3) TAGS / normal keywords — every triple keyword MUST also appear as its own tag (exact or very close phrasing)
- Do not invent different keyword sets for title vs description vs tags — they must match.

TAG RULES (strict):
- Maximum 25 tags total.
- Combined character length of ALL tags (joined with commas) MUST be ≤ 500 characters.
- Prefer specific, searchable history / what-if / dynasty / Tudor / alternate-history terms.
- Avoid weak filler: gallery, AI assisted, random single words with no search intent.
- First tags should be the triple_keywords, then supporting discovery keywords.
- No duplicate / near-duplicate tags.
"""
    system = (
        "You are a YouTube SEO strategist for history documentary channels. "
        "Maximize CTR without clickbait lies. Titles should feel like the seed example. "
        "Always use the SAME triple keywords in title, description, and tags. "
        "Use one shared description structure for all channels (hook, body, CTA, hashtags). "
        "Never claim the script is AI-written. "
        "Tags must obey the 25-tag and 500-character budget exactly."
    )
    data = client.chat_json(system=system, user=user, temperature=0.8)

    title = str(data.get("title") or script.title).strip()[:100]
    description = str(data.get("description") or "").strip()
    tags_raw = data.get("tags") or {}
    triple_raw = data.get("triple_keywords") or data.get("keywords") or []

    if not title:
        raise OpenRouterError("OpenRouter returned empty title")
    if not description:
        description = _fallback_description(script)

    from src.services.youtube_meta import flatten_youtube_tags, normalize_youtube_tags

    triple = []
    if isinstance(triple_raw, list):
        triple = [str(t).strip() for t in triple_raw if str(t).strip()]
    elif isinstance(triple_raw, str) and triple_raw.strip():
        triple = [triple_raw.strip()]

    try:
        flat_tags = flatten_youtube_tags(tags_raw)
    except Exception:  # noqa: BLE001
        flat_tags = ["history", "documentary", "alternate history", "what if history"]

    # Guarantee triple keywords appear first in tags (title/desc already instructed).
    flat_tags = normalize_youtube_tags([*triple, *flat_tags])

    title_path.write_text(title + "\n", encoding="utf-8")
    desc_path.write_text(description + "\n", encoding="utf-8")
    tags_path.write_text(
        json.dumps(flat_tags, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (meta_dir / "triple_keywords.json").write_text(
        json.dumps(triple[:5], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _fallback_description(script: ScriptResult) -> str:
    """Shared napstorian / napping_historian description skeleton (no disclosure)."""
    hook = (script.hook or script.title or "").strip()
    title = (script.title or "").strip()
    lines: list[str] = []
    if hook:
        lines.append(hook)
    if title and title.lower() != hook.lower():
        lines.append(title)
    lines.extend(
        [
            "",
            "Explore this alternate-history documentary and decide where the timeline "
            "really turns.",
            "",
            "If you enjoy grounded what-if history, subscribe and share your "
            "counterfactual in the comments.",
        ]
    )
    return "\n".join(lines).strip()


def _ensure_description_disclosure(meta_dir: Path, settings: Settings) -> None:
    """Append the shared AI disclosure footer to description.txt when present."""
    from src.services.youtube_meta import append_youtube_ai_disclosure

    desc_path = meta_dir / "description.txt"
    if not desc_path.exists():
        return
    body = desc_path.read_text(encoding="utf-8")
    updated = append_youtube_ai_disclosure(
        body,
        settings.youtube_ai_disclosure_text,
    )
    if updated != body:
        desc_path.write_text(updated if updated.endswith("\n") else updated + "\n", encoding="utf-8")


def _looks_visual(text: str) -> bool:
    lower = text.lower()
    return any(
        m in lower
        for m in (
            "final visual",
            "slow zoom",
            "camera",
            "shot should",
            "national portrait gallery",
            "wide shot",
            "close-up",
        )
    )


def _write_chapter_timestamps(
    job_dir: Path,
    script: ScriptResult,
    meta_dir: Path,
    warnings: list[str],
) -> None:
    """Compute YouTube chapter timestamps from voice_manifest when available."""
    voice_manifest = job_dir / "audio" / "voice_manifest.json"
    if not voice_manifest.exists():
        warnings.append("voice_manifest.json missing — skipped chapter timestamps")
        return

    try:
        raw = json.loads(voice_manifest.read_text(encoding="utf-8"))
        durations_by_index = {
            int(s["index"]): float(s["duration_s"]) for s in raw.get("scenes", [])
        }
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        warnings.append(f"could not parse voice_manifest ({exc})")
        return

    chapter_titles = {ch.id: ch.title for ch in script.outline.chapters}
    chapter_durations: dict[int, float] = {}
    chapter_order: list[int] = []
    for scene in script.scenes:
        cid = scene.chapter_id or 0
        if cid not in chapter_durations:
            chapter_durations[cid] = 0.0
            chapter_order.append(cid)
        chapter_durations[cid] += durations_by_index.get(scene.index, 0.0)

    lines: list[str] = []
    elapsed = 0.0
    for cid in chapter_order:
        title = chapter_titles.get(cid, f"Chapter {cid}")
        lines.append(f"{_format_youtube_timestamp(elapsed)} {title}")
        elapsed += chapter_durations.get(cid, 0.0)

    chapters_path = meta_dir / "chapters.txt"
    chapters_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    pinned = _engagement_question(script)
    pinned_path = meta_dir / "pinned_comment.txt"
    pinned_path.write_text(pinned + "\n", encoding="utf-8")

    desc_path = meta_dir / "description.txt"
    if desc_path.exists():
        from src.services.youtube_meta import strip_youtube_ai_disclosure_footer

        body = strip_youtube_ai_disclosure_footer(
            _strip_duplicate_chapter_blocks(desc_path.read_text(encoding="utf-8"))
        )
        hook_lines = _ctr_hook_lines(script)
        chapter_block = "Chapters:\n" + "\n".join(lines) if lines else ""
        parts = [hook_lines]
        if body and body not in hook_lines:
            parts.append(body)
        if chapter_block:
            parts.append(chapter_block)
        desc_path.write_text("\n\n".join(p for p in parts if p).strip() + "\n", encoding="utf-8")
    elif lines:
        hook = _ctr_hook_lines(script)
        block = "Chapters:\n" + "\n".join(lines)
        desc_path.write_text(f"{hook}\n\n{block}\n", encoding="utf-8")


def _ctr_hook_lines(script: ScriptResult) -> str:
    """First two lines = CTR hook for YouTube description."""
    hook = (script.hook or "").strip()
    title = (script.title or script.topic or "").strip()
    if hook and title and hook.lower() != title.lower():
        return f"{hook}\n{title}"
    return hook or title


def _strip_duplicate_chapter_blocks(text: str) -> str:
    """Remove emoji/duplicate chapter lists; keep prose body only."""
    lines = text.splitlines()
    out: list[str] = []
    skip = False
    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith("chapters:") or lower.startswith("timestamps:"):
            skip = True
            continue
        if skip:
            if re.match(r"^(\d{1,2}:)?\d{1,2}:\d{2}\s", stripped):
                continue
            if stripped.startswith("-") and ("—" in stripped or "–" in stripped):
                continue
            if stripped.startswith("🔹") or stripped.startswith("▶"):
                continue
            if not stripped:
                continue
            skip = False
        out.append(line)
    return "\n".join(out).strip()


def _format_youtube_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _engagement_question(script: ScriptResult) -> str:
    """Build a pinned-comment engagement question from outline."""
    chapters = script.outline.chapters
    if chapters:
        last = chapters[-1]
        if last.key_points:
            return str(last.key_points[-1]).strip()
        if last.goal:
            return f"What do you think — {last.goal.rstrip('?')}?"
    closer = (script.outline.closer or "").strip()
    if closer and not _looks_visual(closer):
        return closer if closer.endswith("?") else closer + " Share your theory below."
    hook = (script.hook or script.topic or "").strip()
    return f"What if {hook.rstrip('.')}? Drop your take in the comments."
