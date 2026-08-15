"""Composer still-split smoke tests."""

from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from src.services.composer import EditModule
from src.services.settings import get_settings


def _write_silent_wav(path: Path, *, duration_s: float, sr: int = 24000) -> None:
    n = int(sr * duration_s)
    samples = np.zeros(n, dtype=np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(samples.tobytes())


def _write_still(path: Path, *, w: int = 1280, h: int = 720) -> None:
    img = Image.new("RGB", (w, h), color=(120, 80, 40))
    img.save(path, quality=90)


@pytest.fixture()
def long_scene_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COMPOSE_MAX_STILL_S", "10")
    monkeypatch.setenv("COMPOSE_DATE_OVERLAY_ENABLED", "true")
    monkeypatch.setenv("KEN_BURNS_ZOOM_END_PHASE_B", "1.10")
    # Keep this test on pure Ken Burns split — not PD/map motion / premium path.
    monkeypatch.setenv("COMPOSE_PREMIUM_OVERLAYS_ENABLED", "false")
    monkeypatch.setenv("COMPOSE_INFOGRAPHIC_OVERLAYS_ENABLED", "false")
    monkeypatch.setenv("COMPOSE_PULSE_MARKER_ENABLED", "false")
    get_settings.cache_clear()

    job = tmp_path / "job"
    images = job / "images"
    audio = job / "audio"
    video = job / "video"
    images.mkdir(parents=True)
    audio.mkdir(parents=True)
    video.mkdir(parents=True)

    still = images / "scene_000.jpg"
    wav = audio / "scene_000.wav"
    _write_still(still)
    _write_silent_wav(wav, duration_s=25.0)

    script = {
        "topic": "test",
        "title": "split still smoke",
        "hook": "",
        "outline": {
            "title": "split still smoke",
            "chapters": [
                {
                    "id": 1,
                    "title": "Hook",
                    "goal": "g",
                    "key_points": ["k"],
                    "target_sentences": 1,
                    "pacing_phase": "b",
                }
            ],
            "closer": "closer",
        },
        "scenes": [
            {
                "index": 0,
                "text": "In 1536 the court held its breath for twenty five seconds of narration.",
                "visual_prompt": "court still",
                "chapter_id": 1,
                "word_count": 14,
                "pacing_phase": "b",
                "beat_type": "",
                "target_duration_s": 25.0,
            }
        ],
        "validation": {
            "ok": True,
            "scene_count": 1,
            "min_scenes": 1,
            "max_scenes": 10,
            "target_scenes": 1,
            "estimated_duration_s": 25.0,
            "warnings": [],
            "errors": [],
        },
        "meta": {},
    }
    script_path = job / "script.json"
    script_path.write_text(json.dumps(script), encoding="utf-8")

    voice = {
        "title": "split still smoke",
        "topic": "test",
        "script_path": str(script_path),
        "voice": "am_michael",
        "speed": 0.9,
        "lang": "en-us",
        "out_dir": str(audio),
        "scenes": [
            {
                "index": 0,
                "text": script["scenes"][0]["text"],
                "path": str(wav),
                "duration_s": 25.0,
                "sample_rate": 24000,
                "skipped": False,
            }
        ],
        "validation": {
            "ok": True,
            "scene_count": 1,
            "wav_count": 1,
            "total_duration_s": 25.0,
            "median_duration_s": 25.0,
            "min_duration_s": 25.0,
            "max_duration_s": 25.0,
            "target_seconds_per_scene": 3.0,
            "warnings": [],
            "errors": [],
        },
        "meta": {},
    }
    visual = {
        "title": "split still smoke",
        "topic": "test",
        "script_path": str(script_path),
        "out_dir": str(images),
        "backend": "mock",
        "scenes": [
            {
                "index": 0,
                "visual_prompt": "court still",
                "path": str(still),
                "width": 1280,
                "height": 720,
                "retries_used": 0,
                "skipped": False,
                "backend": "mock",
                "placeholder": False,
                "video_path": None,
            }
        ],
        "validation": {
            "ok": True,
            "scene_count": 1,
            "image_count": 1,
            "width": 1280,
            "height": 720,
            "expected_count": 1,
            "placeholder_count": 0,
            "warnings": [],
            "errors": [],
        },
        "meta": {},
    }
    voice_path = audio / "voice_manifest.json"
    visual_path = images / "visual_manifest.json"
    voice_path.write_text(json.dumps(voice), encoding="utf-8")
    visual_path.write_text(json.dumps(visual), encoding="utf-8")
    return job


def test_long_still_splits_into_multiple_ken_burns(
    long_scene_job: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "src.services.map_motion.motion_mode_wants_maps",
        lambda _cfg: False,
    )
    monkeypatch.setattr(
        "src.services.motion_graphics.motion_mode_wants_graphics",
        lambda _cfg: False,
    )
    voice = long_scene_job / "audio" / "voice_manifest.json"
    visual = long_scene_job / "images" / "visual_manifest.json"
    out = long_scene_job / "video"

    edit = EditModule()
    # unit: duration splitter
    parts = edit._split_durations(25.0, 10.0)
    assert len(parts) == 3
    assert abs(sum(parts) - 25.0) < 1e-6
    assert all(p <= 10.0 + 1e-6 for p in parts)

    result = edit.compose(
        voice_manifest=voice,
        visual_manifest=visual,
        out_dir=out,
        resume=False,
        allow_placeholders=True,
    )
    assert result.validation.ok
    assert result.scenes[0].ken_burns.startswith("split3:")
    assert Path(result.final_path).exists()
    # final ~25s
    assert 24.0 <= float(result.validation.duration_s or 0) <= 26.5

    # year overlay path exercised (1536) — clip encodes cleanly
    clip = Path(result.scenes[0].clip_path)
    probe = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(clip),
        ],
        text=True,
    )
    dur = float(json.loads(probe)["format"]["duration"])
    assert 24.0 <= dur <= 26.5
