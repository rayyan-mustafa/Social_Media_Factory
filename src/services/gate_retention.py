"""Gate R — retention quality checks before publish."""

from __future__ import annotations

import json
import re
import statistics
import subprocess
from pathlib import Path

from src.domain.models import GateRResult, ScriptResult
from src.services.script_validate import _VISUAL_MARKERS
from src.services.settings import get_settings


class GateRError(RuntimeError):
    pass


def run_gate_r(
    *,
    final_path: Path | str,
    script_path: Path | str | None = None,
    voice_manifest: Path | str | None = None,
    job_dir: Path | str | None = None,
    enforce: bool = True,
    dedupe_removed: int | None = None,
) -> GateRResult:
    """Retention gate: hook density, visual leaks, audio bed, loudness.

    Runtime length / retention-band checks were removed — longform cuts
    (e.g. 20+ min) must not HOLD publish. Duration is still probed for
    reporting only.
    """
    s = get_settings()
    errors: list[str] = []
    warnings: list[str] = []

    final = Path(final_path)
    runtime_s = _probe_duration(final) if final.exists() else None
    # Runtime length / retention-band checks removed — never HOLD on duration.

    script: ScriptResult | None = None
    if script_path and Path(script_path).exists():
        script = ScriptResult.model_validate(
            json.loads(Path(script_path).read_text(encoding="utf-8"))
        )

    hook_count: int | None = None
    visual_leak = 0
    dup_rate: float | None = None
    median_phase_a: float | None = None

    vm_path = _resolve_voice_manifest(voice_manifest, job_dir)
    durations_by_idx: dict[int, float] = {}
    if vm_path and vm_path.exists():
        try:
            raw = json.loads(vm_path.read_text(encoding="utf-8"))
            for sc in raw.get("scenes") or []:
                durations_by_idx[int(sc["index"])] = float(sc["duration_s"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            warnings.append(f"could not parse voice manifest: {vm_path}")

    if script:
        visual_leak = sum(
            1
            for sc in script.scenes
            if any(m in (sc.text or "").lower() for m in _VISUAL_MARKERS)
        )
        if visual_leak:
            msg = f"visual-leak in narration: {visual_leak} scenes"
            if enforce:
                errors.append(msg)
            else:
                warnings.append(msg)

        if dedupe_removed is None:
            dedupe_removed = int((script.meta or {}).get("dedupe_removed") or 0)
        total = len(script.scenes) + int(dedupe_removed or 0)
        if total > 0 and dedupe_removed is not None:
            dup_rate = float(dedupe_removed) / float(total)
            if dup_rate >= 0.05:
                msg = f"duplicate beat rate {dup_rate:.1%} (dedupe removed {dedupe_removed})"
                if enforce:
                    errors.append(msg)
                else:
                    warnings.append(msg)

        hook_scenes = [sc for sc in script.scenes if (sc.chapter_id or 0) == 1]
        hook_durs = [
            durations_by_idx.get(sc.index, 3.0) for sc in hook_scenes[:25]
        ]
        elapsed = 0.0
        hook_count = 0
        for d in hook_durs:
            if elapsed >= 60.0:
                break
            hook_count += 1
            elapsed += d
        if hook_count > 20:
            msg = f"hook first-60s scene count {hook_count} > 20"
            if enforce:
                errors.append(msg)
            else:
                warnings.append(msg)

        phase_a_durs = [
            durations_by_idx.get(sc.index, 3.0)
            for sc in script.scenes
            if (sc.pacing_phase or "a").lower() == "a"
        ]
        if phase_a_durs:
            median_phase_a = float(statistics.median(phase_a_durs))
            if median_phase_a < 2.5 or median_phase_a > 4.5:
                warnings.append(
                    f"median Phase A scene duration {median_phase_a:.2f}s "
                    "(target 2.5–4.5s)"
                )

    audio_bed_present = False
    if vm_path and vm_path.exists():
        try:
            raw = json.loads(vm_path.read_text(encoding="utf-8"))
            meta = raw.get("meta") or {}
            audio_bed_present = bool(meta.get("audio_bed")) or "mixed" in str(
                vm_path
            )
        except json.JSONDecodeError:
            pass
    if job_dir:
        mixed = Path(job_dir) / "audio" / "mixed" / "voice_manifest_mixed.json"
        if mixed.exists():
            audio_bed_present = True
    if s.audio_bed_enabled and not audio_bed_present:
        msg = "audio bed not present in voice manifest"
        if enforce:
            errors.append(msg)
        else:
            warnings.append(msg)

    loudness_lufs: float | None = None
    if final.exists():
        loudness_lufs = _measure_loudness(final)
        if loudness_lufs is not None and (
            loudness_lufs < -16.0 or loudness_lufs > -12.0
        ):
            warnings.append(
                f"integrated loudness {loudness_lufs:.1f} LUFS (target -16 to -12)"
            )

    ok = not errors
    return GateRResult(
        ok=ok,
        runtime_s=runtime_s,
        hook_scene_count=hook_count,
        duplicate_beat_rate=dup_rate,
        visual_leak_count=visual_leak,
        median_phase_a_duration_s=median_phase_a,
        audio_bed_present=audio_bed_present,
        loudness_lufs=loudness_lufs,
        warnings=warnings,
        errors=errors,
    )


def _resolve_voice_manifest(
    voice_manifest: Path | str | None, job_dir: Path | str | None
) -> Path | None:
    if voice_manifest:
        p = Path(voice_manifest)
        if p.exists():
            return p
    if job_dir:
        jd = Path(job_dir)
        for cand in (
            jd / "audio" / "mixed" / "voice_manifest_mixed.json",
            jd / "audio" / "voice_manifest.json",
        ):
            if cand.exists():
                return cand
    return None


def _probe_duration(path: Path) -> float | None:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        out = subprocess.check_output(cmd, text=True).strip()
        return float(out) if out else None
    except (subprocess.CalledProcessError, ValueError):
        return None


def _measure_loudness(path: Path) -> float | None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(path),
        "-af",
        "loudnorm=print_format=json",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        blob = proc.stderr or ""
        m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", blob, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group(0))
        return float(data.get("input_i", 0))
    except (json.JSONDecodeError, ValueError, subprocess.SubprocessError):
        return None
