"""Editing lever apply + override wiring tests."""

from __future__ import annotations

import json
from pathlib import Path

from src.services import smm_pipeline_ab as ab
from src.services.editing_overrides import (
    OVERRIDES_PATH,
    apply_editing_to_settings,
    get_merged_channel_editing,
    hook_pattern_prefs,
    thumbnail_style_preset,
    write_channel_overrides,
)
from src.services.settings import get_settings


def test_apply_editing_lever_writes_overrides(tmp_path: Path, monkeypatch):
    overrides = tmp_path / "channel_editing_overrides.json"
    profiles = tmp_path / "competitor_style_profiles.json"
    profiles.write_text(
        json.dumps(
            {
                "channels": {
                    "napstorian": {
                        "hook_pattern": {"default": "ticking_clock"},
                        "compose": {"beat_interval_s": 55},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    overrides.write_text('{"channels": {}}', encoding="utf-8")
    monkeypatch.setattr(ab, "CHANNEL_EDITING_OVERRIDES", overrides)
    monkeypatch.setattr(ab, "EXPERIMENTS", tmp_path / "exp.jsonl")
    monkeypatch.setattr(ab, "WORKS", tmp_path / "works.jsonl")
    monkeypatch.setattr(ab, "FAILS", tmp_path / "fails.jsonl")
    monkeypatch.setattr(ab, "PLAYBOOK", tmp_path / "playbook.md")

    from src.services import editing_overrides as eo

    monkeypatch.setattr(eo, "OVERRIDES_PATH", overrides)
    monkeypatch.setattr(eo, "PROFILES_PATH", profiles)

    out = ab.apply_editing_lever("napstorian", "compose_sfx_beat_interval_s", 45)
    assert out["ok"] is True
    data = json.loads(overrides.read_text(encoding="utf-8"))
    beat = data["channels"]["napstorian"]["overrides"]["compose"]["beat_interval_s"]
    assert beat == 45


def test_merged_editing_and_settings_patch(monkeypatch, tmp_path: Path):
    profiles = tmp_path / "profiles.json"
    overrides = tmp_path / "overrides.json"
    profiles.write_text(
        json.dumps(
            {
                "channels": {
                    "napping_historian": {
                        "compose": {
                            "ken_burns_scale": 1.06,
                            "max_infographic_cards": 8,
                            "beat_interval_s": 0,
                        },
                        "audio": {"ambient_bed_db": -32, "historian_sfx_off": True},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    overrides.write_text('{"channels": {}}', encoding="utf-8")

    from src.services import editing_overrides as eo

    monkeypatch.setattr(eo, "PROFILES_PATH", profiles)
    monkeypatch.setattr(eo, "OVERRIDES_PATH", overrides)

    merged = get_merged_channel_editing("napping_historian")
    assert merged["compose"]["max_infographic_cards"] == 8
    assert hook_pattern_prefs("napping_historian") is None or isinstance(
        hook_pattern_prefs("napping_historian"), list
    )

    get_settings.cache_clear()
    s = get_settings()
    applied = apply_editing_to_settings(s, "napping_historian")
    assert applied["max_infographic_cards"] == 8
    assert s.compose_sfx_historian is False


def test_rank_editing_levers_underperforming():
    levers = ab.rank_editing_levers(
        "napstorian",
        underperforming=True,
        scorecard_reds=["ctr_pct"],
    )
    assert levers[0] == "thumbnail_style"


def test_revert_editing_lever(tmp_path: Path, monkeypatch):
    overrides = tmp_path / "overrides.json"
    overrides.write_text(
        json.dumps(
            {
                "channels": {
                    "napstorian": {
                        "overrides": {"compose": {"beat_interval_s": 40}},
                        "active_experiment": {
                            "lever": "compose_sfx_beat_interval_s",
                            "baseline_overrides": {},
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ab, "CHANNEL_EDITING_OVERRIDES", overrides)
    monkeypatch.setattr(ab, "FAILS", tmp_path / "fails.jsonl")
    monkeypatch.setattr(ab, "PLAYBOOK", tmp_path / "playbook.md")
    from src.services import editing_overrides as eo

    monkeypatch.setattr(eo, "OVERRIDES_PATH", overrides)
    monkeypatch.setattr(eo, "PROFILES_PATH", tmp_path / "p.json")
    (tmp_path / "p.json").write_text('{"channels": {}}', encoding="utf-8")

    r = ab.revert_editing_lever("napstorian", "compose_sfx_beat_interval_s")
    assert r["ok"] is True
