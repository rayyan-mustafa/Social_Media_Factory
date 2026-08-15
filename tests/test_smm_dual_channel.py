"""Dual-channel SMM parity: winners map, OAuth channel, harvest bias scoping."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.agents.smm_harvest_bridge import load_smm_winner_signals
from src.agents.store import OpsStore


def test_youtube_channel_ids_both_configured():
    from src.agents.smm_agent import SocialMediaManager

    smm = SocialMediaManager()
    nap = smm._youtube_channel_id_for("napstorian")
    hist = smm._youtube_channel_id_for("napping_historian")
    assert nap.startswith("UC")
    assert hist.startswith("UC")
    assert nap != hist


def test_load_smm_winner_signals_scoped_per_channel():
    bench = {
        "channel_winners": {
            "updated_at": "2026-08-08T00:00:00+00:00",
            "channel_id": "UC_nap",
            "top": [{"title": "What If Henry VIII Spared Anne Boleyn?", "views": 900}],
            "patterns": {"title_hooks": ["Henry VIII"], "median_winner_views": 900},
        },
        "channel_winners_by_channel": {
            "napstorian": {
                "updated_at": "2026-08-08T00:00:00+00:00",
                "channel_id": "UC_nap",
                "top": [{"title": "What If Henry VIII Spared Anne Boleyn?", "views": 900}],
                "patterns": {"title_hooks": ["Henry VIII"], "median_winner_views": 900},
            },
            "napping_historian": {
                "updated_at": "2026-08-08T00:00:00+00:00",
                "channel_id": "UC_hist",
                "top": [
                    {
                        "title": "What If the Mongol Empire Never Fractured?",
                        "views": 400,
                    }
                ],
                "patterns": {
                    "title_hooks": ["Mongol Empire"],
                    "median_winner_views": 400,
                },
            },
        },
    }
    nap = load_smm_winner_signals(bench, channel="napstorian")
    hist = load_smm_winner_signals(bench, channel="napping_historian")
    assert nap["has_winners"]
    assert hist["has_winners"]
    assert "Henry VIII" in nap["entities"] or any("Henry" in t for t in nap["titles"])
    assert any("Mongol" in t for t in hist["titles"])
    assert not any("Mongol" in t for t in nap["titles"])


def test_opsstore_get_smm_winner_signals_channel(tmp_path: Path):
    store = OpsStore(tmp_path / "ops")
    store.save_benchmarks(
        {
            "channel_winners_by_channel": {
                "napping_historian": {
                    "updated_at": "2026-08-08T00:00:00+00:00",
                    "top": [{"title": "What If Rome Never Fell?", "views": 10}],
                    "patterns": {"title_hooks": ["Rome"], "median_winner_views": 10},
                }
            }
        }
    )
    sig = store.get_smm_winner_signals(channel="napping_historian")
    assert sig["has_winners"]
    assert any("Rome" in t for t in sig["titles"])
    empty = store.get_smm_winner_signals(channel="napstorian")
    assert not empty["has_winners"]


def test_longform_winner_median_is_channel_scoped(tmp_path: Path, monkeypatch):
    from src.agents.smm_agent import SocialMediaManager

    store = OpsStore(tmp_path / "ops")
    store.save_benchmarks(
        {
            "channel_winners_by_channel": {
                "napstorian": {
                    "top": [
                        {"title": "A long napstorian documentary title", "views": 1000},
                        {"title": "Another long napstorian documentary", "views": 2000},
                    ],
                    "channel_median_views": 1500,
                },
                "napping_historian": {
                    "top": [
                        {"title": "A long historian documentary title here", "views": 100},
                        {"title": "Another long historian documentary", "views": 300},
                    ],
                    "channel_median_views": 200,
                },
            }
        }
    )
    smm = SocialMediaManager(store=store)
    # Implementation uses sorted[len//2] (upper mid for even counts).
    assert smm._longform_winner_median_views("napstorian") == 2000
    assert smm._longform_winner_median_views("napping_historian") == 300
    assert smm._longform_winner_median_views("napstorian") != smm._longform_winner_median_views(
        "napping_historian"
    )


def test_soft_packaging_uses_job_channel(tmp_path: Path, monkeypatch):
    from src.agents.smm_agent import SocialMediaManager
    from src.agents.store import JobRecord

    seen: dict[str, str | None] = {"channel": None}

    class FakePub:
        def __init__(self, channel=None):
            seen["channel"] = channel

        def update_packaging(self, *a, **k):
            return {"ok": True}

    monkeypatch.setattr(
        "src.services.publish_youtube.PublishModule", FakePub
    )
    monkeypatch.setattr(
        "src.services.youtube_meta_generate.generate_youtube_pack",
        lambda *a, **k: MagicMock(
            meta_dir=tmp_path,
        ),
    )
    monkeypatch.setattr(
        "src.services.youtube_meta.load_youtube_meta",
        lambda *a, **k: {
            "title": "t",
            "description": "d",
            "tags": ["a"],
            "thumbnail_path": None,
        },
    )

    job_dir = tmp_path / "job"
    (job_dir / "script").mkdir(parents=True)
    (job_dir / "script" / "script.json").write_text("{}", encoding="utf-8")
    store = OpsStore(tmp_path / "ops")
    job = JobRecord(
        id="j1",
        title="x",
        status="public",
        stage="public",
        job_dir=str(job_dir),
        video_id="vid1",
        meta={"channel": "napping_historian"},
    )
    smm = SocialMediaManager(store=store)
    out = smm._apply_soft_packaging(job, video_id="vid1")
    assert out.get("ok") is True
    assert seen["channel"] == "napping_historian"


def test_oauth_fix_payload_channel_token_path():
    from src.agents.schedule_agent import _oauth_fix_payload

    hint = _oauth_fix_payload(
        "403 insufficient authentication scopes force-ssl",
        channel="napping_historian",
    )
    assert hint is not None
    assert hint["channel"] == "napping_historian"
    assert "napping_historian" in hint["token_path"]
    assert "--channel napping_historian" in hint["reauth_command"]
