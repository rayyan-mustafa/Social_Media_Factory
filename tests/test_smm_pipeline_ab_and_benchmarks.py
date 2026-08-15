"""Benchmark agent + SMM pipeline A/B ledger tests."""

from __future__ import annotations

import json
from pathlib import Path

from src.services import benchmark_agent as ba
from src.services import smm_pipeline_ab as ab


def test_set_benchmark_median(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ba, "BENCHMARKS", tmp_path / "benchmarks.jsonl")
    hist = [
        {"video_id": "a", "views_by_day": [10, 20, 30, 40, 50, 60, 70], "subs_at_upload": 100, "niche_tag": "tudor"},
        {"video_id": "b", "views_by_day": [30, 40, 50, 60, 70, 80, 90], "subs_at_upload": 100, "niche_tag": "tudor"},
        {"video_id": "c", "views_by_day": [1000], "subs_at_upload": 100, "niche_tag": "other"},
    ]
    out = ba.set_benchmark("v1", "napstorian", hist, niche_tag="tudor", current_subs=100)
    assert out["projected"]["day1"] == 20  # median of 10,30
    assert out["n_peers"] == 2


def test_diagnose_content_before_distribution(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ba, "PERFORMANCE_LOG", tmp_path / "perf.jsonl")
    d = ba.diagnose_underperformance(
        "v1",
        metrics={"ctr": 1.0, "ctr_channel_avg": 4.0, "avd_pct": 40, "avd_channel_avg": 40},
    )
    assert d["verdict"] == "likely_content_issue"


def test_diagnose_distribution_signature(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ba, "PERFORMANCE_LOG", tmp_path / "perf.jsonl")
    d = ba.diagnose_underperformance(
        "v1",
        metrics={
            "ctr": 5.0,
            "ctr_channel_avg": 4.0,
            "avd_pct": 45,
            "avd_channel_avg": 40,
            "impressions": 100,
            "impressions_channel_avg": 5000,
            "browse_suggested_pct": 5,
            "browse_suggested_channel_avg": 40,
        },
    )
    assert d["verdict"] == "likely_distribution_suppression"


def test_experiment_works_and_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ab, "EXPERIMENTS", tmp_path / "exp.jsonl")
    monkeypatch.setattr(ab, "WORKS", tmp_path / "works.jsonl")
    monkeypatch.setattr(ab, "FAILS", tmp_path / "fails.jsonl")
    monkeypatch.setattr(ab, "PLAYBOOK", tmp_path / "playbook.md")
    monkeypatch.setattr(ab, "CODE_CHANGELOG", tmp_path / "code.jsonl")
    monkeypatch.setattr(ab, "CONSENT_QUEUE", tmp_path / "consent.md")

    opened = ab.open_experiment(
        channel="napstorian",
        video_id_or_job="vid1",
        lever="soft_packaging",
        baseline_metrics={"ctr": 2.0, "avd_pct": 40},
    )
    assert opened["ok"]
    eid = opened["experiment"]["id"]
    # second open blocked
    assert ab.open_experiment(
        channel="napstorian",
        video_id_or_job="vid1",
        lever="hook",
        baseline_metrics={"ctr": 2.0},
    )["ok"] is False

    ev = ab.evaluate_experiment(eid, {"ctr": 5.0, "avd_pct": 41})
    assert ev["outcome"] == "works"
    assert (tmp_path / "works.jsonl").is_file()


