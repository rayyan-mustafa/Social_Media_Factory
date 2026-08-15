"""Tests for publish schedule (per-channel PKT hours) + cost rates."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.agents.cost_guardian import CostGuardian
from src.agents.schedule_agent import ScheduleAgent
from src.agents.smm_agent import SocialMediaManager
from src.agents.store import OpsStore


def test_pkt_historian_6am_vs_eastern_note():
    agent = ScheduleAgent(store=OpsStore(Path("/tmp/ops_sched_note")))
    note = agent.pkt_to_eastern_note(6)
    assert "06:00" in note or "6:00" in note or ":00 Asia/Karachi" in note
    assert "01:00" in note  # 06:00 PKT == 01:00 UTC
    assert "America/New_York" in note


def test_pkt_napstorian_4am_note():
    agent = ScheduleAgent(store=OpsStore(Path("/tmp/ops_sched_note2")))
    note = agent.pkt_to_eastern_note(4)
    assert "04:00" in note or "4:00" in note
    assert "23:00" in note  # 04:00 PKT == 23:00 UTC prior evening


def test_next_slot_is_4am_karachi_napstorian(tmp_path: Path):
    store = OpsStore(tmp_path / "ops")
    agent = ScheduleAgent(store=store)
    agent.cfg = {
        "timezone": "Asia/Karachi",
        "publish_hour_local": 4,
        "publish_minute_local": 0,
        "cadence_days": 2,
        "human_approve_first_n": 15,
        "require_public_approved_for_all": True,
        "channels": {
            "napstorian": {"publish_hour_local": 4, "preferred_hours": [4]},
            "napping_historian": {"publish_hour_local": 6, "preferred_hours": [6]},
        },
    }
    after = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)  # afternoon UTC
    slot = agent.next_slot(after=after, existing=[], channel="napstorian")
    local = slot.astimezone(ZoneInfo("Asia/Karachi"))
    assert local.hour == 4
    assert local.minute == 0


def test_historian_default_is_6am_pkt(tmp_path: Path):
    store = OpsStore(tmp_path / "ops")
    agent = ScheduleAgent(store=store)
    agent.cfg = {
        "timezone": "Asia/Karachi",
        "publish_hour_local": 4,
        "publish_minute_local": 0,
        "cadence_days": 2,
        "channels": {
            "napstorian": {"publish_hour_local": 4, "preferred_hours": [4]},
            "napping_historian": {"publish_hour_local": 6, "preferred_hours": [6]},
        },
    }
    assert agent.publish_hour_for_channel("napping_historian") == 6
    assert agent.publish_hour_for_channel("napstorian") == 4
    after = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    slot = agent.next_slot(after=after, existing=[], channel="napping_historian")
    local = slot.astimezone(ZoneInfo("Asia/Karachi"))
    assert local.hour == 6
    utc = slot.astimezone(timezone.utc)
    assert utc.hour == 1  # 06:00 PKT == 01:00 UTC


def test_per_channel_hour_resolution_preferred_over_global(tmp_path: Path):
    store = OpsStore(tmp_path / "ops")
    agent = ScheduleAgent(store=store)
    agent.cfg = {
        "timezone": "Asia/Karachi",
        "publish_hour_local": 4,
        "channels": {
            "napstorian": {"publish_hour_local": 4, "preferred_hours": [5]},
            "napping_historian": {"publish_hour_local": 6, "preferred_hours": [7]},
        },
    }
    assert agent.publish_hour_for_channel("napstorian") == 5
    assert agent.publish_hour_for_channel("napping_historian") == 7


def test_sheet_hour_columns_removed_uses_preferred(tmp_path: Path):
    """No sheet hour columns — channel preferred_hours win."""
    store = OpsStore(tmp_path / "ops")
    agent = ScheduleAgent(store=store)
    agent.cfg = {
        "timezone": "Asia/Karachi",
        "channels": {
            "napping_historian": {"publish_hour_local": 6, "preferred_hours": [6]},
        },
    }

    class _Row:
        pass

    assert agent.publish_hour_for_channel("napping_historian", row=_Row()) == 6


def test_smm_always_applies_outside_soft_delta(tmp_path: Path, monkeypatch):
    """SMM hour writes use soft=False so large jumps always apply."""
    store = OpsStore(tmp_path / "ops")
    agent = ScheduleAgent(store=store)
    cfg_path = tmp_path / "publish_schedule.json"
    cfg = {
        "timezone": "Asia/Karachi",
        "publish_hour_local": 4,
        "smm_hour_soft_max_delta": 3,
        "channels": {
            "napstorian": {"publish_hour_local": 4, "preferred_hours": [4]},
            "napping_historian": {"publish_hour_local": 6, "preferred_hours": [6]},
        },
    }
    cfg_path.write_text(__import__("json").dumps(cfg), encoding="utf-8")
    monkeypatch.setattr("src.agents.schedule_agent.CONFIG_DIR", tmp_path)
    agent.cfg = cfg

    hard = agent.write_channel_preferred_hours(
        "napping_historian", [20], source="smm_always_accept", soft=False
    )
    assert hard["applied"] is True
    assert hard["preferred_hours"][0] == 20


def test_schedule_requires_public_approved(tmp_path: Path):
    store = OpsStore(tmp_path / "ops")
    agent = ScheduleAgent(store=store)
    agent.cfg["require_public_approved_for_all"] = True
    agent.cfg["human_approve_first_n"] = 15
    ok, reason = agent.can_arm_schedule(public_approved=False)
    assert not ok
    assert "public_approved" in reason
    ok2, _ = agent.can_arm_schedule(public_approved=True)
    assert ok2


def test_assign_slot_dry_without_youtube(tmp_path: Path):
    store = OpsStore(tmp_path / "ops")
    job = store.create_job("What If History?", status="private")
    agent = ScheduleAgent(store=store)
    agent.cfg = {
        "timezone": "Asia/Karachi",
        "publish_minute_local": 0,
        "cadence_days": 2,
        "require_public_approved_for_all": True,
        "channels": {
            "napstorian": {"publish_hour_local": 4, "preferred_hours": [4]},
            "napping_historian": {"publish_hour_local": 6, "preferred_hours": [6]},
        },
    }
    # Local arm without YouTube API (apply_youtube=False) still marks scheduled.
    res = agent.assign_slot_for_job(
        job_id=job.id,
        video_id="vid123",
        public_approved=True,
        dry_run=False,
        apply_youtube=False,
    )
    assert res["ok"]
    assert "publish_at_utc" in res
    assert res.get("channel") == "napstorian"
    assert res.get("publish_hour_local") == 4
    assert store.get_job(job.id).status == "scheduled"

    preview = agent.assign_slot_for_job(
        job_id=job.id,
        video_id="vid123",
        public_approved=True,
        dry_run=True,
        apply_youtube=False,
    )
    assert preview["ok"] and preview.get("dry_run") is True


def test_assign_slot_historian_uses_6am(tmp_path: Path):
    store = OpsStore(tmp_path / "ops")
    job = store.create_job(
        "Ancient Mystery Doc",
        status="private",
        meta={"channel": "napping_historian", "sheet_tab": "napping_historian"},
    )
    agent = ScheduleAgent(store=store)
    agent.cfg = {
        "timezone": "Asia/Karachi",
        "publish_minute_local": 0,
        "cadence_days": 2,
        "require_public_approved_for_all": True,
        "channels": {
            "napstorian": {"publish_hour_local": 4, "preferred_hours": [4]},
            "napping_historian": {"publish_hour_local": 6, "preferred_hours": [6]},
        },
    }
    res = agent.assign_slot_for_job(
        job_id=job.id,
        video_id="vid_hist",
        public_approved=True,
        dry_run=True,
        apply_youtube=False,
    )
    assert res["ok"]
    assert res["channel"] == "napping_historian"
    assert res["publish_hour_local"] == 6
    local = datetime.fromisoformat(res["scheduled_at_local"])
    assert local.astimezone(ZoneInfo("Asia/Karachi")).hour == 6


def test_smm_write_preferred_hours_soft_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = OpsStore(tmp_path / "ops")
    agent = ScheduleAgent(store=store)
    cfg_path = tmp_path / "publish_schedule.json"
    cfg = {
        "timezone": "Asia/Karachi",
        "publish_hour_local": 4,
        "smm_hour_soft_max_delta": 3,
        "channels": {
            "napstorian": {"publish_hour_local": 4, "preferred_hours": [4]},
            "napping_historian": {"publish_hour_local": 6, "preferred_hours": [6]},
        },
    }
    cfg_path.write_text(__import__("json").dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(
        "src.agents.schedule_agent.CONFIG_DIR", tmp_path
    )
    agent.cfg = cfg

    soft_ok = agent.write_channel_preferred_hours(
        "napping_historian", [8], source="test", soft=True
    )
    assert soft_ok["applied"] is True
    assert soft_ok["preferred_hours"][0] == 8

    soft_block = agent.write_channel_preferred_hours(
        "napping_historian", [20], source="test", soft=True
    )
    assert soft_block["applied"] is False
    assert soft_block["reason"] == "outside_soft_window"


def test_smm_hour_histogram():
    videos = [
        {"published_at": "2026-01-10T01:15:00Z", "views": 1000},  # 06:00 PKT
        {"published_at": "2026-01-11T01:30:00Z", "views": 500},  # 06:00 PKT
        {"published_at": "2026-01-12T23:00:00Z", "views": 100},  # 04:00 PKT
    ]
    hist = SocialMediaManager._publish_hour_histogram(videos)
    assert hist[0][0] == 6  # best hour is 06 PKT


def test_cost_rates_runpod_069(tmp_path: Path):
    store = OpsStore(tmp_path / "ops")
    g = CostGuardian(store=store)
    rates = g.rate_card()
    # Pod primary A5000; serverless legacy rate kept for empirics
    assert float(rates.get("runpod_pod_a5000_usd_per_hour") or 0) == pytest.approx(0.16)
    assert float(rates.get("runpod_serverless_usd_per_hour") or 0) == pytest.approx(0.69)
    assert float(rates.get("runpod_estimate_seconds_per_still") or 0) == pytest.approx(30.5)
    assert float(rates.get("runpod_serverless_usd_per_still_empirical") or 0) == pytest.approx(
        0.0095
    )
    assert "A5000" in str(rates.get("runpod_gpu_class") or "") or "PRO 6000" in str(
        rates.get("runpod_gpu_class") or ""
    )
    assert float(rates.get("wavespeed_seedream_thumb_usd_per_image") or 0) == 0.035
    assert rates.get("use_explicit_video_estimates") is not True
    est_r = g.estimate_video_usd(profile="retention")
    est_l = g.estimate_video_usd(profile="longform")
    est_e = g.estimate_video_usd(profile="epic")
    br_l = g.estimate_video_breakdown(profile="longform")
    # Longform dominated by RunPod stills; must include LLM + Seedream line items
    assert est_l > est_r
    assert est_e > est_l
    assert br_l["line_items"]["runpod_stills"] > 0.2
    assert br_l["line_items"]["wavespeed_llm"] > 0.05
    assert br_l["line_items"]["wavespeed_seedream_thumb"] == 0.035
    assert br_l["line_items"]["openrouter_meta"] == 0.0
    assert br_l["line_items"]["kokoro_tts"] == 0.0
    # Pod A5000 @ measured ~30.5s/still — cheaper than serverless ~$2/vid
    assert 0.25 <= est_l <= 1.20
    assert 0.15 <= est_r <= 0.80
    assert 0.40 <= est_e <= 1.80
    pack = g.estimate_monthly_pack(channels=2, videos_per_channel=15, profile="retention")
    assert pack["videos_per_month"] == 30
    assert pack["seedream_required"] is True
    assert pack["tts"] == "kokoro"
    assert pack["per_video_line_items"]["wavespeed_seedream_thumb"] == 0.035
    assert 6.0 <= pack["total_usd"] <= 15.0
    ok, msg = g.check_can_start_job()
    assert ok
    assert f"est=${est_l:.2f}" in msg or "est=$" in msg
    assert "cap=$25.00" in msg


def test_smm_proposes_publish_hour(tmp_path: Path):
    store = OpsStore(tmp_path / "ops")
    smm = SocialMediaManager(store=store)
    insight = smm.evaluate_video(
        video_id="x",
        title="What If History?",
        metrics={
            "avd_pct": 50,
            "first_60s_retention_pct": 75,
            "ctr_pct": 5,
            "peak_live_hour_local": 20,
        },
    )
    types = [p.get("type") for p in insight.proposals]
    assert "publish_hour" in types
