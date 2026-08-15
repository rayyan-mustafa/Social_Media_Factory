"""Epic profile, voice WPM pacing, prompts_dir, farm format helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agents.cost_guardian import CostGuardian
from src.agents.farm import max_concurrent_jobs
from src.agents.store import OpsStore
from src.agents.title_queue import _row_from_dict, _row_to_sheet_dict
from src.services.pacing import load_dual_pacing
from src.services.retention_profile import (
    get_profile,
    load_retention_profiles,
    normalize_format,
    profile_gate_a_band,
    profile_gate_r_band,
    resolve_prompts_dir,
)
from src.services.settings import CONFIG_DIR, ROOT, load_prompt
from src.services.voice_wpm import (
    count_words,
    load_voice_wpm,
    measure_from_audio,
    phase_b_words_from_wpm,
    save_voice_wpm,
    wpm_from_words_duration,
)


@pytest.fixture(autouse=True)
def _clear_profile_cache():
    load_retention_profiles.cache_clear()
    yield
    load_retention_profiles.cache_clear()


def test_voice_wpm_math():
    assert count_words("Hello, Tudor world — one two three.") == 6
    assert wpm_from_words_duration(177, 60.0) == pytest.approx(177.0)
    assert phase_b_words_from_wpm(177.0, 120.0) == 354
    assert phase_b_words_from_wpm(177.0, 60.0) == 177


def test_voice_wpm_load_and_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "voice_wpm.json"
    rec = measure_from_audio(
        text=" ".join(["word"] * 177),
        duration_s=60.0,
        voice="am_michael",
        speed=1.0,
        source="manual",
    )
    save_voice_wpm(rec, path=path)
    monkeypatch.setenv("KOKORO_VOICE", "am_michael")
    monkeypatch.setenv("KOKORO_SPEED", "1.0")
    # Bypass settings cache by passing voice/speed explicitly
    assert load_voice_wpm(path=path, voice="am_michael", speed=1.0) == pytest.approx(
        177.0
    )
    assert load_voice_wpm(path=path, voice="am_michael", speed=0.9) == 150.0


def test_epic_profile_pacing_from_wpm(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RETENTION_PROFILE", "epic")
    load_retention_profiles.cache_clear()
    prof = get_profile("epic")
    assert prof["target_duration_min"] == 90
    assert prof["phase_b_seconds_per_scene"] == 120
    assert prof["target_scenes"] == 240
    assert prof["prompts_dir"] == "config/prompts/epic"

    cfg = dict(prof)
    cfg["voice_wpm"] = 177.0
    pacing = load_dual_pacing(cfg, settings_target_scenes=210)
    assert pacing.phase_a_max_words == 1500
    assert pacing.phase_a_target_scenes == 200
    assert pacing.phase_b_seconds == 120
    assert pacing.phase_b_words == 354
    assert pacing.phase_b_words_min == max(1, int(round(354 * 0.90)))
    assert pacing.phase_b_words_max == int(round(354 * 1.10))
    assert pacing.phase_b_target_scenes == 40
    assert pacing.target_scenes == 240
    assert pacing.min_scenes == 200
    assert pacing.max_scenes == 280
    # ~90 min: 200*3 + 40*120 = 600+4800 = 5400s
    est_min = (
        pacing.phase_a_target_scenes * pacing.phase_a_seconds
        + pacing.phase_b_target_scenes * pacing.phase_b_seconds
    ) / 60.0
    assert 85 <= est_min <= 95


def test_epic_phase_b_words_recenter_when_wpm_drifts():
    cfg = dict(get_profile("epic"))
    cfg["voice_wpm"] = 200.0  # outside 320–390 band at 120s → recenter
    pacing = load_dual_pacing(cfg, settings_target_scenes=240)
    assert pacing.phase_b_words == 400  # round(200*2)
    assert pacing.phase_b_words_min == 360
    assert pacing.phase_b_words_max == 440


def test_epic_gate_bands(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RETENTION_PROFILE", "epic")
    load_retention_profiles.cache_clear()
    lo_a, hi_a = profile_gate_a_band()
    lo_r, hi_r = profile_gate_r_band()
    assert lo_a == 4800
    assert hi_a == 6000
    assert lo_r == 4800
    assert hi_r == 6000


def test_longform_gate_bands_unchanged(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RETENTION_PROFILE", "longform")
    load_retention_profiles.cache_clear()
    lo_a, hi_a = profile_gate_a_band()
    assert lo_a == 900
    assert hi_a == 1500


def test_epic_prompts_resolve(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RETENTION_PROFILE", "epic")
    load_retention_profiles.cache_clear()
    d = resolve_prompts_dir()
    assert d == ROOT / "config" / "prompts" / "epic"
    outline = load_prompt("outline.txt", prompts_dir=d)
    expand = load_prompt("chapter_expand.txt", prompts_dir=d)
    assert "15-section" in outline or "15 sections" in outline.lower() or "exactly 15" in outline
    assert "{{TOPIC}}" in outline
    assert "{{PHASE_B_WORDS}}" in outline
    assert "{{VOICE_WPM}}" in outline
    assert "{{TARGET_SENTENCES}}" in expand
    assert "2-minute" in expand or "2 min" in expand.lower() or "120" in expand


def test_longform_prompts_default_dir(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RETENTION_PROFILE", "longform")
    load_retention_profiles.cache_clear()
    d = resolve_prompts_dir()
    assert d == ROOT / "config" / "prompts"
    text = load_prompt("outline.txt", prompts_dir=d)
    assert "10-section" in text or "10 sections" in text.lower() or "exactly 10" in text


def test_normalize_format(monkeypatch):
    monkeypatch.delenv("RETENTION_PROFILE", raising=False)
    assert normalize_format("epic") == "epic"
    assert normalize_format("90min") == "epic"
    assert normalize_format("15") == "retention"
    assert normalize_format("15min") == "retention"
    assert normalize_format("longform") == "longform"
    assert normalize_format("") == "retention"
    assert normalize_format("nope", default="longform") == "longform"


def test_title_row_format_roundtrip():
    """Legacy format still parses from dict; sheet write uses columns without format."""
    from src.agents.title_queue import DEFAULT_COLUMNS

    row = _row_from_dict(
        2,
        {"title": "What If X?", "format": "epic", "approved": "TRUE", "policy_ok": "1"},
    )
    assert row.format == "epic"
    d = _row_to_sheet_dict(row, columns=DEFAULT_COLUMNS)
    assert "format" not in d
    # Full dict still exposes format for in-memory / job meta
    assert _row_to_sheet_dict(row).get("format") == "epic"


def test_max_concurrent_jobs_stays_one():
    assert max_concurrent_jobs() == 1


def test_cost_guardian_epic_and_gpu(tmp_path: Path):
    store = OpsStore(tmp_path / "ops")
    g = CostGuardian(store=store)
    rates = g.rate_card()
    assert "A5000" in str(rates.get("runpod_gpu_class") or "") or "PRO 6000" in str(
        rates.get("runpod_gpu_class") or ""
    )
    assert float(rates.get("runpod_estimate_seconds_per_still") or 0) == pytest.approx(
        30.5, rel=1e-3
    )
    assert int(rates.get("runpod_stills_per_epic_video") or 0) == 240
    assert "3090" in str(rates.get("runpod_stills_gpu_priority") or "")
    assert "A40" in str(rates.get("runpod_stills_gpu_priority") or "")
    assert "A40" in str(rates.get("runpod_voice_gpu_priority") or "")
    # Voice skips flaky A40 Community
    assert "A40 Community" not in str(rates.get("runpod_voice_gpu_priority") or "")
    assert float(rates.get("runpod_pod_3090_usd_per_hour") or 0) == pytest.approx(0.22)
    assert float(rates.get("runpod_pod_a5000_secure_usd_per_hour") or 0) == pytest.approx(
        0.27
    )

    br_e = g.estimate_video_breakdown(profile="epic")
    br_l = g.estimate_video_breakdown(profile="longform")
    assert br_e["quantities"]["stills"] == 240
    # Pod mode: seconds × A5000 hourly (not serverless empirical)
    assert br_e["line_items"]["runpod_stills"] > 0.3
    assert br_e["total_usd_rounded"] > br_l["total_usd_rounded"]
    assert br_e["line_items"]["wavespeed_llm"] > br_l["line_items"]["wavespeed_llm"]


def test_seeded_voice_wpm_config_exists():
    path = CONFIG_DIR / "voice_wpm.json"
    assert path.exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert float(raw["wpm"]) >= 100
    assert raw.get("voice")
    # Epic profile Phase B docs should track ~round(wpm*2)
    load_retention_profiles.cache_clear()
    epic = get_profile("epic")
    expected = phase_b_words_from_wpm(float(raw["wpm"]), 120.0)
    assert abs(int(epic["phase_b_words_per_scene"]) - expected) <= 5


def test_epic_outline_placeholders_resolve(monkeypatch: pytest.MonkeyPatch):
    """Smoke: epic outline template renders with dual-pacing placeholders (no LLM)."""
    from src.services.settings import load_script_settings, render_prompt

    monkeypatch.setenv("RETENTION_PROFILE", "epic")
    load_retention_profiles.cache_clear()
    cfg = load_script_settings()
    pacing = load_dual_pacing(cfg, settings_target_scenes=240)
    d = resolve_prompts_dir(cfg)
    outline = load_prompt("outline.txt", prompts_dir=d)
    rendered = render_prompt(
        outline,
        {
            "TOPIC": "What If Anne Boleyn's Son Survived?",
            "NICHE_NOTES": "(none)",
            "TARGET_DURATION_MIN": cfg.get("target_duration_min", 90),
            "TARGET_SECONDS_PER_SCENE": pacing.phase_a_seconds,
            "TARGET_SCENES": pacing.target_scenes,
            "MIN_SCENES": pacing.min_scenes,
            "MAX_SCENES": pacing.max_scenes,
            "CHAPTERS_TARGET": cfg.get("chapters_target", 15),
            "PHASE_A_MAX_WORDS": pacing.phase_a_max_words,
            "PHASE_A_SCENES": pacing.phase_a_target_scenes,
            "PHASE_A_SECONDS": pacing.phase_a_seconds,
            "PHASE_A_WORDS_MIN": pacing.phase_a_words_min,
            "PHASE_A_WORDS_MAX": pacing.phase_a_words_max,
            "PHASE_B_SCENES": pacing.phase_b_target_scenes,
            "PHASE_B_SECONDS": pacing.phase_b_seconds,
            "PHASE_B_WORDS": pacing.phase_b_words,
            "VOICE_WPM": round(pacing.voice_wpm, 1),
        },
    )
    assert "{{" not in rendered
    assert "90" in rendered
    assert str(pacing.phase_b_seconds).rstrip("0").rstrip(".") in rendered or "120" in rendered
    assert "reengage" in rendered.lower() or "ask yourself" in rendered.lower()


def test_compose_split_120s_phase_b():
    """Epic Phase B still (~120s) splits into ~12 Ken Burns subclips at 10s max."""
    from src.services.composer import EditModule

    # _split_durations is pure (does not use instance state)
    parts = EditModule._split_durations(None, 120.0, 10.0)  # type: ignore[arg-type]
    assert len(parts) == 12
    assert sum(parts) == pytest.approx(120.0, rel=1e-6)
    assert all(p <= 10.0 + 1e-6 for p in parts)