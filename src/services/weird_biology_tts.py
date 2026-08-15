"""Whole-script Kokoro TTS for Weird Biology (single full_vo.wav)."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.services.tts_chunking import (
    DEFAULT_MAX_PHONEMES,
    pack_sentences_by_phoneme_limit,
)
from src.services.tts_sanitize import spell_numbers_for_vo

logger = logging.getLogger(__name__)

# Tiny gap between phoneme batches inside the single VO.
_BATCH_GAP_SAMPLES_FRAC = 0.05  # seconds of silence between batches


@dataclass
class FullVoResult:
    path: Path
    duration_s: float
    sample_rate: int
    text: str
    meta: dict[str, Any]


def ffprobe_duration(path: Path) -> float:
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
    out = subprocess.check_output(cmd, text=True).strip()
    return float(out)


def synthesize_full_vo(
    vo_raw: str,
    *,
    out_path: Path,
    voice: str | None = None,
    speed: float | None = None,
    resume: bool = True,
    max_phonemes: int = DEFAULT_MAX_PHONEMES,
) -> FullVoResult:
    """Synthesize entire VO to one wav (chunk internally only for phoneme cap)."""
    # Heavy deps live in .venv-kokoro — import lazily.
    import numpy as np
    import soundfile as sf

    from src.services.tts_kokoro import VoiceModule, VoiceModuleError, _wav_meta

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    text = spell_numbers_for_vo((vo_raw or "").strip())
    text = (text or "").strip()
    if not text:
        raise VoiceModuleError("empty vo_raw for full VO TTS")

    if resume and out_path.exists() and out_path.stat().st_size > 44:
        duration_s, sample_rate = _wav_meta(out_path)
        return FullVoResult(
            path=out_path,
            duration_s=duration_s,
            sample_rate=sample_rate,
            text=text,
            meta={"skipped": True, "path": str(out_path)},
        )

    vm = VoiceModule(voice=voice, speed=speed)
    if vm._tts_backend() != "kokoro":
        logger.warning(
            "TTS_BACKEND=%s — Weird Biology full VO expects kokoro",
            vm._tts_backend(),
        )

    engine = vm._ensure_engine()
    batches = pack_sentences_by_phoneme_limit(text, max_phonemes=max_phonemes)
    if not batches:
        raise VoiceModuleError("full VO: no phoneme batches")

    parts: list = []
    sample_rate = 24000
    gap = None
    for i, batch in enumerate(batches):
        samples, sr = engine.create(
            batch,
            voice=vm.voice,
            speed=vm.speed,
            lang=vm.lang,
            trim=False,
        )
        sample_rate = int(sr)
        arr = np.asarray(samples, dtype=np.float32)
        if arr.size == 0:
            raise VoiceModuleError(f"full VO batch {i} returned empty audio")
        parts.append(arr)
        if gap is None:
            gap = np.zeros(int(sample_rate * _BATCH_GAP_SAMPLES_FRAC), dtype=np.float32)
        if i < len(batches) - 1 and gap.size:
            parts.append(gap)

    audio = np.concatenate(parts)
    sf.write(str(out_path), audio, sample_rate)
    duration_s, sample_rate = _wav_meta(out_path)
    meta = {
        "skipped": False,
        "path": str(out_path.resolve()),
        "duration_s": duration_s,
        "sample_rate": sample_rate,
        "batch_count": len(batches),
        "voice": vm.voice,
        "speed": vm.speed,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "whole_vo",
    }
    manifest = out_path.parent / "full_vo_manifest.json"
    manifest.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    meta["manifest"] = str(manifest)
    return FullVoResult(
        path=out_path.resolve(),
        duration_s=duration_s,
        sample_rate=sample_rate,
        text=text,
        meta=meta,
    )
