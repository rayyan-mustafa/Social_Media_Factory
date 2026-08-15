"""Mux Weird Biology stickman timeline + full VO → final.mp4 (no history overlays)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.services.weird_biology_compositor import (
    is_compositor_backend,
    render_beat_clip_compositor,
    visual_backend_from_config,
)
from src.services.weird_biology_scenes import VisualTimeline
from src.services.weird_biology_stickman import ShotSpec, render_beat_clip
from src.services.weird_biology_style import load_visual_config


class WeirdBiologyComposeError(RuntimeError):
    pass


@dataclass
class ComposeResult:
    final_path: Path
    silent_video: Path
    meta: dict[str, Any]


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise WeirdBiologyComposeError(
            f"cmd failed ({proc.returncode}): {' '.join(cmd[:6])}…\n"
            f"{(proc.stderr or proc.stdout)[-800:]}"
        )


def _render_beat_clip(
    shot: ShotSpec,
    out: Path,
    *,
    duration_s: float,
    visual_backend: str | None = None,
) -> Path:
    backend = visual_backend or visual_backend_from_config()
    if is_compositor_backend(backend):
        return render_beat_clip_compositor(shot, out, duration_s=duration_s)
    return render_beat_clip(shot, out, duration_s=duration_s)


def render_all_beats(
    tl: VisualTimeline,
    shots: list[ShotSpec],
    beats_dir: Path,
    *,
    resume: bool = True,
    visual_backend: str | None = None,
) -> list[Path]:
    beats_dir = Path(beats_dir)
    beats_dir.mkdir(parents=True, exist_ok=True)
    if len(shots) != len(tl.beats):
        raise WeirdBiologyComposeError(
            f"shots ({len(shots)}) != beats ({len(tl.beats)})"
        )
    paths: list[Path] = []
    for beat, shot in zip(tl.beats, shots):
        out = beats_dir / f"beat_{beat.index:03d}.mp4"
        if resume and out.exists() and out.stat().st_size > 1000:
            paths.append(out)
            continue
        _render_beat_clip(
            shot, out, duration_s=beat.duration_s, visual_backend=visual_backend
        )
        paths.append(out)
    return paths


def concat_beats_silent(beat_paths: list[Path], out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lst = out_path.parent / "beats_concat.txt"
    lines = []
    for p in beat_paths:
        # ffmpeg concat demuxer requires escaped quotes
        ap = str(p.resolve()).replace("'", "'\\''")
        lines.append(f"file '{ap}'")
    lst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(lst),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
    )
    return out_path


def mux_full_vo(
    silent_video: Path,
    full_vo_wav: Path,
    final_path: Path,
) -> Path:
    final_path = Path(final_path)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = load_visual_config()
    W = int(cfg.get("width") or 1280)
    H = int(cfg.get("height") or 720)
    # Scale/pad + shortest: audio drives length if video slightly off
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(silent_video),
            "-i",
            str(full_vo_wav),
            "-filter_complex",
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[v]",
            "-map",
            "[v]",
            "-map",
            "1:a",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(final_path),
        ]
    )
    return final_path


def compose_weird_biology(
    *,
    tl: VisualTimeline,
    shots: list[ShotSpec],
    full_vo_wav: Path,
    job_dir: Path,
    resume: bool = True,
    visual_backend: str | None = None,
) -> ComposeResult:
    job_dir = Path(job_dir)
    beats_dir = job_dir / "beats"
    video_dir = job_dir / "video"
    video_dir.mkdir(parents=True, exist_ok=True)

    backend = visual_backend or visual_backend_from_config()
    beat_paths = render_all_beats(
        tl, shots, beats_dir, resume=resume, visual_backend=backend
    )
    silent = video_dir / "silent_timeline.mp4"
    if not (resume and silent.exists() and silent.stat().st_size > 1000):
        concat_beats_silent(beat_paths, silent)

    final = video_dir / "final.mp4"
    mux_full_vo(silent, Path(full_vo_wav), final)

    meta = {
        "final": str(final),
        "silent_video": str(silent),
        "full_vo": str(full_vo_wav),
        "beat_count": len(beat_paths),
        "full_vo_duration_s": tl.full_vo_duration_s,
        "overlays": "none",
        "compose": "weird_biology_timeline_mux",
        "visual_backend": backend,
    }
    (video_dir / "compose_meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    return ComposeResult(final_path=final, silent_video=silent, meta=meta)
