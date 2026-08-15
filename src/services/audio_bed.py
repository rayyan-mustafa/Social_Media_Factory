"""AudioModule — ambient bed + sparse SFX under voice (Plan C retention Phase 3).

Channel tone:
  - napstorian: ambient bed + sparse keyword SFX (plus compose-time cinematic SFX)
  - napping_historian: soft ambient bed only (no keyword SFX hits) unless
    AUDIO_BED_SFX_HISTORIAN=1
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.domain.models import Scene, SceneAudio, ScriptResult, VoiceResult
from src.services.premium_overlays import resolve_channel
from src.services.settings import ROOT, get_settings

logger = logging.getLogger(__name__)

ASSETS_DIR = ROOT / "assets" / "audio"

# Chapter archetype → bed asset (relative to assets/audio)
_CHAPTER_MOOD: dict[int, str] = {
    1: "ambient/tension_low.wav",
    2: "ambient/tudor_chamber.wav",
    3: "ambient/tension_low.wav",
    4: "ambient/castle_rain.wav",
    5: "ambient/court_murmur_low.wav",
    6: "ambient/tension_low.wav",
    7: "ambient/castle_rain.wav",
    8: "ambient/tudor_chamber.wav",
    9: "ambient/hope_resolve.wav",
    10: "ambient/hope_resolve.wav",
}

_HISTORIAN_BED = "ambient/tudor_chamber.wav"

_SFX_KEYWORDS: list[tuple[str, str]] = [
    (r"\b(quill|letter|parchment|write)\b", "sfx/quill_scratch.wav"),
    (r"\b(door|chamber|gate)\b", "sfx/door_creak.wav"),
    (r"\b(horse|carriage|ride)\b", "sfx/horse_distant.wav"),
]


class AudioBedError(RuntimeError):
    pass


@dataclass
class AudioBedResult:
    voice_manifest: Path
    mixed_manifest: Path
    out_dir: Path
    scene_count: int
    meta: dict[str, Any]


class AudioBedModule:
    """Mix voice WAVs with chapter ambient bed + sparse SFX."""

    def __init__(self):
        get_settings.cache_clear()
        self.s = get_settings()
        self._ensure_default_assets()

    def mix_voice_manifest(
        self,
        voice_manifest: Path | str,
        *,
        script_path: Path | str | None = None,
        out_dir: Path | str | None = None,
        resume: bool = True,
        channel: str | None = None,
    ) -> AudioBedResult:
        if not self.s.audio_bed_enabled:
            vm = Path(voice_manifest)
            return AudioBedResult(
                voice_manifest=vm,
                mixed_manifest=vm,
                out_dir=vm.parent,
                scene_count=0,
                meta={"skipped": True, "reason": "audio_bed_disabled"},
            )

        voice_path = Path(voice_manifest)
        if not voice_path.exists():
            raise AudioBedError(f"voice manifest missing: {voice_path}")

        voice = VoiceResult.model_validate(
            json.loads(voice_path.read_text(encoding="utf-8"))
        )
        script: ScriptResult | None = None
        if script_path:
            sp = Path(script_path)
            if sp.exists():
                script = ScriptResult.model_validate(
                    json.loads(sp.read_text(encoding="utf-8"))
                )
        elif voice.script_path:
            sp = Path(voice.script_path)
            if sp.exists():
                script = ScriptResult.model_validate(
                    json.loads(sp.read_text(encoding="utf-8"))
                )

        script_raw: dict[str, Any] | None = None
        if script is not None:
            script_raw = script.model_dump()
        elif voice.script_path and Path(voice.script_path).exists():
            try:
                script_raw = json.loads(
                    Path(voice.script_path).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                script_raw = None

        ch = resolve_channel(
            script_raw,
            fallback=(
                channel
                or getattr(self.s, "compose_channel_default", None)
                or "napstorian"
            ),
        )
        historian = ch in {"napping_historian", "historian", "sleep"}
        # Historian: soft bed only; skip keyword SFX unless explicitly armed.
        allow_keyword_sfx = (not historian) or bool(
            getattr(self.s, "audio_bed_sfx_historian", False)
        )
        # Prefer competitor-style ambient_bed_db / score_mode when stamped.
        score_mode = "ambient_soft" if historian else "trailer_bed"
        bed_db = -32.0 if historian else -26.0
        try:
            from src.services.editing_overrides import (
                get_merged_channel_editing,
                score_mode_for_channel,
            )

            editing = get_merged_channel_editing(ch)
            audio_cfg = editing.get("audio") or {}
            if audio_cfg.get("ambient_bed_db") is not None:
                bed_db = float(audio_cfg["ambient_bed_db"])
            score_mode = score_mode_for_channel(ch) or score_mode
            if score_mode == "none":
                # Still mix a very quiet bed for continuity unless explicitly disabled later.
                bed_db = min(bed_db, -40.0)
        except Exception:  # noqa: BLE001
            logger.debug("score_mode resolve failed", exc_info=True)

        # Napstorian trailer bed: prefer assets/audio/trailer/* then ambient.
        trailer_dir = ASSETS_DIR / "trailer"
        ambient_dir = ASSETS_DIR / "ambient"
        trailer_beds = sorted(trailer_dir.glob("*.wav")) if trailer_dir.is_dir() else []
        ambient_beds = (
            sorted(ambient_dir.glob("*.wav")) if ambient_dir.is_dir() else []
        )

        scene_by_idx: dict[int, Scene] = {}
        if script:
            scene_by_idx = {s.index: s for s in script.scenes}

        mixed_dir = Path(out_dir) if out_dir else voice_path.parent / "mixed"
        mixed_dir.mkdir(parents=True, exist_ok=True)

        last_sfx_at = -999.0
        elapsed = 0.0
        mixed_scenes: list[SceneAudio] = []

        for sa in sorted(voice.scenes, key=lambda x: x.index):
            scene = scene_by_idx.get(sa.index)
            ch_id = (scene.chapter_id if scene else None) or 1
            wav_in = Path(sa.path)
            wav_out = mixed_dir / f"scene_{sa.index:03d}.wav"

            if resume and wav_out.exists() and wav_out.stat().st_size > 44:
                dur, sr = _wav_duration_sr(wav_out)
                mixed_scenes.append(
                    SceneAudio(
                        index=sa.index,
                        text=sa.text,
                        path=str(wav_out.resolve()),
                        duration_s=dur,
                        sample_rate=sr,
                        skipped=True,
                    )
                )
                elapsed += dur
                continue

            if score_mode == "trailer_bed" and trailer_beds:
                bed_path = trailer_beds[(ch_id - 1) % len(trailer_beds)]
            elif score_mode in {"ambient_soft", "trailer_bed"} and ambient_beds:
                # Continuous soft bed playlist (historian −32 dB path).
                if historian:
                    bed_path = ambient_beds[(ch_id - 1) % len(ambient_beds)]
                else:
                    bed_rel = _CHAPTER_MOOD.get(ch_id, "ambient/tudor_chamber.wav")
                    bed_path = ASSETS_DIR / bed_rel
            elif historian:
                bed_path = ASSETS_DIR / _HISTORIAN_BED
            else:
                bed_rel = _CHAPTER_MOOD.get(ch_id, "ambient/tudor_chamber.wav")
                bed_path = ASSETS_DIR / bed_rel
            # Historian never gets scream/keyword SFX on ambient_soft.
            if historian and score_mode == "ambient_soft":
                allow_keyword_sfx = False
            sfx_path = (
                _pick_sfx(scene, elapsed, last_sfx_at) if allow_keyword_sfx else None
            )
            if sfx_path:
                last_sfx_at = elapsed

            self._mix_scene(
                voice_wav=wav_in,
                out_wav=wav_out,
                bed_wav=bed_path,
                sfx_wav=sfx_path,
                bed_db=bed_db,
            )
            dur, sr = _wav_duration_sr(wav_out)
            mixed_scenes.append(
                SceneAudio(
                    index=sa.index,
                    text=sa.text,
                    path=str(wav_out.resolve()),
                    duration_s=dur,
                    sample_rate=sr,
                    skipped=False,
                )
            )
            elapsed += dur

        mixed_manifest = mixed_dir / "voice_manifest_mixed.json"
        mixed_voice = voice.model_copy(
            update={
                "out_dir": str(mixed_dir.resolve()),
                "scenes": mixed_scenes,
                "meta": {
                    **voice.meta,
                    "audio_bed": True,
                    "channel": ch,
                    "keyword_sfx": allow_keyword_sfx,
                    "bed_db": bed_db,
                    "score_mode": score_mode,
                    "mixed_at": datetime.now(timezone.utc).isoformat(),
                    "source_manifest": str(voice_path.resolve()),
                },
            }
        )
        mixed_manifest.write_text(
            json.dumps(mixed_voice.model_dump(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        return AudioBedResult(
            voice_manifest=voice_path,
            mixed_manifest=mixed_manifest,
            out_dir=mixed_dir,
            scene_count=len(mixed_scenes),
            meta={
                "audio_bed_enabled": True,
                "channel": ch,
                "keyword_sfx": allow_keyword_sfx,
                "bed_db": bed_db,
                "score_mode": score_mode,
                "assets_dir": str(ASSETS_DIR),
            },
        )

    def _mix_scene(
        self,
        *,
        voice_wav: Path,
        out_wav: Path,
        bed_wav: Path,
        sfx_wav: Path | None,
        bed_db: float = -20.0,
    ) -> None:
        if not voice_wav.exists():
            raise AudioBedError(f"voice wav missing: {voice_wav}")
        if not bed_wav.exists():
            self._ensure_default_assets()
        if not bed_wav.exists():
            raise AudioBedError(f"bed asset missing: {bed_wav}")

        dur, _ = _wav_duration_sr(voice_wav)
        dur = max(0.5, dur)

        # Voice 0 dB ref; bed under voice; optional SFX -12 dB.
        # Prefer amix over sidechaincompress: the latter (with aloop size=2e+09)
        # truncated scene tails by ~0.5–1.2s and clipped words before full stops.
        filters = [
            (
                f"[1:a]aloop=loop=-1:size=480000,atrim=0:{dur:.3f},"
                f"asetpts=N/SR/TB,volume={bed_db:.1f}dB[bed]"
            ),
            f"[0:a]apad=whole_dur={dur:.3f},asetpts=N/SR/TB[voice]",
            (
                "[voice][bed]amix=inputs=2:duration=first:"
                "dropout_transition=0:normalize=0[mixed]"
            ),
        ]
        map_label = "[mixed]"
        inputs = ["-i", str(voice_wav), "-i", str(bed_wav)]

        if sfx_wav and sfx_wav.exists():
            filters.append(
                f"[2:a]volume=-12dB,apad=whole_dur={dur:.3f},asetpts=N/SR/TB[sfx]"
            )
            filters.append(
                f"{map_label}[sfx]amix=inputs=2:duration=first:"
                "dropout_transition=0:normalize=0[out]"
            )
            map_label = "[out]"
            inputs.extend(["-i", str(sfx_wav)])

        fc = ";".join(filters)
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            *inputs,
            "-filter_complex",
            fc,
            "-map",
            map_label,
            "-t",
            f"{dur:.3f}",
            str(out_wav),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or "")[-600:]
            raise AudioBedError(f"ffmpeg mix failed: {err}") from exc

    def _ensure_default_assets(self) -> None:
        """Generate minimal royalty-free loops via ffmpeg if assets are missing."""
        specs: dict[str, tuple[str, float]] = {
            "ambient/tudor_chamber.wav": ("brown", 0.02),
            "ambient/castle_rain.wav": ("pink", 0.015),
            "ambient/court_murmur_low.wav": ("pink", 0.012),
            "ambient/tension_low.wav": ("brown", 0.018),
            "ambient/hope_resolve.wav": ("sine", 55.0),
            "sfx/quill_scratch.wav": ("sine", 800.0),
            "sfx/door_creak.wav": ("sine", 120.0),
            "sfx/horse_distant.wav": ("sine", 90.0),
        }
        for rel, (kind, param) in specs.items():
            path = ASSETS_DIR / rel
            if path.exists() and path.stat().st_size > 1000:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            dur = 8.0 if rel.startswith("ambient/") else 0.6
            if kind == "brown":
                src = f"anoisesrc=color=brown:amplitude={param}:duration={dur}"
            elif kind == "pink":
                src = f"anoisesrc=color=pink:amplitude={param}:duration={dur}"
            else:
                src = f"sine=frequency={param}:duration={dur}"
            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                src,
                "-ar",
                "24000",
                "-ac",
                "1",
                str(path),
            ]
            try:
                subprocess.run(cmd, check=True, capture_output=True)
            except subprocess.CalledProcessError as exc:
                logger.warning("could not generate asset %s: %s", rel, exc)


def _pick_sfx(
    scene: Scene | None, elapsed_s: float, last_sfx_at: float
) -> Path | None:
    """Max 1 SFX per 45s; hook chapter gets one in first 30s."""
    if scene is None:
        return None
    if elapsed_s - last_sfx_at < 45.0:
        return None
    text = (scene.text or "").lower()
    ch = scene.chapter_id or 0
    if ch == 1 and elapsed_s < 30.0:
        p = ASSETS_DIR / "sfx/quill_scratch.wav"
        return p if p.exists() else None
    for pattern, rel in _SFX_KEYWORDS:
        if re.search(pattern, text, re.IGNORECASE):
            p = ASSETS_DIR / rel
            if p.exists():
                return p
    return None


def _wav_duration_sr(path: Path) -> tuple[float, int]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=sample_rate",
        "-of",
        "json",
        str(path),
    ]
    raw = subprocess.check_output(cmd, text=True)
    data = json.loads(raw)
    dur = float((data.get("format") or {}).get("duration") or 0.0)
    sr = 24000
    for st in data.get("streams") or []:
        if st.get("sample_rate"):
            sr = int(st["sample_rate"])
            break
    return dur, sr