def test_coding_snapshot_restore_needs_consent(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ab, "CODE_SNAPSHOTS", tmp_path / "snaps")
    monkeypatch.setattr(ab, "CODE_CHANGELOG", tmp_path / "code.jsonl")
    monkeypatch.setattr(ab, "CONSENT_QUEUE", tmp_path / "consent.md")
    monkeypatch.setattr(ab, "OPS_DIR", tmp_path)
    monkeypatch.setattr(ab, "WORKS", tmp_path / "works.jsonl")
    monkeypatch.setattr(ab, "FAILS", tmp_path / "fails.jsonl")
    monkeypatch.setattr(ab, "PLAYBOOK", tmp_path / "playbook.md")
    monkeypatch.setattr(ab, "ROOT", tmp_path)

    src = tmp_path / "config" / "prompts" / "hook.txt"
    src.parent.mkdir(parents=True)
    src.write_text("OLD\n", encoding="utf-8")
    snap = ab.snapshot_coding([src], reason="test")
    src.write_text("NEW\n", encoding="utf-8")
    blocked = ab.apply_coding_restore_if_consented(snap["id"])
    assert blocked["ok"] is False
    forced = ab.apply_coding_restore_if_consented(snap["id"], force=True)
    assert forced["ok"] is True
    assert src.read_text(encoding="utf-8") == "OLD\n"


def test_detect_winner_lock_requires_ctr_and_first60():
    goals = {
        "channels": {
            "napstorian": {
                "ctr_pct_min": 4.0,
                "avd_pct_min": 40.0,
                "first_60s_retention_pct_min": 70.0,
            }
        }
    }
    # AVD-only → provisional, not hard lock
    d = ab.detect_winner_lock_candidate(
        {"avd_pct": 55.0, "ctr_pct": None, "first_60s_retention_pct": None},
        channel="napstorian",
        goals=goals,
    )
    assert d["lock"] is False
    assert d["provisional"] is True

    # CTR + first60 + AVD meet goals → hard lock
    d2 = ab.detect_winner_lock_candidate(
        {"avd_pct": 45.0, "ctr_pct": 5.0, "first_60s_retention_pct": 72.0},
        channel="napstorian",
        goals=goals,
    )
    assert d2["lock"] is True
    assert "avd" in d2["reason"]

    # CTR + first60 ok but AVD present and below → no lock
    d3 = ab.detect_winner_lock_candidate(
        {"avd_pct": 20.0, "ctr_pct": 5.0, "first_60s_retention_pct": 72.0},
        channel="napstorian",
        goals=goals,
    )
    assert d3["lock"] is False


def test_lock_winner_skips_open_experiment(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ab, "EXPERIMENTS", tmp_path / "exp.jsonl")
    monkeypatch.setattr(ab, "WORKS", tmp_path / "works.jsonl")
    monkeypatch.setattr(ab, "FAILS", tmp_path / "fails.jsonl")
    monkeypatch.setattr(ab, "PLAYBOOK", tmp_path / "playbook.md")
    monkeypatch.setattr(ab, "LOCKED_VIDEOS", tmp_path / "locked.json")
    monkeypatch.setattr(ab, "LOCKED_WINNERS_JSONL", tmp_path / "locked.jsonl")
    monkeypatch.setattr(ab, "CODE_CHANGELOG", tmp_path / "code.jsonl")
    monkeypatch.setattr(ab, "CONSENT_QUEUE", tmp_path / "consent.md")

    opened = ab.open_experiment(
        channel="napstorian",
        video_id_or_job="nIBtbpyU1rk",
        lever="soft_packaging",
        baseline_metrics={"ctr": 2.0},
    )
    assert opened["ok"]

    locked = ab.lock_video_winner(
        video_id="nIBtbpyU1rk",
        channel="napstorian",
        metrics={"ctr_pct": 5.0, "avd_pct": 50.0, "first_60s_retention_pct": 75.0},
        reason="goals_ctr_first60_avd",
        title="SOP win",
        format_cue="napstorian_new_format_10m_sop",
    )
    assert locked["newly_locked"] is True
    assert ab.is_video_locked("nIBtbpyU1rk")
    assert (tmp_path / "locked.json").is_file()
    works = (tmp_path / "works.jsonl").read_text(encoding="utf-8")
    assert "packaging+format" in works
    assert "new_format_sop" in works

    blocked = ab.open_experiment(
        channel="napstorian",
        video_id_or_job="nIBtbpyU1rk",
        lever="hook",
        baseline_metrics={"ctr": 5.0},
    )
    assert blocked["ok"] is False
    assert blocked["reason"] == "video_locked_winner"


