"""SMM daily scorecard + hook_failure proposals."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from src.agents.smm_agent import SocialMediaManager
from src.agents.store import OpsStore


def test_evaluate_hook_failure_proposal(tmp_path: Path):
    store = OpsStore(root=tmp_path)
    smm = SocialMediaManager(store=store)
    insight = smm.evaluate_video(
        video_id="vid1",
        title="What If Test",
        metrics={
            "ctr_pct": 5.0,
            "avd_pct": 30.0,
            "first_60s_retention_pct": 50.0,
            "source": "cli",
        },
        apply_actions=False,
    )
    types = {p.get("type") for p in insight.proposals}
    assert "hook_failure" in types
    assert "prompts_pacing" in types


def test_daily_scorecard_writes_md(tmp_path: Path, monkeypatch):
    store = OpsStore(root=tmp_path)
    store.create_job(
        "What If Scorecard",
        id="j1",
        status="public",
        stage="public",
        video_id="vid_sc",
        job_dir=str(tmp_path / "job"),
        meta={"channel": "napstorian"},
    )

    smm = SocialMediaManager(store=store)
    monkeypatch.setattr(
        smm,
        "_fetch_or_stub_metrics",
        lambda *a, **k: {
            "video_id": "vid_sc",
            "views": 100,
            "likes": 5,
            "impressions": 2500,
            "ctr_pct": 5.2,
            "avd_pct": 41.0,
            "source": "youtube_analytics",
        },
    )
    monkeypatch.setattr(smm, "_own_live_concurrent_viewers", lambda ch: 0)
    monkeypatch.setattr(smm, "_load_featured_vods", lambda: {"channels": {}})
    monkeypatch.setattr(smm, "_smm_live_channels", lambda: ("napstorian",))
    monkeypatch.setattr(smm, "_job_channel", lambda job: "napstorian")

    with patch(
        "src.agents.smm_agent._smm_yt_scorecard_path",
        return_value=tmp_path / "smm_yt_scorecard.md",
    ):
        with patch(
            "src.agents.smm_agent._smm_yt_scorecard_jsonl_path",
            return_value=tmp_path / "smm_yt_scorecard.jsonl",
        ):
            with patch.object(smm, "_persist_retention_dogs", return_value=tmp_path / "dogs.json"):
                out = smm.write_daily_yt_scorecard()
    assert out["ok"] is True
    assert (tmp_path / "smm_yt_scorecard.md").exists()
    text = (tmp_path / "smm_yt_scorecard.md").read_text(encoding="utf-8")
    assert "daily YT scorecard" in text
    assert "napstorian" in text
    assert "5.2" in text
    assert out["payload"]["channels"]["napstorian"]["videos"][0]["ctr_pct"] == 5.2
    assert out["payload"]["analytics_scope_blocked"] is False
    assert out["payload"]["bars"]["avd_policy"] == "advisory_only"


def test_daily_scorecard_ctr_pending_note(tmp_path: Path, monkeypatch):
    store = OpsStore(root=tmp_path)
    store.create_job(
        "What If Pending CTR",
        id="j2",
        status="public",
        stage="public",
        video_id="vid_pend",
        job_dir=str(tmp_path / "job2"),
        meta={"channel": "napstorian"},
    )
    smm = SocialMediaManager(store=store)
    monkeypatch.setattr(
        smm,
        "_fetch_or_stub_metrics",
        lambda *a, **k: {
            "video_id": "vid_pend",
            "views": 14,
            "avd_pct": 10.0,
            "ctr_pct": None,
            "impressions": None,
            "ctr_unavailable": True,
            "ctr_pending_reporting": True,
            "analytics_note": "Reporting reach awaiting CSV",
            "source": "youtube_analytics",
        },
    )
    monkeypatch.setattr(smm, "_own_live_concurrent_viewers", lambda ch: 0)
    monkeypatch.setattr(smm, "_load_featured_vods", lambda: {"channels": {}})
    monkeypatch.setattr(smm, "_smm_live_channels", lambda: ("napstorian",))
    monkeypatch.setattr(smm, "_job_channel", lambda job: "napstorian")

    with patch(
        "src.agents.smm_agent._smm_yt_scorecard_path",
        return_value=tmp_path / "smm_yt_scorecard.md",
    ), patch(
        "src.agents.smm_agent._smm_yt_scorecard_jsonl_path",
        return_value=tmp_path / "smm_yt_scorecard.jsonl",
    ), patch.object(smm, "_persist_retention_dogs", return_value=tmp_path / "dogs.json"):
        out = smm.write_daily_yt_scorecard()

    assert out["payload"]["ctr_pending_reporting"] is True
    text = (tmp_path / "smm_yt_scorecard.md").read_text(encoding="utf-8")
    assert "CTR PENDING" in text or "CTR pending" in text
    assert "advisory only" in text.lower()


def test_evaluate_skips_first_60s_when_missing(tmp_path: Path):
    store = OpsStore(root=tmp_path)
    smm = SocialMediaManager(store=store)
    insight = smm.evaluate_video(
        video_id="vid1",
        title="What If Test",
        metrics={
            "ctr_pct": 5.0,
            "avd_pct": 45.0,
            "first_60s_retention_pct": None,
            "first_60s_note": "audience_retention_empty_rows",
            "source": "youtube_analytics",
        },
        apply_actions=False,
    )
    assert insight.vs_benchmark["first_60s"].get("skipped") is True
    types = {p.get("type") for p in insight.proposals}
    assert "hook_failure" not in types
    assert insight.vs_benchmark["avd_pct"].get("policy") == "advisory_only"
