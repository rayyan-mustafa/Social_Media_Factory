"""Unit tests for voice WPM + dual-pacing math (foundation / epic)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.services.pacing import load_dual_pacing
from src.services.retention_profile import (
    load_retention_profiles,
    profile_gate_a_band,
    profile_gate_r_band,
)
from src.services.voice_wpm import (
    VoiceWpmRecord,
    count_words,
    load_voice_wpm,
    phase_b_words_from_wpm,
    save_voice_wpm,
    wpm_from_words_duration,
)


def test_wpm_from_words_duration():
    assert wpm_from_words_duration(150, 60.0) == 150.0
    assert abs(wpm_from_words_duration(177, 60.0) - 177.0) < 1e-9
    assert abs(wpm_from_words_duration(354, 120.0) - 177.0) < 1e-9


def test_phase_b_words_from_wpm():
    assert phase_b_words_from_wpm(177.0, 60.0) == 177
    assert phase_b_words_from_wpm(177.0, 120.0) == 354
    assert phase_b_words_from_wpm(160.92, 120.0) == 322


def test_count_words():
    assert count_words("one two three") == 3
    assert count_words("  spaced   out  ") == 2


def test_load_voice_wpm_from_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "voice_wpm.json"
    save_voice_wpm(
        VoiceWpmRecord(
            voice="am_michael",
            speed=1.0,
            wpm=177.0,
            measured_at="2026-01-01T00:00:00+00:00",
            words=177,
            duration_s=60.0,
            source="test",
        ),
        path=path,
    )
    monkeypatch.setenv("KOKORO_VOICE", "am_michael")
    monkeypatch.setenv("KOKORO_SPEED", "1.0")
    # Bypass cached Settings if any — load_voice_wpm reads get_settings()
    from src.services.settings import get_settings

    get_settings.cache_clear()
    assert load_voice_wpm(path=path, voice="am_michael", speed=1.0) == 177.0
    assert load_voice_wpm(path=tmp_path / "missing.json", default=150.0) == 150.0
    get_settings.cache_clear()


def test_load_dual_pacing_uses_measured_wpm():
    cfg = {
        "dual_pacing": True,
        "target_duration_min": 20,
        "phase_a_max_words": 1500,
        "phase_a_seconds_per_scene": 3,
        "phase_a_words_min": 6,
        "phase_a_words_max": 12,
        "phase_a_target_scenes": 200,
        "phase_b_seconds_per_scene": 60,
        "phase_b_target_scenes": 10,
        "target_scenes": 210,
        "voice_wpm": 177.0,
    }
    p = load_dual_pacing(cfg, settings_target_scenes=210)
    assert p.voice_wpm == 177.0
    assert p.phase_b_words == 177  # round(177 * 60/60)
    assert p.phase_b_words_min <= p.phase_b_words <= p.phase_b_words_max
    assert p.phase_b_target_scenes == 10


def test_epic_pacing_phase_b_120s_from_wpm():
    cfg = {
        "dual_pacing": True,
        "target_duration_min": 90,
        "phase_a_max_words": 1500,
        "phase_a_seconds_per_scene": 3,
        "phase_a_words_min": 6,
        "phase_a_words_max": 12,
        "phase_a_target_scenes": 200,
        "phase_b_seconds_per_scene": 120,
        "phase_b_target_scenes": 40,
        "target_scenes": 240,
        "min_scenes": 200,
        "max_scenes": 280,
        "voice_wpm": 177.0,
    }
    p = load_dual_pacing(cfg, settings_target_scenes=240)
    assert p.phase_b_seconds == 120.0
    assert p.phase_b_words == 354  # round(177 * 2)
    assert p.phase_b_target_scenes == 40
    assert p.target_scenes == 240
    # Derived B scenes if omitted: (90 - 1500/177) * 60 / 120 ≈ 40.8 → 41
    derived = load_dual_pacing(
        {k: v for k, v in cfg.items() if k != "phase_b_target_scenes"},
        settings_target_scenes=240,
    )
    assert derived.phase_b_target_scenes in (40, 41)


def test_epic_profile_gates(monkeypatch: pytest.MonkeyPatch):
    load_retention_profiles.cache_clear()
    monkeypatch.setenv("RETENTION_PROFILE", "epic")
    lo_a, hi_a = profile_gate_a_band()
    lo_r, hi_r = profile_gate_r_band()
    assert lo_a == 4800.0
    assert hi_a == 6000.0
    assert lo_r == 4800.0
    assert hi_r == 6000.0
    load_retention_profiles.cache_clear()
    monkeypatch.setenv("RETENTION_PROFILE", "longform")
    lo_a, hi_a = profile_gate_a_band()
    assert lo_a == 900.0
    assert hi_a == 1500.0
    load_retention_profiles.cache_clear()


def test_epic_profile_json_shape():
    profiles = load_retention_profiles()
    epic = profiles["epic"]
    assert epic["target_duration_min"] == 90
    assert epic["phase_b_seconds_per_scene"] == 120
    assert epic["phase_b_target_scenes"] == 40
    assert epic["target_scenes"] == 240
    assert epic["chapters_target"] == 15
    assert epic["prompts_dir"] == "config/prompts/epic"
    assert epic["gate_r_min_duration_s"] == 4800
    assert epic["gate_r_max_duration_s"] == 6000
    assert epic["gate_a_min_duration_s"] == 4800
    assert epic["gate_a_max_duration_s"] == 6000
