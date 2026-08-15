"""Premium editor SMM directive tests."""

from __future__ import annotations

import json
from pathlib import Path

from src.services import premium_editor_smm as pe


def test_build_editing_directive_napstorian():
    d = pe.build_editing_directive("napstorian")
    assert d["channel"] == "napstorian"
    assert d["hook_pattern"] == "ticking_clock"
    assert d["thumbnail_style"].get("preset") == "napstorian_punch"


def test_build_editing_directive_historian():
    d = pe.build_editing_directive("napping_historian")
    assert d["hook_pattern"] == "identity_withheld"
    assert d["compose"].get("max_infographic_cards") == 8


def test_run_premium_editor_beat_dry_run(tmp_path: Path, monkeypatch):
    ops = tmp_path / "ops"
    ops.mkdir()
    intel = {
        "channels": {
            "napstorian": {
                "cluster": {
                    "dominant_form": "mid_form_drama",
                    "hints": {"compose": {"beat_interval_s": 50}},
                }
            },
            "napping_historian": {"cluster": {"dominant_form": "long_form_sleep", "hints": {}}},
        }
    }
    (ops / "competitor_style_intel.json").write_text(json.dumps(intel), encoding="utf-8")
    monkeypatch.setattr(pe, "OPS_DIR", ops)
    monkeypatch.setattr(pe, "PREMIUM_LOG", ops / "premium_editor_log.jsonl")
    monkeypatch.setattr(pe, "refresh_both_channels", lambda **kw: intel)

    goals = {
        "channels": {
            "napstorian": {"ctr_pct_min": 4.0, "avd_pct_min": 40.0, "first_60s_retention_pct_min": 70.0},
        }
    }
    scorecard = {
        "channels": {
            "napstorian": {
                "videos": [{"ctr_pct": 2.0, "avd_pct": 35, "first_60s_retention_pct": 60}],
            },
            "napping_historian": {"videos": []},
        }
    }
    out = pe.run_premium_editor_beat(goals=goals, scorecard=scorecard, dry_run=True)
    assert out["ok"] is True
    nap = out["channels"]["napstorian"]
    assert nap["underperforming"] is True
    assert nap["chosen_lever"] is not None
