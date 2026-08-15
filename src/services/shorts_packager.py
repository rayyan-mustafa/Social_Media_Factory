"""Clip a 40s 9:16 YouTube Short from an owned longform ``final.mp4``.

No GPU. Scene-snapped window from ``voice_manifest.json``; default layout is
blur-pillar so 16:9 archival stills stay readable.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Literal

from src.services.settings import ROOT

TARGET_S = 40.0
MIN_S = 38.0
MAX_S = 45.0
HARD_MAX_S = 60.0
# Modest speed-up so Shorts feel faster than longform VO. Source window is
# TARGET_S * SPEED_FACTOR (~48s) so output still lands in the 38–45s Gate S band.
SPEED_FACTOR = 1.20
SHORTS_W = 1080
SHORTS_H = 1920
SHORTS_FPS = 30

Layout = Literal["blur_pillar", "center_crop"]


class ShortsPackagerError(RuntimeError):
    pass


def load_scene_durations(job_dir: Path | str) -> list[float]:
    """Read per-scene seconds from voice_manifest (mixed preferred)."""
    root = Path(job_dir)
    candidates = (
        root / "audio" / "mixed" / "voice_manifest_mixed.json",
        root / "audio" / "voice_manifest.json",
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        scenes = data.get("scenes") if isinstance(data, dict) else None
        if not isinstance(scenes, list):
            continue
        durs: list[float] = []
        for sc in scenes:
            if not isinstance(sc, dict):
                continue
            try:
                durs.append(float(sc.get("duration_s") or 0.0))
            except (TypeError, ValueError):
                durs.append(0.0)
        if durs:
            return durs
    return []


def atempo_chain(speed: float) -> str:
    """Build an ``atempo`` chain. Each stage must be in ``[0.5, 2.0]``."""
    s = float(speed)
    if s <= 0:
        return "atempo=1"
    parts: list[str] = []
    while s > 2.0 + 1e-9:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5 - 1e-9:
        parts.append("atempo=0.5")
        s /= 0.5
    parts.append(f"atempo={s:.4g}")
    return ",".join(parts)


def source_window_params(*, speed: float = SPEED_FACTOR) -> dict[str, float]:
    """Source-window seconds so output after ``speed`` lands in Gate S."""
    s = float(speed) if speed > 0 else 1.0
    return {
        "target_s": TARGET_S * s,
        "min_s": MIN_S * s,
        "max_s": MAX_S * s,
        "hard_max_s": HARD_MAX_S * s,
        "speed": s,
    }


def expected_output_s(source_duration_s: float, *, speed: float = SPEED_FACTOR) -> float:
    s = float(speed) if speed > 0 else 1.0
    return float(source_duration_s) / s


def select_window(
    scene_durations: list[float] | None,
    *,
    target_s: float = TARGET_S,
    min_s: float = MIN_S,
    max_s: float = MAX_S,
    hard_max_s: float = HARD_MAX_S,
    start_at_s: float = 0.0,
) -> tuple[float, float]:
    """Return ``(start_s, duration_s)`` snapped to complete scenes.

    Default starts at t=0 (cold open). ``start_at_s`` skips to the first scene
    boundary at/after that time, then accumulates ``target_s``.
    """
    target_s = float(target_s)
    min_s = float(min_s)
    max_s = float(max_s)
    hard_max_s = float(hard_max_s)
    durs = [max(0.0, float(d)) for d in (scene_durations or [])]
    offset = 0.0
    start_at_s = max(0.0, float(start_at_s))
    if start_at_s > 0 and durs:
        cum = 0.0
        idx = len(durs)
        for i, d in enumerate(durs):
            if cum + d > start_at_s + 1e-9:
                idx = i
                break
            cum += d
        else:
            return 0.0, min(hard_max_s, max(min_s, target_s))
        offset = cum
        durs = durs[idx:]
    if not durs:
        return round(offset, 3), min(hard_max_s, max(min_s, target_s))

    acc = 0.0
    end = target_s
    for d in durs:
        nxt = acc + d
        if nxt + 1e-9 >= target_s:
            if nxt <= max_s + 1e-9:
                end = nxt
            elif acc >= min_s - 1e-9:
                end = acc
            else:
                end = min(max_s, nxt)
            break
        acc = nxt
    else:
        end = acc if acc > 0 else target_s

    end = min(hard_max_s, end)
    if end > max_s:
        end = max_s
    if end < min_s:
        total = sum(durs)
        if total >= min_s:
            end = min(max_s, max(min_s, end if end > 0 else min_s))
        elif total > 0:
            end = min(hard_max_s, total)
    return round(offset, 3), round(float(end), 3)


def probe_media_duration_s(path: Path | str | None) -> float:
    """ffprobe format duration; 0 if missing or unreadable."""
    if not path:
        return 0.0
    p = Path(path)
    if not p.is_file() or p.stat().st_size < 1000:
        return 0.0
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(p),
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return max(0.0, float((out or "").strip() or 0.0))
    except (OSError, subprocess.CalledProcessError, TypeError, ValueError):
        return 0.0


def trailer_window_starts(
    scene_durations: list[float] | None,
    *,
    channel: str = "",
    media_duration_s: float | None = None,
) -> list[float]:
    """Cold-open plus extra trailers for long sleep/docs (historian).

    Prefer the published file length when voice_manifest and ``final.mp4`` disagree.
    """
    durs = [max(0.0, float(d)) for d in (scene_durations or [])]
    scene_total = sum(durs)
    total = float(media_duration_s or 0.0) or scene_total
    starts = [0.0]
    ch = (channel or "").strip().lower()
    long_doc = total >= 480.0 or "historian" in ch or "napping" in ch
    if long_doc and total >= 480.0:
        starts.append(total * 0.22)
    if long_doc and total >= 900.0:
        starts.append(total * 0.45)
    need = MIN_S * SPEED_FACTOR
    out: list[float] = []
    for s in starts:
        if total > 0 and s >= total - need:
            continue
        if out and abs(s - out[-1]) < 90:
            continue
        out.append(round(float(s), 3))
    return out or [0.0]


def blur_pillar_filter(*, width: int = SHORTS_W, height: int = SHORTS_H) -> str:
    """Keep full 16:9 frame centered on a blurred 9:16 canvas."""
    w, h = int(width), int(height)
    return (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},boxblur=20:5[bg];"
        f"[0:v]scale={w}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={SHORTS_FPS},format=yuv420p,setsar=1[v]"
    )


def center_crop_filter(*, width: int = SHORTS_W, height: int = SHORTS_H) -> str:
    w, h = int(width), int(height)
    return (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},fps={SHORTS_FPS},format=yuv420p,setsar=1[v]"
    )


def shorts_output_path(job_dir: Path | str, *, window_index: int = 0) -> Path:
    root = Path(job_dir)
    name = "short_40s.mp4" if int(window_index) <= 0 else f"short_40s_w{int(window_index)}.mp4"
    return root / "derivatives" / "shorts" / name


def resolve_final_mp4(
    job_dir: Path | str | None,
    *,
    channel: str | None = None,
    video_id: str | None = None,
) -> Path | None:
    if job_dir:
        root = Path(job_dir)
        for cand in (
            root / "video" / "final.mp4",
            root / "video_final_production" / "final.mp4",
            root / "video_live_head" / "final.mp4",
        ):
            if cand.is_file() and cand.stat().st_size > 1000:
                return cand
    vid = (video_id or "").strip()
    ch = (channel or "").strip()
    if vid and ch:
        live = ROOT / "output" / "live_vods" / ch / vid / "final.mp4"
        if live.is_file() and live.stat().st_size > 1000:
            return live
    return None


def _speed_filter_complex(vf: str, *, speed: float) -> tuple[str, list[str]]:
    """Inject setpts + atempo so video and audio play at ``speed``."""
    if abs(float(speed) - 1.0) < 1e-6:
        return vf, ["-map", "[v]", "-map", "0:a?"]
    marker = f"fps={SHORTS_FPS},format=yuv420p,setsar=1[v]"
    sped_v = vf.replace(
        marker,
        f"setpts=PTS/{float(speed):.4g},{marker}",
    )
    fc = f"{sped_v};[0:a]{atempo_chain(speed)}[a]"
    return fc, ["-map", "[v]", "-map", "[a]"]


def pack_short_clip(
    job_dir: Path | str,
    *,
    out_path: Path | str | None = None,
    layout: Layout = "blur_pillar",
    final_path: Path | str | None = None,
    duration_s: float | None = None,
    start_s: float | None = None,
    speed: float = SPEED_FACTOR,
) -> dict[str, Any]:
    """Encode one 9:16 Short from a job's longform final. Returns manifest dict.

    Selects a slightly longer source window so after ``speed`` (default 1.20x)
    the output still sits in the Gate S 38–45s band. Does not modify the parent.
    """
    root = Path(job_dir)
    src = Path(final_path) if final_path else resolve_final_mp4(root)
    if src is None or not src.is_file():
        raise ShortsPackagerError(f"final.mp4 missing under {root}")

    speed_f = float(speed) if float(speed) > 0 else 1.0
    src_p = source_window_params(speed=speed_f)
    media_s = probe_media_duration_s(src)
    scene_durs = load_scene_durations(root)
    scene_total = sum(scene_durs)
    mismatch = (
        media_s > 0
        and scene_total > 0
        and abs(scene_total - media_s) / max(media_s, 1.0) > 0.2
    )
    if start_s is None or duration_s is None:
        if mismatch:
            start_s = max(0.0, float(start_s or 0.0))
            remain = max(0.5, media_s - start_s) if media_s else src_p["target_s"]
            duration_s = min(src_p["target_s"], remain)
        else:
            win_start, win_dur = select_window(
                scene_durs,
                target_s=src_p["target_s"],
                min_s=src_p["min_s"],
                max_s=src_p["max_s"],
                hard_max_s=src_p["hard_max_s"],
                start_at_s=float(start_s or 0.0),
            )
            start_s = win_start
            duration_s = win_dur if duration_s is None else float(duration_s)
    start_s = max(0.0, float(start_s))
    duration_s = min(src_p["hard_max_s"], max(0.5, float(duration_s)))
    if media_s > 0 and start_s + duration_s > media_s:
        duration_s = max(0.5, media_s - start_s)
    # Prefer a source window that lands in the Gate S output band after speed-up.
    if duration_s < src_p["min_s"] and media_s > 0:
        duration_s = min(src_p["min_s"], max(0.5, media_s - start_s))
    if duration_s > src_p["max_s"]:
        duration_s = src_p["max_s"]

    dest = Path(out_path) if out_path else shorts_output_path(root)
    dest.parent.mkdir(parents=True, exist_ok=True)

    vf = (
        blur_pillar_filter()
        if layout != "center_crop"
        else center_crop_filter()
    )
    fc, map_args = _speed_filter_complex(vf, speed=speed_f)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{start_s:.3f}",
        "-t",
        f"{duration_s:.3f}",
        "-i",
        str(src),
        "-filter_complex",
        fc,
        *map_args,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size < 1000:
        err = (proc.stderr or proc.stdout or "")[-800:]
        raise ShortsPackagerError(f"ffmpeg shorts pack failed: {err}")

    out_dur = expected_output_s(duration_s, speed=speed_f)
    payload = {
        "ok": True,
        "job_dir": str(root.resolve()),
        "source": str(src.resolve()),
        "out_path": str(dest.resolve()),
        "start_s": start_s,
        "source_duration_s": duration_s,
        "duration_s": out_dur,
        "speed_factor": speed_f,
        "layout": layout,
        "width": SHORTS_W,
        "height": SHORTS_H,
        "fps": SHORTS_FPS,
        "bytes": dest.stat().st_size,
    }
    meta_path = dest.with_suffix(".json")
    meta_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    payload["meta_path"] = str(meta_path)
    return payload


def repo_root() -> Path:
    return ROOT
