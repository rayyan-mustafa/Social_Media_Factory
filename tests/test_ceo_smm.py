"""CEO-SMM unit tests (no live YouTube / SMTP)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from src.agents.ceo_smm import (
    _ceo_cfg,
    _delete_bad_still,
    _projectile,
    _resend_to_farm_phase,
    build_ceo_digest,
    build_zero_dollar_playbook,
    load_or_seed_goals,
)
from src.agents.smm_sop import stamp_stage_gate_meta


def test_ceo_cfg_defaults_all_true(tmp_path: Path, monkeypatch):
    cfg = {
        "smm": {
            "ceo": {
                # omit keys → defaults True
            }
        }
    }
    path = tmp_path / "agents_settings.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr("src.agents.ceo_smm.CONFIG_DIR", tmp_path)
    c = _ceo_cfg()
    assert c["enabled"] is True
    assert c["auto_sop_write"] is True
    assert c["auto_gpu"] is True
    assert c["auto_prompt_write"] is True


def test_ceo_cfg_explicit_false(tmp_path: Path, monkeypatch):
    cfg = {"smm": {"ceo": {"auto_email": False}}}
    path = tmp_path / "agents_settings.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr("src.agents.ceo_smm.CONFIG_DIR", tmp_path)
    c = _ceo_cfg()
    assert c["auto_email"] is False
    assert c["auto_gpu"] is True


def test_resend_to_farm_phase():
    assert _resend_to_farm_phase("script") == "prep"
    assert _resend_to_farm_phase("stills") == "visuals"
    assert _resend_to_farm_phase("compose") == "visuals"
    assert _resend_to_farm_phase("nope") is None


def test_delete_bad_still(tmp_path: Path):
    images = tmp_path / "images"
    images.mkdir()
    p = images / "scene_012.jpg"
    p.write_bytes(b"x")
    deleted = _delete_bad_still(tmp_path, "scene_12")
    assert deleted and not p.exists()


def test_projectile():
    assert _projectile(views=10, ctr=None, avd=None, ctr_min=4, avd_min=40) == "awaiting_analytics"
    assert (
        _projectile(views=100, ctr=2.0, avd=10.0, ctr_min=4, avd_min=40)
        == "critical_behind"
    )
    assert _projectile(views=100, ctr=5.0, avd=50.0, ctr_min=4, avd_min=40) == "on_track"


def test_stamp_extracts_bad_scene():
    meta = stamp_stage_gate_meta(
        {},
        {
            "ok": False,
            "stage": "stills",
            "warnings": ["scene_007 looks off-SOP"],
            "remediation": {"resend_stage": "stills"},
        },
    )
    assert meta["smm_sop_resend_stage"] == "stills"
    assert meta["smm_sop_bad_scene"] == "scene_007"


def test_load_goals_and_digest(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("src.agents.ceo_smm.OPS_DIR", tmp_path)
    monkeypatch.setattr("src.agents.ceo_smm.CEO_GOALS_PATH", tmp_path / "goals.json")
    monkeypatch.setattr("src.agents.ceo_smm.CEO_DIGEST_MD", tmp_path / "digest.md")
    monkeypatch.setattr("src.agents.ceo_smm.CEO_DIGEST_JSON", tmp_path / "digest.json")
    monkeypatch.setattr("src.agents.ceo_smm.CEO_PLAYBOOK", tmp_path / "playbook.md")
    goals = load_or_seed_goals()
    assert "napstorian" in goals["channels"]
    jsonl = tmp_path / "smm_yt_scorecard.jsonl"
    jsonl.write_text(
        json.dumps(
            {
                "channels": {
                    "napstorian": {
                        "n_public": 1,
                        "n_red": 1,
                        "videos": [
                            {
                                "title": "What If Test",
                                "views": 14,
                                "ctr_pct": None,
                                "avd_pct": 10.0,
                                "flag": "red",
                            }
                        ],
                    }
                },
                "ctr_pending_reporting": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.agents.ceo_smm._latest_scorecard_payload",
        lambda: json.loads(jsonl.read_text(encoding="utf-8").strip()),
    )
    dig = build_ceo_digest(actions=[{"type": "test"}], goals=goals)
    assert dig["channel_health"]["napstorian"] == "critical"
    assert (tmp_path / "digest.md").is_file()
    assert "projected" in (tmp_path / "digest.md").read_text(encoding="utf-8").lower() or "→" in (
        tmp_path / "digest.md"
    ).read_text(encoding="utf-8")


def test_aggregate_competitor_schedule(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "src.agents.ceo_smm._competitor_path_for",
        lambda ch: tmp_path / f"competitors_{ch}.json",
    )
    (tmp_path / "competitors_napstorian.json").write_text(
        json.dumps(
            {
                "competitors": [
                    {
                        "label": "a",
                        "best_upload_hours_local": [3, 4],
                        "upload_hour_histogram_local": {"3": 100, "4": 50},
                        "median_recent_views": 1000,
                        "upload_freq_per_30d": 15,
                    },
                    {
                        "label": "b",
                        "best_upload_hours_local": [3],
                        "upload_hour_histogram_local": {"3": 80},
                        "median_recent_views": 500,
                        "upload_freq_per_30d": 10,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    from src.agents.ceo_smm import _aggregate_competitor_schedule

    agg = _aggregate_competitor_schedule("napstorian")
    assert agg["ok"] is True
    assert agg["best_hours"][0] == 3
    assert agg["cadence_days"] == 2  # Rayyan-locked 15/mo
    assert agg["videos_per_month_target"] == 15
    assert agg["frequency_locked"] is True
    assert agg["competitor_implied_cadence_days"] == 2  # ~30/12.5 ≈ 2
    assert agg["competitor_implied_videos_per_month"] >= 10


def test_aggregate_historian_hours_deweights_foc_aspirational(tmp_path: Path, monkeypatch):
    """FoC aspirational epics must not dominate preferred_hours vs History Calling."""
    monkeypatch.setattr(
        "src.agents.ceo_smm._competitor_path_for",
        lambda ch: tmp_path / f"competitors_{ch}.json",
    )
    (tmp_path / "competitors_napping_historian.json").write_text(
        json.dumps(
            {
                "competitors": [
                    {
                        "label": "History Calling",
                        "style_role": "primary_anchor",
                        "best_upload_hours_local": [0],
                        "upload_hour_histogram_local": {"0": 700000},
                        "median_recent_views": 46517,
                        "upload_freq_per_30d": 4,
                    },
                    {
                        "label": "Fall of Civilizations",
                        "style_role": "aspirational_reference",
                        "best_upload_hours_local": [20, 22, 21],
                        "upload_hour_histogram_local": {
                            "20": 14948995,
                            "22": 12148022,
                            "21": 9278098,
                        },
                        "median_recent_views": 1070426,
                        "upload_freq_per_30d": 1,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    from src.agents.ceo_smm import _aggregate_competitor_schedule

    agg = _aggregate_competitor_schedule("napping_historian")
    assert agg["ok"] is True
    assert agg["best_hours"][0] == 0
    assert 20 not in agg["best_hours"][:1]
    assert agg["cadence_days"] == 2
    assert agg["videos_per_month_target"] == 15


def test_apply_competitor_schedules_hours_only_when_locked(tmp_path: Path, monkeypatch):
    """Competitor train writes preferred_hours but never cadence/monthly target."""
    monkeypatch.setattr(
        "src.agents.ceo_smm._competitor_path_for",
        lambda ch: tmp_path / f"competitors_{ch}.json",
    )
    monkeypatch.setattr("src.agents.ceo_smm.OPS_DIR", tmp_path)
    monkeypatch.setattr("src.agents.ceo_smm.CEO_GOALS_PATH", tmp_path / "goals.json")
    monkeypatch.setattr("src.agents.ceo_smm.CEO_ACTIONS_LOG", tmp_path / "ceo_actions.jsonl")
    monkeypatch.setattr(
        "src.agents.ceo_smm._ceo_cfg",
        lambda: {"auto_competitors": True},
    )

    for ch in ("napstorian", "napping_historian"):
        (tmp_path / f"competitors_{ch}.json").write_text(
            json.dumps(
                {
                    "competitors": [
                        {
                            "label": "a",
                            "best_upload_hours_local": [11, 12],
                            "upload_hour_histogram_local": {"11": 100, "12": 40},
                            "median_recent_views": 900,
                            "upload_freq_per_30d": 1,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    cfg_path = tmp_path / "publish_schedule.json"
    cfg = {
        "timezone": "Asia/Karachi",
        "frequency_locked": True,
        "frequency_lock_source": "rayyan_fixed_15_per_month",
        "cadence_days": 2,
        "videos_per_month_target": 15,
        "cadence_source": "rayyan_fixed_15_per_month",
        "channels": {
            "napstorian": {
                "publish_hour_local": 8,
                "preferred_hours": [8],
                "cadence_days": 2,
                "videos_per_month_target": 15,
            },
            "napping_historian": {
                "publish_hour_local": 20,
                "preferred_hours": [20],
                "cadence_days": 2,
                "videos_per_month_target": 15,
            },
        },
    }
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr("src.agents.schedule_agent.CONFIG_DIR", tmp_path)

    from src.agents.ceo_smm import apply_competitor_schedules_both_channels

    out = apply_competitor_schedules_both_channels()
    assert out["ok"] is True
    assert out["frequency_locked"] is True
    assert out["global_cadence"]["applied"] is False
    assert out["global_cadence"]["reason"] == "frequency_locked"

    written = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert written["cadence_days"] == 2
    assert written["videos_per_month_target"] == 15
    assert written["channels"]["napstorian"]["preferred_hours"][0] == 11
    assert written["channels"]["napstorian"]["cadence_days"] == 2
    assert written["channels"]["napstorian"]["videos_per_month_target"] == 15

    goals = json.loads((tmp_path / "goals.json").read_text(encoding="utf-8"))
    assert goals["channels"]["napstorian"]["weekly_publics_target"] == 4
    assert goals["channels"]["napstorian"]["competitor_schedule"]["cadence_days"] == 2
    assert goals["channels"]["napstorian"]["competitor_schedule"]["videos_per_month_target"] == 15


def test_write_cadence_respects_frequency_lock(tmp_path: Path, monkeypatch):
    from src.agents.schedule_agent import ScheduleAgent
    from src.agents.store import OpsStore

    cfg_path = tmp_path / "publish_schedule.json"
    cfg = {
        "timezone": "Asia/Karachi",
        "frequency_locked": True,
        "cadence_days": 2,
        "videos_per_month_target": 15,
        "channels": {
            "napstorian": {"cadence_days": 2, "videos_per_month_target": 15},
        },
    }
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr("src.agents.schedule_agent.CONFIG_DIR", tmp_path)
    agent = ScheduleAgent(store=OpsStore(tmp_path / "ops"))
    blocked = agent.write_cadence_days(7, source="ceo_competitor_pacing", channel="napstorian")
    assert blocked["applied"] is False
    assert blocked["reason"] == "frequency_locked"
    after = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert after["cadence_days"] == 2
    forced = agent.write_cadence_days(
        2, source="rayyan_fixed_15_per_month", channel="napstorian", force=True
    )
    assert forced["applied"] is True


def test_score_channel_upload_hours():
    from datetime import datetime, timezone
    from src.agents.youtube_data import score_channel

    # 04:00 PKT = 23:00 UTC previous day
    uploads = [{"video_id": "v1", "published_at": "2026-08-08T23:00:00Z"}]
    stats = {"v1": {"views": 100, "published_at": "2026-08-08T23:00:00Z"}}
    out = score_channel(
        channel={"subscribers": 50000, "total_views": 1, "video_count": 1},
        uploads=uploads,
        video_stats=stats,
        power={
            "good_channel_views": 1,
            "min_subscribers": 1,
            "require_recent_upload": False,
            "recent_video_sample": 5,
        },
        tz_name="Asia/Karachi",
    )
    assert 4 in out["best_upload_hours_local"]
    assert "4" in out["upload_hour_histogram_local"]
