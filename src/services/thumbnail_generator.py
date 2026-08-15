"""Thumbnail variants from video frames + hook text overlays (A/B ready)."""

from __future__ import annotations

import json
import logging
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.store import OPS_DIR

log = logging.getLogger(__name__)

THUMBNAIL_LOG = OPS_DIR / "thumbnail_log.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _contrast_score(img: Any) -> float:
    import numpy as np

    arr = np.asarray(img.convert("L"), dtype="float32")
    return float(arr.std())


def _blur_ok(img: Any, *, min_var: float = 40.0) -> bool:
    import numpy as np

    arr = np.asarray(img.convert("L"), dtype="float32")
    # Laplacian-ish via second differences
    gy, gx = np.gradient(arr)
    lap = np.gradient(gx)[1] + np.gradient(gy)[0]
    return float(lap.var()) >= min_var


def _face_detected(img: Any) -> bool:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return False
    gray = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    cascade = cv2.CascadeClassifier(cascade_path)
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(60, 60))
    return len(faces) > 0


def _face_bbox(img: Any) -> tuple[int, int, int, int] | None:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    gray = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    cascade = cv2.CascadeClassifier(cascade_path)
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(60, 60))
    if len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    return int(x), int(y), int(w), int(h)


def extract_candidate_frames(
    video_path: str | Path,
    output_dir: str | Path,
    *,
    scene_threshold: float = 0.3,
    max_frames: int = 12,
) -> list[Path]:
    output_dir = Path(output_dir)
    cand = output_dir / "candidates"
    cand.mkdir(parents=True, exist_ok=True)
    pattern = str(cand / "frame_%03d.jpg")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"select='gt(scene,{scene_threshold})'",
        "-vsync",
        "vfr",
        "-frames:v",
        str(max_frames),
        pattern,
    ]
    try:
        subprocess.run(cmd, check=False, capture_output=True, timeout=180)
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("ffmpeg scene extract failed: %s", exc)

    frames = sorted(cand.glob("frame_*.jpg"))
    if frames:
        return frames[:max_frames]

    # Fallback: evenly spaced stills
    for i, t in enumerate((5, 15, 30, 60, 90)):
        out = cand / f"fallback_{i:03d}.jpg"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                str(t),
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                str(out),
            ],
            check=False,
            capture_output=True,
            timeout=60,
        )
    return sorted(cand.glob("*.jpg"))[:max_frames]


def _apply_vignette(img: Any) -> Any:
    from PIL import Image, ImageEnhance
    import numpy as np

    img = ImageEnhance.Contrast(img).enhance(1.15)
    img = ImageEnhance.Color(img).enhance(1.1)
    w, h = img.size
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = w / 2.0, h / 2.0
    r = math.sqrt(cx * cx + cy * cy) or 1.0
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / r
    factor = np.clip(1.0 - 0.55 * (dist**2), 0.35, 1.0)
    arr = np.asarray(img).astype("float32")
    arr *= factor[..., None]
    return Image.fromarray(arr.astype("uint8"))