def test_maybe_lock_from_scorecard(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ab, "EXPERIMENTS", tmp_path / "exp.jsonl")
    monkeypatch.setattr(ab, "WORKS", tmp_path / "works.jsonl")
    monkeypatch.setattr(ab, "FAILS", tmp_path / "fails.jsonl")
    monkeypatch.setattr(ab, "PLAYBOOK", tmp_path / "playbook.md")
    monkeypatch.setattr(ab, "LOCKED_VIDEOS", tmp_path / "locked.json")
    monkeypatch.setattr(ab, "LOCKED_WINNERS_JSONL", tmp_path / "locked.jsonl")

    payload = {
        "channels": {
            "napstorian": {
                "videos": [
                    {
                        "video_id": "nIBtbpyU1rk",
                        "title": "10m SOP",
                        "ctr_pct": 4.5,
                        "avd_pct": 48.0,
                        "first_60s_retention_pct": 71.0,
                    },
                    {
                        "video_id": "avd_only_vid",
                        "title": "pending CTR",
                        "ctr_pct": None,
                        "avd_pct": 55.0,
                        "first_60s_retention_pct": None,
                    },
                ]
            }
        }
    }
    out = ab.maybe_lock_winner_from_scorecard(payload)
    assert out["n_newly_locked"] == 1
    assert out["newly_locked"][0]["video_id"] == "nIBtbpyU1rk"
    assert out["n_provisionals"] == 1
    # Idempotent
    out2 = ab.maybe_lock_winner_from_scorecard(payload)
    assert out2["n_newly_locked"] == 0


def test_apply_vision_pass_heroes_additive(tmp_path: Path):
    from PIL import Image

    from src.services.asset_fetcher import apply_vision_pass_heroes_to_job

    job = tmp_path / "job"
    images = job / "images"
    images.mkdir(parents=True)
    # Existing Flux still that should be backed up when replaced
    flux = images / "scene_005.jpg"
    Image.new("RGB", (1280, 720), (10, 20, 30)).save(flux, format="JPEG")

    src = tmp_path / "pass.jpg"
    Image.new("RGB", (800, 600), (200, 100, 50)).save(src, format="JPEG")

    script = {
        "scenes": [
            {"index": i, "chapter_id": (i // 5) + 1} for i in range(0, 40)
        ]
    }
    (job / "script").mkdir()
    (job / "script" / "script.json").write_text(
        json.dumps(script), encoding="utf-8"
    )

    report = apply_vision_pass_heroes_to_job(
        job,
        [{"local_path": str(src), "title": "hero", "vision": {"confidence": 9}}],
        script=script,
        max_heroes=3,
    )
    assert report["ok"]
    assert report["fetched_hero_count"] >= 1
    assert (job / "fetched_heroes.json").is_file()
    assert (job / "pd_clippings.json").is_file()


def test_vision_threshold_helper():
    from src.services.vision_judge import PASS_THRESHOLD

    assert PASS_THRESHOLD == 8


def test_pd_clip_sources_wiki_met_flux_policy():
    import json
    from pathlib import Path

    cfg = json.loads(
        Path("config/pd_clip_sources.json").read_text(encoding="utf-8")
    )
    hybrid = cfg["hybrid"]
    assert hybrid["wiki_met_primary"] is True
    assert hybrid["flux_on_vision_reject"] is True
    assert hybrid["reuse_weak_pd"] is False
    assert hybrid.get("no_hero_product_cap") is True
    assert hybrid["auto_search"] == "rmagine_per_scene_wiki_met"
    assert "napstorian" in hybrid["channels"]
    assert "napping_historian" in hybrid["channels"]
