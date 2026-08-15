"""Gate A — technical auto-fail checks before YouTube upload (Plan C §9)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from src.domain.models import GateAResult, VisualResult
from src.services.retention_profile import profile_gate_a_band
from src.services.settings import get_settings


class GateAError(RuntimeError):
    pass


def run_gate_a(
    final_path: Path | str,
    *,
    visual_manifest: Path | str | None = None,
    enforce_duration: bool | None = None,
    allow_placeholders: bool = False,
) -> GateAResult:
    """Validate final.mp4 before private upload. Fail closed on hard errors."""
    s = get_settings()
    path = Path(final_path)
    errors: list[str] = []
    warnings: list[str] = []
    # Profile-driven band (epic ~80–100 min; longform 15–25 min). Env GATE_A_*
    # still applies when RETENTION_PROFILE has no gate_a_* and fallbacks match.
    gate_lo, gate_hi = profile_gate_a_band()

    if not path.exists():
        return GateAResult(ok=False, errors=[f"final missing: {path}"])

    size = path.stat().st_size
    if size < int(s.gate_a_min_file_bytes):
        errors.append(f"file too small ({size} B < {s.gate_a_min_file_bytes})")

    info = _probe(path)
    w, h = info.get("width"), info.get("height")
    expect_w, expect_h = int(s.image_width), int(s.image_height)
    if w != expect_w or h != expect_h:
        errors.append(f"aspect {w}x{h}, need {expect_w}x{expect_h}")
    if not info.get("has_video"):
        errors.append("no video stream")
    if not info.get("has_audio"):
        errors.append("no audio stream")

    dur = info.get("duration_s")
    do_dur = s.gate_a_enforce_duration if enforce_duration is None else enforce_duration
    if do_dur and dur is not None:
        if dur < gate_lo or dur > gate_hi:
            errors.append(
                f"duration {dur:.1f}s outside Gate A band "
                f"{gate_lo:.0f}–{gate_hi:.0f}s "
                "(use --allow-short for test uploads)"
            )
    elif dur is not None and (dur < gate_lo or dur > gate_hi):
        warnings.append(
            f"duration {dur:.1f}s outside production band "
            f"{gate_lo:.0f}–{gate_hi:.0f}s (skipped)"
        )

    placeholder = False
    if visual_manifest:
        vm_path = Path(visual_manifest)
        if vm_path.exists():
            visual = VisualResult.model_validate(
                json.loads(vm_path.read_text(encoding="utf-8"))
            )
            if visual.backend == "mock" or any(sc.placeholder for sc in visual.scenes):
                placeholder = True
                if not allow_placeholders:
                    errors.append(
                        "placeholder/mock stills present — refuse upload "
                        "(pass --allow-placeholders only for dry tests)"
                    )
                else:
                    warnings.append("placeholder/mock stills allowed by flag")

    return GateAResult(
        ok=not errors,
        width=w,
        height=h,
        has_video=bool(info.get("has_video")),
        has_audio=bool(info.get("has_audio")),
        duration_s=dur,
        file_size_bytes=size,
        placeholder_backend=placeholder,
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
