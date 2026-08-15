"""P0 compose fixes: VO-length mux, overlay density scale, pulse window."""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from src.services.composer import EditModule
from src.services.settings import get_settings


def _silent_wav(path: Path, *, duration_s: float, sr: int = 24000) -> None:
    n = int(sr * duration_s)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(np.zeros(n, dtype=np.int16).tobytes())


def _probe_dur(path: Path) -> float:
    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float((r.stdout or "0").strip())


def test_overlay_card_budget_scales_for_short_vo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPOSE_INFOGRAPHIC_MIN_CARDS", "8")
    monkeypatch.setenv("COMPOSE_INFOGRAPHIC_MAX_CARDS", "20")
    monkeypatch.setenv("COMPOSE_INFOGRAPHIC_TARGET_INTERVAL_S", "75")
    get_settings.cache_clear()
    em = EditModule()
    lo, hi, interval = em._overlay_card_budget(60)
    assert lo == 1 and hi <= 3 and interval >= 60
    lo2, hi2, _ = em._overlay_card_budget(900)
    assert lo2 == 8 and hi2 == 20


def test_pulse_enable_bounded_to_card_window() -> None:
    import inspect

    src = inspect.getsource(EditModule._burn_premium_chrome)
    assert "between(t,0," in src
    assert "lt(mod(t" in src
    assert "enable='lt(mod(t\\,1.2)\\,0.55)'" not in src  # unbounded form removed


def test_mux_ai_clip_keeps_full_audio_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_WIDTH", "320")
    monkeypatch.setenv("IMAGE_HEIGHT", "180")
    get_settings.cache_clear()

    # Short motion clip (~2s) + long VO (~12s) — old -shortest chopped to ~2s.
    still = tmp_path / "frame.png"
    Image.new("RGB", (320, 180), color=(40, 80, 120)).save(still)
    short_vid = tmp_path / "motion.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-i",
            str(still),
            "-t",
            "2.0",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(short_vid),
        ],
        check=True,
    )
    wav = tmp_path / "vo.wav"
    _silent_wav(wav, duration_s=12.0)
    out = tmp_path / "muxed.mp4"

    EditModule()._mux_ai_clip(short_vid, wav, out, duration_s=12.0)
    dur = _probe_dur(out)
    assert dur >= 11.5, f"expected full VO length, got {dur}"
