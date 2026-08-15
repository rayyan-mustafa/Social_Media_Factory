"""High-effort competitor feature foundations (maps, epic band, voice cast, formats)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.services import map_motion as mm
from src.services import motion_graphics as mg
from src.services import still_budget as sb
from src.services import voice_cast as vc
from src.services.premium_editor_smm import build_editing_directive
from src.services.retention_profile import (
    get_profile,
    load_retention_profiles,
    normalize_format,
)
from src.services.pacing import load_dual_pacing


@pytest.fixture(autouse=True)
def _clear_profile_cache():
    load_retention_profiles.cache_clear()
    yield
    load_retention_profiles.cache_clear()


def test_map_motion_plan_and_frame(tmp_path: Path):
    script = {
        "title": "What If Vienna Fell in 1529",
        "scenes": [
            {"index": 0, "text": "The Ottoman army marched on Vienna in 1529."},
            {"index": 1, "text": "From Constantinople the campaign route stretched west."},
        ],
    }
    plan = mm.plan_from_script(script, scene_index=0, duration_s=1.0)
    assert plan.date_stamp == "1529"
    assert any(w.label in {"Vienna", "Constantinople"} for w in plan.waypoints)
    frame = tmp_path / "frame.jpg"
    mm.render_map_frame(plan, t=0.5, out_path=frame)
    assert frame.is_file() and frame.stat().st_size > 1000


def test_map_motion_clip_synthetic(tmp_path: Path):
    if not mm.ffmpeg_available():
        pytest.skip("ffmpeg missing")
    plan = mm.MapMotionPlan(
        title="Test Campaign",
        date_stamp="1540",
        waypoints=[
            mm.MapWaypoint("A", 0.3, 0.5, "1540"),
            mm.MapWaypoint("B", 0.7, 0.4, "1540"),
        ],
        duration_s=0.5,
        fps=8,
        width=640,
        height=360,
    )
    out = tmp_path / "map.mp4"
    mm.generate_map_clip(plan, out_path=out)
    assert out.is_file() and out.stat().st_size > 500
    assert out.with_suffix(".map.json").is_file()


def test_motion_mode_wants_maps():
    assert mm.motion_mode_wants_maps({"motion_mode": "animated_maps"})
    assert mm.motion_mode_wants_maps({"animated_maps": True})
    assert not mm.motion_mode_wants_maps({"motion_mode": "ken_burns"})


def test_motion_graphics_plates(tmp_path: Path):
    script = {
        "title": "Tudor War",
        "scenes": [{"index": 0, "text": "In 1536 the north rose."}],
        "outline": {"chapters": [{"title": "Soft Dusk"}]},
    }
    rows = mg.plan_motion_plates_for_script(
        script, channel="napstorian", out_dir=tmp_path / "nap"
    )
    assert any(r["kind"] == "timeline_wipe" for r in rows)
    soft = mg.plan_motion_plates_for_script(
        script, channel="napping_historian", out_dir=tmp_path / "hist"
    )
    assert any(r["kind"] == "poetic_chapter" for r in soft)


def test_foc_epic_length_band_and_profile():
    lo, hi = sb.foc_epic_length_band_s()
    assert lo == 10800 and hi == 14400
    assert normalize_format("foc_epic") == "foc_epic"
    assert normalize_format("3-4h") == "foc_epic"
    assert normalize_format("primary_source_reading") == "primary_source_reading"
    prof = get_profile("foc_epic")
    assert prof["target_duration_min"] == 210
    assert prof["gate_a_min_duration_s"] == 10800
    assert prof["gate_a_max_duration_s"] == 14400
    assert prof["max_unique_stills"] == 80
    assert prof["prompts_dir"] == "config/prompts/foc_epic"
    pacing = load_dual_pacing(dict(prof), settings_target_scenes=272)
    est = (
        pacing.phase_a_target_scenes * pacing.phase_a_seconds
        + pacing.phase_b_target_scenes * pacing.phase_b_seconds
    )
    assert 10800 <= est <= 14400


def test_still_reuse_map_caps_unique():
    mapping = sb.still_reuse_map(272, max_unique=80)
    assert len(mapping) == 272
    assert len(set(mapping)) == 80
    idxs = sb.unique_scene_indices(272, max_unique=80)
    assert len(idxs) == 80


def test_primary_source_format_flags():
    prof = get_profile("primary_source_reading")
    assert prof.get("format_mode") == "primary_source_reading"
    assert Path(prof["prompts_dir"]).joinpath("outline.txt").exists() or (
        Path("config/prompts/primary_source_reading/outline.txt").exists()
    )


def test_voice_cast_human_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cast = {
        "channels": {
            "napstorian": {
                "voice_mode": "human_brand",
                "speakers": {
                    "narrator": {
                        "kokoro_voice": "am_michael",
                        "human_wav": "assets/voice_cast/missing_brand.wav",
                    },
                    "quote_a": {"kokoro_voice": "am_adam"},
                },
            }
        }
    }
    path = tmp_path / "voice_cast.json"
    path.write_text(json.dumps(cast), encoding="utf-8")
    monkeypatch.setattr(vc, "VOICE_CAST_PATH", path)
    resolved = vc.resolve_speaker_voice(
        "narrator", channel="napstorian", cast=cast, voice_mode="human_brand"
    )
    assert resolved["fallback_used"] is True
    assert resolved["voice_id"] == "am_michael"
    assert resolved["backend_hint"] == "kokoro"

    multi = vc.resolve_speaker_voice(
        "quote_a", channel="napstorian", cast=cast, voice_mode="tts_multi"
    )
    assert multi["voice_id"] == "am_adam"

    single = vc.resolve_speaker_voice(
        "quote_a", channel="napstorian", cast=cast, voice_mode="tts_single"
    )
    assert single["voice_id"] == "am_michael"


def test_voice_infer_quote_role():
    long_q = (
        'He said, "The realm will burn tonight under foreign sails across '
        'the channel and the court will scatter before dawn."'
    )
    assert vc.infer_speaker_role(long_q) == "quote_a"
    assert vc.infer_speaker_role('"Short."') == "quote_b"
    assert vc.infer_speaker_role("The chronicler notes the weather.") == "narrator"


def test_premium_directive_includes_high_effort_levers():
    d = build_editing_directive("napstorian")
    assert d["motion_mode"] in {"animated_maps", "ken_burns", "motion_graphics"}
    assert d["format_mode"]
    assert d["voice_mode"]
    assert d["score_mode"]
    assert "voice_cast" in d
    h = build_editing_directive("napping_historian")
    assert h["score_mode"] == "ambient_soft"
    assert h["motion_mode"] == "ken_burns"
    assert h["format_mode"] == "standard"
    assert h["format_mode"] != "foc_epic"
    pacing = h.get("pacing") or {}
    assert int(pacing.get("target_length_band_min_s") or 0) == 1200
    assert int(pacing.get("target_length_band_max_s") or 0) == 2700
    assert int(pacing.get("target_length_band_max_s") or 0) < 10800
    assert (h.get("hook_pattern") or pacing)  # directive has hook_pattern field
    assert h.get("hook_pattern") in {"false_assumption", "identity_withheld", "other"}
    assert h["profile_label"] == "History Calling–like mystery documentary"


def test_historian_style_profile_history_calling_defaults():
    from src.services.editing_overrides import (
        format_mode_for_channel,
        get_channel_profile,
        target_length_band,
    )

    prof = get_channel_profile("napping_historian")
    assert "History Calling" in str(prof.get("label") or "")
    assert prof.get("style_anchor") == "History Calling"
    assert "Fall of Civilizations" in (prof.get("aspirational_only") or [])
    assert format_mode_for_channel("napping_historian") == "standard"
    lo, hi = target_length_band("napping_historian")
    assert lo == 1200 and hi == 2700
    assert (prof.get("compose") or {}).get("sfx_density") == "none"
    assert (prof.get("audio") or {}).get("score_mode") == "ambient_soft"
    assert (prof.get("motion") or {}).get("motion_mode") == "ken_burns"
    assert (prof.get("hook_pattern") or {}).get("default") == "false_assumption"
    # foc_epic remains available as optional band metadata, not format_mode
    assert (prof.get("pacing") or {}).get("format_mode") == "standard"
    assert "foc_epic" in ((prof.get("pacing") or {}).get("optional_formats") or [])


def test_historian_competitors_prioritize_history_calling():
    import json
    from pathlib import Path

    data = json.loads(
        Path("config/competitors_napping_historian.json").read_text(encoding="utf-8")
    )
    comps = data.get("competitors") or []
    by_label = {c.get("label"): c for c in comps}
    hc = by_label["History Calling"]
    assert hc.get("style_role") == "primary_anchor"
    assert hc.get("tier") == "A" or int(hc.get("style_priority") or 99) == 1
    foc = by_label["Fall of Civilizations"]
    assert foc.get("style_role") == "aspirational_reference"
    assert foc.get("eligible_for_titles") is False
    assert comps[0].get("label") == "History Calling"


def test_format_mode_lever_patch():
    from src.services.smm_pipeline_ab import _lever_to_override_patch

    patch = _lever_to_override_patch("format_mode", "foc_epic")
    assert patch["pacing"]["format_mode"] == "foc_epic"
    assert patch["pacing"]["target_length_band_min_s"] == 10800
    assert _lever_to_override_patch("motion_mode", "animated_maps")["motion"][
        "motion_mode"
    ] == "animated_maps"
