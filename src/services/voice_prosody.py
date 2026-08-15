"""Per-scene Kokoro prosody: speed + trailing silence for human rhythm."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.domain.models import Scene


@dataclass(frozen=True)
class ProsodySettings:
    speed: float
    pause_after_ms: int
    is_chapter_end: bool = False


def prosody_for_scene(
    scene: Scene,
    *,
    base_speed: float = 0.90,
    next_chapter_id: int | None = None,
    chapter_end_pause_ms: int = 200,
) -> ProsodySettings:
    """Map scene metadata to Kokoro speed and post-pause."""
    phase = (scene.pacing_phase or "a").lower()
    ch = scene.chapter_id or 0
    text = scene.text or ""
    beat = (getattr(scene, "beat_type", None) or "").lower()

    speed = float(base_speed)
    pause_ms = 80

    if ch == 1:
        speed = min(1.0, base_speed + 0.05)
        pause_ms = 150
    elif phase == "b":
        speed = max(0.85, base_speed)
        pause_ms = 120
    else:
        speed = max(0.88, base_speed + 0.02)

    interruptish = beat in ("interrupt", "reengage") or "!" in text
    ellipsis = "..." in text

    chapter_end = next_chapter_id is not None and next_chapter_id != ch
    if chapter_end:
        # Cap stacking: chapter boundary alone — do not pile interrupt/`...` on top.
        pause_ms = max(80, int(chapter_end_pause_ms))
    elif beat == "reengage":
        # Light documentary ask — small breath, not a hard stop.
        speed = max(0.85, speed - 0.02)
        pause_ms = max(pause_ms, 150)
    elif interruptish:
        speed = max(0.85, speed - 0.04)
        pause_ms = 200
        if ellipsis:
            pause_ms = max(pause_ms, 200 if phase == "b" else 150)
    elif ellipsis:
        pause_ms = max(pause_ms, 200 if phase == "b" else 150)

    return ProsodySettings(
        speed=round(speed, 3),
        pause_after_ms=pause_ms,
        is_chapter_end=chapter_end,
    )


def append_silence_to_wav(wav_path: Path, *, pause_ms: int) -> None:
    """Pad trailing silence onto a WAV using ffmpeg (in-place via temp)."""
    if pause_ms <= 0:
        return
    pause_s = pause_ms / 1000.0
    tmp = wav_path.with_suffix(".pause.wav")
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(wav_path),
        "-af",
        f"apad=pad_dur={pause_s:.3f}",
        str(tmp),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    tmp.replace(wav_path)


def strip_ellipsis_for_tts(text: str) -> str:
    """Normalize ellipses for cleaner Kokoro delivery."""
    t = re.sub(r"\.{3,}", ", ", text)
    t = re.sub(r"\s+", " ", t).strip()
    return t