def _draw_text(img: Any, text: str, *, face_box: tuple[int, int, int, int] | None, style: dict[str, Any] | None = None) -> Any:
    from PIL import ImageDraw, ImageFont

    style = style or {}
    words = text.strip().split()
    word_max = int(style.get("word_count_max") or 5)
    if len(words) > word_max:
        text = " ".join(words[:word_max])
    draw = ImageDraw.Draw(img)
    w, h = img.size
    font_size = max(36, w // 18)
    font = None
    for name in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        try:
            font = ImageFont.truetype(name, font_size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    # Place opposite face
    y = int(h * 0.72)
    if face_box:
        _fx, fy, _fw, fh = face_box
        face_cy = fy + fh / 2
        y = int(h * 0.12) if face_cy > h / 2 else int(h * 0.75)

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = max(10, (w - tw) // 2)
    mood = str(style.get("mood") or "")
    text_color = str(style.get("text_color") or "yellow")
    fill = "yellow" if text_color == "yellow" else "#f0ece4"
    if mood == "moody_calm" or style.get("preset") == "historian_soft":
        fill = "#e8e4dc"
    stroke_w = 2 if mood == "moody_calm" else 3
    for dx in range(-stroke_w, stroke_w + 1):
        for dy in range(-stroke_w, stroke_w + 1):
            if dx == 0 and dy == 0:
                continue
            draw.text((x + dx, y + dy), text, font=font, fill="black")
    draw.text((x, y), text, font=font, fill=fill)
    return img


def composite_thumbnail(
    frame_path: Path,
    hook_text: str,
    out_path: Path,
    *,
    grade: float = 1.0,
    thumb_style: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from PIL import Image, ImageEnhance

    style = thumb_style or {}
    img = Image.open(frame_path).convert("RGB")
    face = _face_detected(img)
    face_box = _face_bbox(img) if face else None
    contrast = _contrast_score(img)
    if not _blur_ok(img) and contrast < 25:
        raise ValueError("frame too blurry/dark")
    img = _apply_vignette(img)
    boost = float(style.get("contrast_boost") or 1.0)
    grade = grade * boost
    if grade != 1.0:
        img = ImageEnhance.Color(img).enhance(grade)
        img = ImageEnhance.Contrast(img).enhance(grade)
    img = _draw_text(img, hook_text, face_box=face_box, style=style)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, quality=92)
    return {
        "variant_path": str(out_path),
        "source_frame": str(frame_path),
        "hook_text_used": " ".join(hook_text.split()[:5]),
        "face_detected": face,
        "contrast_score": contrast,
    }


def generate_thumbnails(
    video_path: str,
    hook_text_options: list[str],
    output_dir: str,
    num_variants: int = 3,
    *,
    channel: str | None = None,
) -> list[dict[str, Any]]:
    video_path_p = Path(video_path)
    output_dir_p = Path(output_dir)
    variants_dir = output_dir_p / "variants"
    variants_dir.mkdir(parents=True, exist_ok=True)

    thumb_style: dict[str, Any] = {}
    try:
        from src.services.editing_overrides import thumbnail_style_preset

        thumb_style = thumbnail_style_preset(channel)
    except Exception:  # noqa: BLE001
        pass

    frames = extract_candidate_frames(video_path_p, output_dir_p)
    if not frames:
        return []

    # Score frames
    scored: list[tuple[float, Path, bool]] = []
    from PIL import Image

    for fp in frames:
        try:
            im = Image.open(fp).convert("RGB")
            if not _blur_ok(im):
                continue
            c = _contrast_score(im)
            face = _face_detected(im)
            score = c + (40.0 if face else 0.0)
            scored.append((score, fp, face))
        except Exception:  # noqa: BLE001
            continue
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return []

    texts = [t.strip() for t in hook_text_options if str(t).strip()] or ["WHAT IF"]
    grades = [1.0, 1.15, 0.9]
    picks: list[dict[str, Any]] = []
    used_frames: set[str] = set()
    used_texts: set[str] = set()
    n = max(1, min(int(num_variants), 6))
    i = 0
    for score, fp, _face in scored:
        if len(picks) >= n:
            break
        text = texts[i % len(texts)]
        # Force diversity
        if str(fp) in used_frames and len(scored) > len(picks):
            i += 1
            continue
        if text in used_texts and len(texts) > 1:
            text = texts[(i + 1) % len(texts)]
        grade = grades[i % len(grades)]
        out = variants_dir / f"thumb_{fp.stem}_{i}_{grade:.2f}.jpg"
        try:
            meta = composite_thumbnail(fp, text, out, grade=grade, thumb_style=thumb_style)
            meta["thumb_style_preset"] = thumb_style.get("preset")
            meta["channel"] = channel
            picks.append(meta)
            used_frames.add(str(fp))
            used_texts.add(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("thumb composite skip: %s", exc)
        i += 1
    return picks


def log_published_variant(
    video_id: str,
    variant_path: str,
    *,
    channel: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    row = {
        "ts": _now(),
        "video_id": video_id,
        "variant_path": variant_path,
        "channel": channel,
        **(extra or {}),
    }
    _append_jsonl(THUMBNAIL_LOG, row)


def record_ctr_result(video_id: str, variant_path: str, ctr: float) -> None:
    """Placeholder + log for CTR feedback into SMM works memory."""
    _append_jsonl(
        THUMBNAIL_LOG,
        {
            "ts": _now(),
            "event": "ctr_result",
            "video_id": video_id,
            "variant_path": variant_path,
            "ctr": float(ctr),
        },
    )
