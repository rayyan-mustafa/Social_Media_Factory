"""Gate S — technical auto-fail checks before YouTube Shorts upload.

Never run Gate A on Shorts (Gate A requires 1280×720 and the longform band).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from src.domain.models import GateAResult
from src.services.shorts_packager import HARD_MAX_S, MAX_S, MIN_S, SHORTS_H, SHORTS_W

# Alias: same shape as Gate A; callers stamp meta.gate = "S".
GateSResult = GateAResult

GATE_S_MIN_FILE_BYTES = 8_000
GATE_S_WIDTH = SHORTS_W
GATE_S_HEIGHT = SHORTS_H
GATE_S_MIN_S = MIN_S
GATE_S_MAX_S = MAX_S
GATE_S_HARD_MAX_S = HARD_MAX_S


class GateSError(RuntimeError):
    pass


def run_gate_s(
    final_path: Path | str,
    *,
    enforce_duration: bool = True,
) -> GateSResult:
    """Validate a 9:16 Short before upload. Fail closed on hard errors."""
    path = Path(final_path)
    errors: list[str] = []
    warnings: list[str] = []

    if not path.exists():
        return GateSResult(ok=False, errors=[f"short missing: {path}"])

    size = path.stat().st_size
    if size < GATE_S_MIN_FILE_BYTES:
        errors.append(f"file too small ({size} B < {GATE_S_MIN_FILE_BYTES})")

    info = _probe(path)
    w, h = info.get("width"), info.get("height")
    if w != GATE_S_WIDTH or h != GATE_S_HEIGHT:
        errors.append(f"aspect {w}x{h}, need {GATE_S_WIDTH}x{GATE_S_HEIGHT}")
    if not info.get("has_video"):
        errors.append("no video stream")
    if not info.get("has_audio"):
        errors.append("no audio stream")

    dur = info.get("duration_s")
    if dur is not None and dur > GATE_S_HARD_MAX_S:
        errors.append(f"duration {dur:.1f}s exceeds Shorts hard max {GATE_S_HARD_MAX_S:.0f}s")
    elif enforce_duration and dur is not None:
        if dur < GATE_S_MIN_S or dur > GATE_S_MAX_S:
            errors.append(
                f"duration {dur:.1f}s outside Gate S band "
                f"{GATE_S_MIN_S:.0f}–{GATE_S_MAX_S:.0f}s"
            )
    elif dur is not None and (dur < GATE_S_MIN_S or dur > GATE_S_MAX_S):
        warnings.append(
            f"duration {dur:.1f}s outside production band "
            f"{GATE_S_MIN_S:.0f}–{GATE_S_MAX_S:.0f}s (skipped)"
        )

    return GateSResult(
        ok=not errors,
        width=w,
        height=h,
        has_video=bool(info.get("has_video")),
        has_audio=bool(info.get("has_audio")),
        duration_s=dur,
        file_size_bytes=size,
        placeholder_backend=False,
        warnings=warnings,
        errors=errors,
    )


def _probe(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,width,height:format=duration",
        "-of",
        "json",
        str(path),
    ]
    data = json.loads(subprocess.check_output(cmd, text=True))
    width = height = None
    has_audio = has_video = False
    for st in data.get("streams") or []:
        if st.get("codec_type") == "video":
            has_video = True
            width = int(st.get("width") or 0) or width
            height = int(st.get("height") or 0) or height
        elif st.get("codec_type") == "audio":
            has_audio = True
    dur = None
    try:
        dur = float((data.get("format") or {}).get("duration") or 0) or None
    except (TypeError, ValueError):
        dur = None
    return {
        "width": width,
        "height": height,
        "has_audio": has_audio,
        "has_video": has_video,
        "duration_s": dur,
    }
