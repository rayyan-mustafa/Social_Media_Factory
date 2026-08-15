"""Competitor style intel extraction tests."""

from __future__ import annotations

import json
from pathlib import Path

from src.services import competitor_style_intel as csi


def test_title_style_signals_sleep_vs_drama():
    sleep = csi._title_style_signals("Fall Asleep to Tudor History")
    drama = csi._title_style_signals("What If Anne Boleyn Survived?")
    assert sleep["sleep"] is True
    assert drama["drama_what_if"] is True


def test_cluster_top_performers_napstorian(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(csi, "INTEL_PATH", tmp_path / "intel.json")
    videos = [
        {
            "video_id": "a",
            "title": "What If Rome Never Fell — Alternate Timeline",
            "views": 500000,
            "video_uploaded_at": "2026-08-01T18:00:00Z",
        },
        {
            "video_id": "b",
            "title": "What If Britain Lost WW2 Map Scenario",
            "views": 400000,
            "video_uploaded_at": "2026-08-02T20:00:00Z",
        },
    ]
    cluster = csi.cluster_top_performers(
        "napstorian",
        videos=videos,
        durations={"a": 600, "b": 720},
    )
    assert cluster["n_top"] == 2
    assert cluster["drama_ratio"] >= 0.5
    assert "hints" in cluster


def test_extract_intel_from_inspiration(tmp_path: Path, monkeypatch):
    ops = tmp_path / "ops"
    ops.mkdir()
    insp = {
        "items": [
            {
                "kind": "competitor_video",
                "video_id": "vid1",
                "blocked_title": "What If Henry VIII Spared Anne",
                "video_views": 900000,
                "video_uploaded_at": "2026-07-01T12:00:00Z",
                "good_video": True,
            }
        ]
    }
    (ops / "last_inspiration.json").write_text(json.dumps(insp), encoding="utf-8")
    monkeypatch.setattr(csi, "OPS_DIR", ops)
    monkeypatch.setattr(csi, "INTEL_PATH", ops / "competitor_style_intel.json")
    monkeypatch.setattr(csi, "_fetch_yt_durations", lambda ids: {})

    out = csi.extract_competitor_style_intel("napstorian", fetch_durations=False)
    assert out["n_videos"] == 1
    assert out["cluster"]["n_top"] == 1


def test_historian_cluster_prefers_mystery_doc_over_foc(tmp_path: Path, monkeypatch):
    """FoC mega-views must not set historian length/style defaults."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    comps = {
        "competitors": [
            {
                "label": "History Calling",
                "style_role": "primary_anchor",
                "style_priority": 1,
                "eligible_for_titles": True,
            },
            {
                "label": "Fall of Civilizations",
                "style_role": "aspirational_reference",
                "style_priority": 99,
                "eligible_for_titles": False,
            },
        ]
    }
    (cfg / "competitors_napping_historian.json").write_text(
        json.dumps(comps), encoding="utf-8"
    )
    monkeypatch.setattr(csi, "CONFIG_DIR", cfg)

    videos = [
        {
            "video_id": "foc1",
            "title": "18. Egypt - Fall of the Pharaohs",
            "views": 5_000_000,
            "channel_label": "Fall of Civilizations",
            "uploaded_at": "2026-07-01T20:00:00Z",
        },
        {
            "video_id": "hc1",
            "title": "The Secret Letters of Anne Boleyn — What Really Happened?",
            "views": 80_000,
            "channel_label": "History Calling",
            "uploaded_at": "2026-07-02T19:00:00Z",
        },
        {
            "video_id": "hc2",
            "title": "The Dark History of the Tudor Court",
            "views": 60_000,
            "channel_label": "History Calling",
            "uploaded_at": "2026-07-03T19:00:00Z",
        },
        {
            "video_id": "hc3",
            "title": "Why Catherine of Aragon Still Haunts the Dynasty",
            "views": 50_000,
            "channel_label": "History Calling",
            "uploaded_at": "2026-07-04T19:00:00Z",
        },
    ]
    cluster = csi.cluster_top_performers(
        "napping_historian",
        videos=videos,
        durations={"foc1": 12000, "hc1": 2100, "hc2": 1800, "hc3": 2400},
    )
    assert cluster["dominant_form"] == "mystery_doc"
    assert cluster["dominant_form"] != "epic_aspirational"
    hints = cluster.get("hints") or {}
    pacing = hints.get("pacing") or {}
    assert pacing.get("format_mode") == "standard"
    assert int(pacing.get("target_length_band_max_s") or 0) <= 2700
    assert "Fall of the Pharaohs" not in " ".join(cluster.get("top_titles") or [])