"""Audio bed mix must not truncate voice tails."""

from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from src.services.audio_bed import ASSETS_DIR, AudioBedModule, _wav_duration_sr
from src.services.settings import get_settings


def _write_tone_wav(
    path: Path, *, duration_s: float, sr: int = 24000, freq: float = 440.0
) -> None:
    n = int(sr * duration_s)
    t = np.arange(n, dtype=np.float32) / float(sr)
    # Non-silent throughout so truncation is measurable (not just trailing zeros).
    samples = (0.35 * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)
    # Ramp down last 50ms but keep audible energy until the end.
    tail = max(1, int(0.05 * sr))
    fade = np.linspace(1.0, 0.15, tail, dtype=np.float32)
    samples[-tail:] *= fade
    pcm = np.clip(samples * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


@pytest.fixture()
def bed_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AUDIO_BED_ENABLED", "true")
    get_settings.cache_clear()

    audio = tmp_path / "audio"
    audio.mkdir(parents=True)
    voice = audio / "scene_000.wav"
    _write_tone_wav(voice, duration_s=5.42)

    manifest = {
        "script_path": str(tmp_path / "script.json"),
        "title": "bed duration smoke",
        "topic": "bed duration smoke",
        "out_dir": str(audio.resolve()),
        "voice": "am_michael",
        "speed": 0.9,
        "lang": "en-us",
        "scenes": [
            {
                "index": 0,
                "text": "The Armada turned for home.",
                "path": str(voice.resolve()),
                "duration_s": 5.42,
                "sample_rate": 24000,
                "skipped": False,
            }
        ],
        "validation": {
            "ok": True,
            "scene_count": 1,
            "wav_count": 1,
            "total_duration_s": 5.42,
            "median_duration_s": 5.42,
            "min_duration_s": 5.42,
            "max_duration_s": 5.42,
            "target_seconds_per_scene": 8.0,
            "warnings": [],
            "errors": [],
        },
        "meta": {},
    }
    path = audio / "voice_manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def test_mix_scene_preserves_voice_duration(bed_job: Path):
    voice_wav = bed_job.parent / "scene_000.wav"
    voice_dur, _ = _wav_duration_sr(voice_wav)

    result = AudioBedModule().mix_voice_manifest(bed_job, resume=False)
    mixed = result.out_dir / "scene_000.wav"
    assert mixed.exists()
    mixed_dur, _ = _wav_duration_sr(mixed)

    # Mixed must not cut the voice tail (allow tiny mux jitter).
    assert mixed_dur + 0.05 >= voice_dur
    assert abs(mixed_dur - voice_dur) < 0.08


def test_mix_matches_armada_truncation_repro(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Synthetic stand-in for Armada scene_001 (was ~5.42s voice → ~4.18s mixed)."""
    monkeypatch.setenv("AUDIO_BED_ENABLED", "true")
    get_settings.cache_clear()

    voice = tmp_path / "voice.wav"
    out = tmp_path / "mixed.wav"
    _write_tone_wav(voice, duration_s=5.419333)

    mod = AudioBedModule()
    mod._ensure_default_assets()
    bed_wav = ASSETS_DIR / "ambient/tension_low.wav"
    assert bed_wav.exists()

    mod._mix_scene(voice_wav=voice, out_wav=out, bed_wav=bed_wav, sfx_wav=None)
    voice_dur, _ = _wav_duration_sr(voice)
    mixed_dur, _ = _wav_duration_sr(out)
    assert mixed_dur + 0.05 >= voice_dur
    # Old sidechaincompress path lost ~1.2s on this length.
    assert mixed_dur > voice_dur - 0.2
