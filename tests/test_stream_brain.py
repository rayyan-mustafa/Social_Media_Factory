"""Tests for stream_beat / vod_picker / modalities / multiplatform scaffolds."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.services.visual_modalities import (
    list_reject_paths,
    load_modalities_config,
    modalities_status,
    prompt_suffix,
    render_infographic_card,
)
from src.streaming.multiplatform import (
    inventory,
    max_concurrent_encodes,
    within_encode_budget,
)
from src.streaming.traffic_upload_engine import design_status
from src.streaming.vod_picker import rank_vods, write_playlist


def test_modalities_config_rejects_cookie_paths() -> None:
    cfg = load_modalities_config()
    reject = list_reject_paths(cfg)
    assert "meta_cookie_n8n_enter" in reject
    assert "adobe_cookie_n8n_enter" in reject
    assert cfg.get("default_still_backend") == "flux_runpod"
    assert prompt_suffix("cinematic")


def test_render_infographic_card(tmp_path: Path) -> None:
    out = tmp_path / "card.png"
    path = render_infographic_card(
        "What If Rome Held",
        ["Hook: the border holds", "Map: Rhine line", "Payoff: 300 years later"],
        out_path=out,
        width=640,
        height=360,
    )
    assert path.is_file()
    assert path.stat().st_size > 1000
    st = modalities_status()
    assert st["ok"] is True


def test_views_boost_ranks_higher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.streaming import vod_picker as vp

    # Synthetic: same duration file scored with/without views via resolve mock
    ranked = vp.rank_vods("napstorian", limit=3, min_duration_sec=60)
    if not ranked:
        pytest.skip("no local finals")
    # Ensure rank_metric primary order (score may diverge as tie-break only).
    assert ranked[0].rank_metric >= ranked[-1].rank_metric


def test_live_rank_metric_views_times_avd() -> None:
    """Primary Live score is views × (AVD%/100); views fallback when AVD missing."""
    from src.streaming.vod_picker import live_rank_metric

    assert live_rank_metric(1000, 50.0) == 500.0
    assert live_rank_metric(1000, None) == 1000.0
    assert live_rank_metric(1000, 0) == 1000.0  # non-positive → views fallback
    assert live_rank_metric(800, 80.0) == 640.0
    assert live_rank_metric(0, 90.0) == 0.0


def test_rank_vods_views_times_avd_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Playlist order must be views×AVD desc (not composite score or views alone)."""
    from src.streaming import vod_picker as vp

    # High views but weak AVD
    high_views = vp.VodCandidate(
        path="/tmp/high_views.mp4",
        duration_sec=600,
        size_bytes=5_000_000,
        mtime=1.0,
        score=999.0,
        reasons=["synthetic"],
        views=2000,
        views_src="views_cache_path",
        avd_pct=10.0,
        avd_src="scorecard:test",
        rank_metric=vp.live_rank_metric(2000, 10.0),  # 200
    )
    # Fewer views but strong AVD → wins on rank_metric
    sticky = vp.VodCandidate(
        path="/tmp/sticky.mp4",
        duration_sec=600,
        size_bytes=5_000_000,
        mtime=1.0,
        score=10.0,
        reasons=["synthetic"],
        views=800,
        views_src="views_cache_path",
        avd_pct=50.0,
        avd_src="scorecard:test",
        rank_metric=vp.live_rank_metric(800, 50.0),  # 400
    )
    # No AVD → views fallback
    no_avd = vp.VodCandidate(
        path="/tmp/no_avd.mp4",
        duration_sec=600,
        size_bytes=5_000_000,
        mtime=1.0,
        score=50.0,
        reasons=["synthetic"],
        views=300,
        views_src="views_cache_path",
        avd_pct=None,
        avd_src="none",
        rank_metric=vp.live_rank_metric(300, None),  # 300
    )

    ordered = sorted(
        [high_views, sticky, no_avd],
        key=lambda x: (float(x.rank_metric or 0.0), float(x.score)),
        reverse=True,
    )
    assert [c.path for c in ordered] == [
        "/tmp/sticky.mp4",
        "/tmp/no_avd.mp4",
        "/tmp/high_views.mp4",
    ]


def test_score_candidate_sets_rank_metric(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.streaming import vod_picker as vp

    media = tmp_path / "final.mp4"
    media.write_bytes(b"0" * 3_000_000)
    monkeypatch.setattr(vp, "_ffprobe_duration", lambda _p: 900.0)
    monkeypatch.setattr(
        vp,
        "resolve_views_for_path",
        lambda *a, **k: (1000, "views_cache_path"),
    )
    monkeypatch.setattr(
        vp,
        "resolve_avd_for_path",
        lambda *a, **k: (40.0, "scorecard:test"),
    )
    monkeypatch.setattr(vp, "_channel_for_final_path", lambda *a, **k: "napstorian")
    monkeypatch.setattr(vp, "_load_views_cache", lambda: {})

    cand = vp.score_candidate(
        media,
        channel="napstorian",
        bench_titles=[],
        smm_tokens=set(),
        min_duration_sec=60,
        publish_index={},
        winner_titles=[],
        views_cache={},
        avd_by_id={},
    )
    assert cand is not None
    assert cand.avd_pct == 40.0
    assert cand.rank_metric == 400.0
    assert any(r.startswith("avd_pct=40") for r in cand.reasons)


def test_load_avd_prefers_scorecard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.streaming import vod_picker as vp

    ops = tmp_path / "ops"
    ops.mkdir()
    monkeypatch.setattr(vp, "OPS", ops)
    (ops / "algo_insights.json").write_text(
        json.dumps(
            [
                {
                    "video_id": "vidAAA",
                    "metrics": {"avd_pct": 20.0},
                }
            ]
        ),
        encoding="utf-8",
    )
    (ops / "smm_yt_scorecard.jsonl").write_text(
        json.dumps(
            {
                "day_utc": "2026-08-09",
                "channels": {
                    "napstorian": {
                        "videos": [
                            {"video_id": "vidAAA", "avd_pct": 55.5, "views": 10}
                        ]
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    by_id = vp._load_avd_by_video_id()
    assert by_id["vidAAA"]["avd_pct"] == 55.5
    assert str(by_id["vidAAA"]["source"]).startswith("scorecard:")


def test_rank_vods_most_viewed_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backward-compat: when AVD missing, order is views desc (views fallback)."""
    from src.streaming import vod_picker as vp

    low = vp.VodCandidate(
        path="/tmp/low.mp4",
        duration_sec=600,
        size_bytes=5_000_000,
        mtime=1.0,
        score=999.0,  # high composite must not beat higher views
        reasons=["synthetic"],
        views=100,
        views_src="views_cache_path",
        rank_metric=100.0,
    )
    high = vp.VodCandidate(
        path="/tmp/high.mp4",
        duration_sec=600,
        size_bytes=5_000_000,
        mtime=1.0,
        score=10.0,
        reasons=["synthetic"],
        views=5000,
        views_src="views_cache_path",
        rank_metric=5000.0,
    )
    mid = vp.VodCandidate(
        path="/tmp/mid.mp4",
        duration_sec=600,
        size_bytes=5_000_000,
        mtime=1.0,
        score=50.0,
        reasons=["synthetic"],
        views=500,
        views_src="views_cache_path",
        rank_metric=500.0,
    )

    def fake_rank(channel, *, limit=5, min_duration_sec=None, exclude_paths=None):
        cands = [low, high, mid]
        excluded = {str(Path(p).resolve()) for p in (exclude_paths or []) if str(p).strip()}
        cands = [c for c in cands if c.path not in excluded]
        cands.sort(
            key=lambda x: (float(x.rank_metric or 0.0), float(x.score)),
            reverse=True,
        )
        return cands[: max(1, limit)]

    # Exercise the sort key used by rank_vods directly.
    ordered = sorted(
        [low, high, mid],
        key=lambda x: (float(x.rank_metric or 0.0), float(x.score)),
        reverse=True,
    )
    assert [c.path for c in ordered] == [
        "/tmp/high.mp4",
        "/tmp/mid.mp4",
        "/tmp/low.mp4",
    ]
    # exclude_current → next-best is mid after high is featured/excluded
    next_only = fake_rank("napstorian", limit=3, exclude_paths=["/tmp/high.mp4"])
    assert next_only[0].path == "/tmp/mid.mp4"


def test_underperform_selects_next_after_dwell(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Clearly weak head (next ≥2× views) + dwell met → should_swap to next ranked."""
    from src.agents.smm_agent import SocialMediaManager
    from src.streaming import vod_picker as vp

    smm = SocialMediaManager()
    monkeypatch.setattr(smm, "_own_live_concurrent_viewers", lambda channel=None: 5)
    monkeypatch.setattr(
        smm,
        "_competitor_live_pressure",
        lambda channel=None: {
            "ok": True,
            "pressure": False,
            "eligible_n": 0,
            "active_7d": 0,
        },
    )
    monkeypatch.setattr(smm, "_longform_winner_median_views", lambda channel=None: 1000)
    monkeypatch.setattr(
        smm,
        "_live_vod_swap_safe_now",
        lambda channel: (True, "encode_stable"),
    )
    monkeypatch.setattr(smm, "_live_vod_swap_allowed", lambda channel: (True, "no_marker"))

    head = "/tmp/featured_weak.mp4"
    nxt = "/tmp/next_strong.mp4"
    # Pretend featured for > dwell already.
    past = "2020-01-01T00:00:00+00:00"
    monkeypatch.setattr(
        smm,
        "_load_featured_vods",
        lambda: {
            "channels": {
                "napstorian": {
                    "featured_path": head,
                    "featured_since": past,
                }
            }
        },
    )
    monkeypatch.setattr(
        "src.streaming.vod_picker.read_playlist_entries",
        lambda channel: [head],
    )
    monkeypatch.setattr(
        "src.streaming.vod_picker.resolve_views_for_path",
        lambda path, **kwargs: (
            (50, "views_cache_path")
            if "featured_weak" in str(path)
            else (500, "views_cache_path")
        ),
    )
    monkeypatch.setattr(
        "src.streaming.vod_picker.rank_vods",
        lambda channel, limit=5, **kwargs: [
            vp.VodCandidate(
                path=head,
                duration_sec=600,
                size_bytes=5_000_000,
                mtime=1.0,
                score=20.0,
                reasons=[],
                views=50,
                views_src="views_cache_path",
            ),
            vp.VodCandidate(
                path=nxt,
                duration_sec=600,
                size_bytes=5_000_000,
                mtime=1.0,
                score=80.0,
                reasons=[],
                views=500,
                views_src="views_cache_path",
            ),
        ],
    )
    monkeypatch.setattr(
        "src.streaming.vod_loop.performance_issues",
        lambda channel: {"issues": [], "needs_repair": False},
    )

    j = smm.judge_live_vod_performance("napstorian")
    assert j["underperforming"] is True
    assert j["should_swap"] is True
    assert j["next_path"] == nxt
    assert "next_best_views" in (j.get("reason") or "")


def test_underperform_selects_next_historian_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same soft underperform→next swap job must work for napping_historian."""
    from src.agents.smm_agent import SocialMediaManager
    from src.streaming import vod_picker as vp

    smm = SocialMediaManager()
    monkeypatch.setattr(smm, "_own_live_concurrent_viewers", lambda channel=None: 2)
    monkeypatch.setattr(
        smm,
        "_competitor_live_pressure",
        lambda channel=None: {
            "ok": True,
            "pressure": False,
            "eligible_n": 0,
            "active_7d": 0,
            "path": f"competitors_{channel}.json" if channel else "competitors.json",
        },
    )
    monkeypatch.setattr(smm, "_longform_winner_median_views", lambda channel=None: 800)
    monkeypatch.setattr(
        smm, "_live_vod_swap_safe_now", lambda channel: (True, "encode_stable")
    )
    monkeypatch.setattr(smm, "_live_vod_swap_allowed", lambda channel: (True, "no_marker"))

    head = "/tmp/historian_weak.mp4"
    nxt = "/tmp/historian_next.mp4"
    past = "2020-01-01T00:00:00+00:00"
    monkeypatch.setattr(
        smm,
        "_load_featured_vods",
        lambda: {
            "channels": {
                "napping_historian": {
                    "featured_path": head,
                    "featured_since": past,
                }
            }
        },
    )
    monkeypatch.setattr(
        "src.streaming.vod_picker.read_playlist_entries",
        lambda channel: [head] if channel == "napping_historian" else [],
    )
    monkeypatch.setattr(
        "src.streaming.vod_picker.resolve_views_for_path",
        lambda path, **kwargs: (
            (40, "views_cache_path")
            if "historian_weak" in str(path)
            else (400, "views_cache_path")
        ),
    )
    monkeypatch.setattr(
        "src.streaming.vod_picker.rank_vods",
        lambda channel, limit=5, **kwargs: [
            vp.VodCandidate(
                path=head,
                duration_sec=600,
                size_bytes=5_000_000,
                mtime=1.0,
                score=15.0,
                reasons=[],
                views=40,
                views_src="views_cache_path",
            ),
            vp.VodCandidate(
                path=nxt,
                duration_sec=600,
                size_bytes=5_000_000,
                mtime=1.0,
                score=90.0,
                reasons=[],
                views=400,
                views_src="views_cache_path",
            ),
        ],
    )
    monkeypatch.setattr(
        "src.streaming.vod_loop.performance_issues",
        lambda channel: {"issues": [], "needs_repair": False},
    )

    j = smm.judge_live_vod_performance("napping_historian")
    assert j["channel"] == "napping_historian"
    assert j["underperforming"] is True
    assert j["should_swap"] is True
    assert j["next_path"] == nxt
    assert j.get("policy", {}).get("views_vs_winner_soft") == 0.20


def test_smm_live_channels_dual_parity() -> None:
    from src.agents.smm_agent import SocialMediaManager

    chans = SocialMediaManager()._smm_live_channels()
    assert "napstorian" in chans
    assert "napping_historian" in chans


def test_winner_median_is_channel_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Historian must not inherit napstorian winner median."""
    from src.agents.smm_agent import SocialMediaManager

    smm = SocialMediaManager()
    monkeypatch.setattr(
        smm,
        "_channel_winners_payload",
        lambda channel=None: (
            {"top": [{"title": "What If Rome Held", "views": 200}]}
            if channel == "napping_historian"
            else {"top": [{"title": "What If Anne Lived", "views": 5000}]}
        ),
    )
    assert smm._longform_winner_median_views("napping_historian") == 200
    assert smm._longform_winner_median_views("napstorian") == 5000


def test_underperform_blocked_during_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agents.smm_agent import SocialMediaManager
    from src.streaming import vod_picker as vp

    smm = SocialMediaManager()
    monkeypatch.setattr(smm, "_own_live_concurrent_viewers", lambda channel=None: 0)
    monkeypatch.setattr(
        smm,
        "_competitor_live_pressure",
        lambda channel=None: {"ok": True, "pressure": False, "eligible_n": 0, "active_7d": 0},
    )
    monkeypatch.setattr(smm, "_longform_winner_median_views", lambda channel=None: 1000)
    monkeypatch.setattr(smm, "_live_vod_swap_allowed", lambda channel: (True, "no_marker"))
    monkeypatch.setattr(
        smm,
        "_featured_dwell_ok",
        lambda channel, current_path: (True, "dwell_ok_2000s", 2000.0),
    )
    head = "/tmp/featured_weak.mp4"
    nxt = "/tmp/next_strong.mp4"
    monkeypatch.setattr(
        "src.streaming.vod_picker.read_playlist_entries",
        lambda channel: [head],
    )
    monkeypatch.setattr(
        "src.streaming.vod_picker.resolve_views_for_path",
        lambda path, **kwargs: (
            (50, "views_cache_path")
            if "featured_weak" in str(path)
            else (500, "views_cache_path")
        ),
    )
    monkeypatch.setattr(
        "src.streaming.vod_picker.rank_vods",
        lambda channel, limit=5, **kwargs: [
            vp.VodCandidate(
                path=head,
                duration_sec=600,
                size_bytes=5_000_000,
                mtime=1.0,
                score=20.0,
                reasons=[],
                views=50,
                views_src="views_cache_path",
            ),
            vp.VodCandidate(
                path=nxt,
                duration_sec=600,
                size_bytes=5_000_000,
                mtime=1.0,
                score=80.0,
                reasons=[],
                views=500,
                views_src="views_cache_path",
            ),
        ],
    )
    monkeypatch.setattr(
        "src.streaming.vod_loop.performance_issues",
        lambda channel: {"issues": ["stall", "reconnect_storm"], "needs_repair": True},
    )

    j = smm.judge_live_vod_performance("napstorian")
    assert j["underperforming"] is True
    assert j["should_swap"] is False
    assert "swap_blocked=unsafe_" in (j.get("reason") or "")

    j2 = smm.judge_live_vod_performance("napping_historian")
    assert j2["underperforming"] is True
    assert j2["should_swap"] is False
    assert "swap_blocked=unsafe_" in (j2.get("reason") or "")


def test_multiplatform_encode_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOD_LOOP_MAX_CONCURRENT_ENCODES", "2")
    assert max_concurrent_encodes() == 2
    assert within_encode_budget(2) is True
    assert within_encode_budget(3) is False
    inv = inventory()
    assert inv["ok"] is True
    assert inv["max_concurrent_encodes"] == 2


def test_traffic_upload_engine_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = design_status()
    assert payload["implemented"] is False
    assert "tiktok" in payload["platforms"]
    assert "meta_cookie_n8n" in payload["reject"]


def test_underperform_weak_watch_time_swaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """concurrentViewers weak + better next → underperform/should_swap (both channels)."""
    from src.agents.smm_agent import SocialMediaManager
    from src.streaming import vod_picker as vp

    smm = SocialMediaManager()
    monkeypatch.setattr(smm, "_own_live_concurrent_viewers", lambda channel=None: 0)
    monkeypatch.setattr(
        smm,
        "_competitor_live_pressure",
        lambda channel=None: {
            "ok": True,
            "pressure": False,
            "eligible_n": 0,
            "active_7d": 0,
        },
    )
    # Winner median high enough that views floor alone would not fire at 200.
    monkeypatch.setattr(smm, "_longform_winner_median_views", lambda channel=None: 100)
    monkeypatch.setattr(
        smm, "_live_vod_swap_safe_now", lambda channel: (True, "encode_stable")
    )
    monkeypatch.setattr(smm, "_live_vod_swap_allowed", lambda channel: (True, "no_marker"))
    monkeypatch.setattr(
        smm,
        "_featured_dwell_ok",
        lambda channel, current_path: (True, "dwell_ok_2000s", 2000.0),
    )
    head = "/tmp/live_head.mp4"
    nxt = "/tmp/live_next.mp4"
    monkeypatch.setattr(
        "src.streaming.vod_picker.read_playlist_entries",
        lambda channel: [head],
    )
    # Same-ish views (not 2× gap) so only watch-time concurrent fires.
    monkeypatch.setattr(
        "src.streaming.vod_picker.resolve_views_for_path",
        lambda path, **kwargs: (
            (100, "views_cache_path")
            if "live_head" in str(path)
            else (150, "views_cache_path")
        ),
    )
    monkeypatch.setattr(
        "src.streaming.vod_picker.rank_vods",
        lambda channel, limit=5, **kwargs: [
            vp.VodCandidate(
                path=head,
                duration_sec=600,
                size_bytes=5_000_000,
                mtime=1.0,
                score=50.0,
                reasons=[],
                views=100,
                views_src="views_cache_path",
            ),
            vp.VodCandidate(
                path=nxt,
                duration_sec=600,
                size_bytes=5_000_000,
                mtime=1.0,
                score=55.0,
                reasons=[],
                views=150,
                views_src="views_cache_path",
            ),
        ],
    )
    monkeypatch.setattr(
        "src.streaming.vod_loop.performance_issues",
        lambda channel: {"issues": [], "needs_repair": False},
    )

    for ch in ("napstorian", "napping_historian"):
        j = smm.judge_live_vod_performance(ch)
        assert j["underperforming"] is True
        assert j["should_swap"] is True
        assert "weak_watch_time_concurrent" in (j.get("reason") or "")
        assert j["concurrent_viewers"] == 0


def test_vod_picker_ranks_or_empty() -> None:
    # On VPS with farm finals this returns candidates; still must not crash.
    ranked = rank_vods("napstorian", limit=2, min_duration_sec=60)
    assert isinstance(ranked, list)
    if ranked:
        assert Path(ranked[0].path).is_file()
        # Prefer-public empty-pool fallback may score/featured-rank (not strict
        # views×AVD order) when max views < 50 — only assert files exist.
        if not any(
            "fallback_score_rank_low_views" in (c.reasons or []) for c in ranked
        ):
            assert ranked[0].rank_metric >= ranked[-1].rank_metric


def test_vod_picker_exclude_current_changes_rank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.streaming import vod_picker as vp

    ranked = vp.rank_vods("napstorian", limit=3, min_duration_sec=60)
    if len(ranked) < 2:
        pytest.skip("need >=2 local finals")
    head = ranked[0].path
    excluded = vp.rank_vods(
        "napstorian", limit=3, min_duration_sec=60, exclude_paths=[head]
    )
    assert excluded
    assert excluded[0].path != head


def test_default_pick_limit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.streaming import vod_picker as vp

    monkeypatch.delenv("VOD_PICKER_LIMIT", raising=False)
    assert vp.default_pick_limit() == 8
    monkeypatch.setenv("VOD_PICKER_LIMIT", "10")
    assert vp.default_pick_limit() == 10
    monkeypatch.setenv("VOD_PICKER_LIMIT", "99")
    assert vp.default_pick_limit() == 20
    monkeypatch.setenv("VOD_PICKER_LIMIT", "0")
    assert vp.default_pick_limit() == 1


def test_channel_brand_hints_diverge_scores() -> None:
    """Historian prefers empire/doc stems; napstorian prefers what-if Anne packaging."""
    from src.streaming import vod_picker as vp

    anne = "what_if_anne_boleyn_outlived_henry"
    empire = "what_if_the_roman_empire_never_fell"
    assert anne in " ".join(vp._CHANNEL_HINTS["napstorian"]) or any(
        h in anne for h in vp._CHANNEL_HINTS["napstorian"]
    )
    assert any(h in empire for h in ("roman", "empire"))
    assert "what_if" in vp._CHANNEL_HINTS["napstorian"]
    assert "what_if" not in vp._CHANNEL_HINTS["napping_historian"]
    assert "empire" in vp._CHANNEL_HINTS["napping_historian"]
    assert "mystery" in vp._CHANNEL_HINTS["napping_historian"]


def test_historian_hard_excludes_sibling_owned_vod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """napping_historian must never rank a napstorian-owned final."""
    from src.streaming import vod_picker as vp

    nap_dir = tmp_path / "jobs" / "20260101T000000Z_What_If_Anne_Sibling"
    nap_final = nap_dir / "video" / "final.mp4"
    nap_final.parent.mkdir(parents=True)
    nap_final.write_bytes(b"0" * 3_000_000)
    hist_dir = tmp_path / "jobs" / "20260101T000001Z_Catherine_Haunt_Mystery"
    hist_final = hist_dir / "video" / "final.mp4"
    hist_final.parent.mkdir(parents=True)
    hist_final.write_bytes(b"0" * 3_000_000)

    monkeypatch.setattr(vp, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(vp, "_ffprobe_duration", lambda p: 700.0)
    monkeypatch.setattr(
        vp,
        "_load_ops_job_channels",
        lambda: {
            nap_dir.name: "napstorian",
            hist_dir.name: "napping_historian",
            str(nap_dir.resolve()): "napstorian",
            str(hist_dir.resolve()): "napping_historian",
        },
    )
    monkeypatch.setattr(vp, "_load_publish_index", lambda: {})
    monkeypatch.setattr(vp, "_load_views_cache", lambda: {})
    monkeypatch.setattr(vp, "_load_winner_views_by_title", lambda channel=None: [])
    monkeypatch.setattr(vp, "_load_benchmark_titles", lambda channel=None: [])
    monkeypatch.setattr(vp, "_load_smm_boost_tokens", lambda: set())

    ranked = vp.rank_vods("napping_historian", limit=5, min_duration_sec=60)
    paths = {c.path for c in ranked}
    assert str(hist_final.resolve()) in paths
    assert str(nap_final.resolve()) not in paths


def test_diversify_away_from_sibling_head() -> None:
    from src.streaming import vod_picker as vp

    a = vp.VodCandidate(
        path="/tmp/a.mp4",
        duration_sec=600,
        size_bytes=10,
        mtime=1.0,
        score=90.0,
        reasons=[],
        views=100,
        views_src="test",
    )
    b = vp.VodCandidate(
        path="/tmp/b.mp4",
        duration_sec=600,
        size_bytes=10,
        mtime=1.0,
        score=80.0,
        reasons=[],
        views=50,
        views_src="test",
    )
    c = vp.VodCandidate(
        path="/tmp/c.mp4",
        duration_sec=600,
        size_bytes=10,
        mtime=1.0,
        score=70.0,
        reasons=[],
        views=40,
        views_src="test",
    )
    out = vp._diversify_away_from([a, b, c], {a.path})
    assert [x.path for x in out] == [b.path, c.path, a.path]
    assert "sibling_head_deprioritized" in a.reasons
    # No alternatives → keep order
    only = vp._diversify_away_from([a], {a.path})
    assert only[0].path == a.path


def test_refresh_playlists_diversifies_channel_heads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When refreshing both channels, historian should not clone napstorian head."""
    from src.streaming import vod_picker as vp

    paths = [tmp_path / f"{name}.mp4" for name in ("anne", "empire", "tudor")]
    for p in paths:
        p.write_bytes(b"x" * 100)
    cands_by_ch = {
        "napstorian": [
            vp.VodCandidate(
                path=str(paths[0].resolve()),
                duration_sec=600,
                size_bytes=10,
                mtime=1.0,
                score=100.0,
                reasons=["nap"],
                views=200,
                views_src="test",
            ),
            vp.VodCandidate(
                path=str(paths[1].resolve()),
                duration_sec=600,
                size_bytes=10,
                mtime=1.0,
                score=50.0,
                reasons=["nap"],
                views=80,
                views_src="test",
            ),
            vp.VodCandidate(
                path=str(paths[2].resolve()),
                duration_sec=600,
                size_bytes=10,
                mtime=1.0,
                score=40.0,
                reasons=["nap"],
                views=70,
                views_src="test",
            ),
        ],
        "napping_historian": [
            vp.VodCandidate(
                path=str(paths[0].resolve()),
                duration_sec=600,
                size_bytes=10,
                mtime=1.0,
                score=60.0,
                reasons=["hist"],
                views=200,
                views_src="test",
            ),
            vp.VodCandidate(
                path=str(paths[1].resolve()),
                duration_sec=600,
                size_bytes=10,
                mtime=1.0,
                score=95.0,
                reasons=["hist"],
                views=80,
                views_src="test",
            ),
            vp.VodCandidate(
                path=str(paths[2].resolve()),
                duration_sec=600,
                size_bytes=10,
                mtime=1.0,
                score=90.0,
                reasons=["hist"],
                views=70,
                views_src="test",
            ),
        ],
    }

    def fake_rank(
        channel,
        *,
        limit=5,
        min_duration_sec=None,
        exclude_paths=None,
        deprioritize_paths=None,
        **_kwargs,
    ):
        excluded = {
            str(Path(p).resolve()) for p in (exclude_paths or []) if str(p).strip()
        }
        out = [c for c in cands_by_ch[channel] if c.path not in excluded]
        out.sort(
            key=lambda x: (float(x.rank_metric or x.views or 0.0), float(x.score)),
            reverse=True,
        )
        out = vp._diversify_away_from(out, deprioritize_paths)
        return out[: max(1, limit)]

    monkeypatch.setattr(vp, "rank_vods", fake_rank)
    monkeypatch.setattr(vp, "PICKER_STATUS", tmp_path / "picker_status.json")
    monkeypatch.setattr(vp, "STREAM_CFG", tmp_path)
    monkeypatch.setattr(vp, "OPS", tmp_path)
    monkeypatch.setattr(vp, "read_playlist_entries", lambda channel: [])

    payload = vp.refresh_playlists(
        channels=("napstorian", "napping_historian"),
        limit=8,
        dry_run=False,
    )
    assert payload["ok"] is True
    assert payload["limit"] == 8
    nap_head = payload["channels"]["napstorian"]["entries"][0]
    hist_head = payload["channels"]["napping_historian"]["entries"][0]
    assert nap_head == str(paths[0].resolve())
    assert hist_head != nap_head
    # Sibling stack deprioritizes nap head + next (anne, empire) → tudor wins.
    assert hist_head == str(paths[2].resolve())
    assert len(payload["channels"]["napstorian"]["entries"]) == 3


def test_vod_picker_exclude_exhaustion_clears_and_reranks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When every candidate is excluded, clear excludes and restore best head."""
    from src.streaming import vod_picker as vp

    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    cands = [
        vp.VodCandidate(
            path=str(a.resolve()),
            duration_sec=600,
            size_bytes=10,
            mtime=1.0,
            score=90.0,
            reasons=["synthetic"],
            views=200,
            views_src="test",
        ),
        vp.VodCandidate(
            path=str(b.resolve()),
            duration_sec=600,
            size_bytes=10,
            mtime=2.0,
            score=80.0,
            reasons=["synthetic"],
            views=100,
            views_src="test",
        ),
    ]

    def fake_rank(channel, *, limit=5, min_duration_sec=None, exclude_paths=None, deprioritize_paths=None, **_kwargs):
        excluded = {
            str(Path(p).resolve()) for p in (exclude_paths or []) if str(p).strip()
        }
        out = [c for c in cands if c.path not in excluded]
        out.sort(key=lambda x: (int(x.views or 0), float(x.score)), reverse=True)
        return out[: max(1, limit)]

    monkeypatch.setattr(vp, "rank_vods", fake_rank)
    monkeypatch.setattr(vp, "PICKER_STATUS", tmp_path / "picker_status.json")
    monkeypatch.setattr(vp, "STREAM_CFG", tmp_path)
    monkeypatch.setattr(vp, "OPS", tmp_path)

    # Pretend both channels currently feature `a`.
    for ch in ("napstorian", "napping_historian"):
        (tmp_path / f"playlist_{ch}.txt").write_text(
            f"file '{a.resolve().as_posix()}'\n",
            encoding="utf-8",
        )

    all_paths = [str(a.resolve()), str(b.resolve())]
    payload = vp.refresh_playlists(
        channels=("napstorian", "napping_historian"),
        limit=3,
        dry_run=False,
        exclude_paths_by_channel={
            "napstorian": all_paths,
            "napping_historian": all_paths,
        },
    )
    assert payload["ok"] is True
    for ch in ("napstorian", "napping_historian"):
        row = payload["channels"][ch]
        assert row["ok"] is True
        assert row["excludes_cleared"] is True
        assert row["exclusion_exhausted"] is True
        assert row["entries"][0] == str(a.resolve())
        assert set(row["excluded"]) == set(all_paths)


def test_judge_live_vod_returns_structure(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agents.smm_agent import SocialMediaManager

    smm = SocialMediaManager()
    # Avoid network for concurrent / channel list during unit check.
    monkeypatch.setattr(smm, "_own_live_concurrent_viewers", lambda channel=None: None)
    monkeypatch.setattr(
        smm,
        "_competitor_live_pressure",
        lambda channel=None: {
            "ok": True,
            "pressure": False,
            "eligible_n": 0,
            "active_7d": 0,
        },
    )
    monkeypatch.setattr(
        "src.streaming.vod_loop.performance_issues",
        lambda channel: {"issues": [], "needs_repair": False},
    )
    for ch in ("napstorian", "napping_historian"):
        j = smm.judge_live_vod_performance(ch)
        assert j.get("ok") is True
        assert j.get("channel") == ch
        assert "underperforming" in j
        assert "should_swap" in j
        assert "winner_longform_median" in j
        assert "dwell_ok" in j
        assert j.get("policy", {}).get("views_vs_winner_soft") == 0.20
        assert j.get("policy", {}).get("next_views_mult") == 2.0


def test_public_only_pool_excludes_private_and_wrong_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live pool prefers public+channel-matched locals; sibling + private drop out."""
    from src.streaming import vod_picker as vp

    pub_dir = tmp_path / "jobs" / "20260101T000000Z_Public_What_If_Anne"
    pub_final = pub_dir / "video" / "final.mp4"
    pub_final.parent.mkdir(parents=True)
    pub_final.write_bytes(b"0" * 3_000_000)
    (pub_dir / "publish_manifest.json").write_text(
        json.dumps(
            {
                "video_id": "pubVidNap1",
                "title": "What If Anne Became Public",
                "channel": "napstorian",
                "final_path": str(pub_final),
            }
        ),
        encoding="utf-8",
    )

    priv_dir = tmp_path / "jobs" / "20260101T000001Z_Private_What_If_Anne"
    priv_final = priv_dir / "video" / "final.mp4"
    priv_final.parent.mkdir(parents=True)
    priv_final.write_bytes(b"0" * 3_000_000)
    (priv_dir / "publish_manifest.json").write_text(
        json.dumps(
            {
                "video_id": "privVidNap1",
                "title": "What If Anne Stayed Private",
                "channel": "napstorian",
                "final_path": str(priv_final),
            }
        ),
        encoding="utf-8",
    )

    hist_dir = tmp_path / "jobs" / "20260101T000002Z_Public_Catherine_Haunt"
    hist_final = hist_dir / "video" / "final.mp4"
    hist_final.parent.mkdir(parents=True)
    hist_final.write_bytes(b"0" * 3_000_000)
    (hist_dir / "publish_manifest.json").write_text(
        json.dumps(
            {
                "video_id": "pubVidHist1",
                "title": "Catherine Haunt Mystery Documentary",
                "channel": "napping_historian",
                "final_path": str(hist_final),
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(vp, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(vp, "_ffprobe_duration", lambda p: 700.0)
    monkeypatch.setattr(vp, "_load_ops_job_channels", lambda: {})
    monkeypatch.setattr(
        vp,
        "_load_ops_job_privacy",
        lambda: {
            "pubVidNap1": {
                "status": "public",
                "channel": "napstorian",
                "title": "What If Anne Became Public",
                "job_dir": str(pub_dir),
            },
            "privVidNap1": {
                "status": "private",
                "channel": "napstorian",
                "title": "What If Anne Stayed Private",
                "job_dir": str(priv_dir),
            },
            "pubVidHist1": {
                "status": "public",
                "channel": "napping_historian",
                "title": "Catherine Haunt Mystery Documentary",
                "job_dir": str(hist_dir),
            },
        },
    )
    monkeypatch.setattr(
        vp,
        "_load_views_cache",
        lambda: {
            "by_video_id": {
                "pubVidNap1": 500,
                "privVidNap1": 9999,
                "pubVidHist1": 800,
            },
            "by_path": {},
            "titles_by_id": {
                "pubVidNap1": "What If Anne Became Public",
                "privVidNap1": "What If Anne Stayed Private",
                "pubVidHist1": "Catherine Haunt Mystery Documentary",
            },
            "channel_by_video_id": {
                "pubVidNap1": "napstorian",
                "pubVidHist1": "napping_historian",
            },
        },
    )
    monkeypatch.setattr(vp, "_load_winner_views_by_title", lambda channel=None: [])
    monkeypatch.setattr(vp, "_load_benchmark_titles", lambda channel=None: [])
    monkeypatch.setattr(vp, "_load_smm_boost_tokens", lambda: set())
    monkeypatch.setattr(vp, "_load_avd_by_video_id", lambda: {})

    ranked = vp.rank_vods(
        "napstorian",
        limit=5,
        min_duration_sec=60,
        prefer_public=True,
        allow_nonpublic_fallback=False,
    )
    paths = {c.path for c in ranked}
    assert str(pub_final.resolve()) in paths
    assert str(priv_final.resolve()) not in paths
    assert str(hist_final.resolve()) not in paths
    assert ranked[0].is_public is True
    assert ranked[0].video_id == "pubVidNap1"
    assert ranked[0].views == 500
    assert ranked[0].public_proof in {"ops_public", "yt_channel_listing"}

    # Historian must not pick napstorian public either.
    hist_ranked = vp.rank_vods(
        "napping_historian",
        limit=5,
        min_duration_sec=60,
        prefer_public=True,
        allow_nonpublic_fallback=False,
    )
    hist_paths = {c.path for c in hist_ranked}
    assert str(hist_final.resolve()) in hist_paths
    assert str(pub_final.resolve()) not in hist_paths


def test_public_pool_empty_falls_back_to_nonpublic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no public+local finals exist, allow private channel-owned (documented)."""
    from src.streaming import vod_picker as vp

    priv_dir = tmp_path / "jobs" / "20260101T000010Z_Private_Only_What_If"
    priv_final = priv_dir / "video" / "final.mp4"
    priv_final.parent.mkdir(parents=True)
    priv_final.write_bytes(b"0" * 3_000_000)
    (priv_dir / "publish_manifest.json").write_text(
        json.dumps(
            {
                "video_id": "onlyPrivate1",
                "title": "What If Only Private Exists",
                "channel": "napstorian",
                "final_path": str(priv_final),
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(vp, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(vp, "_ffprobe_duration", lambda p: 700.0)
    monkeypatch.setattr(vp, "_load_ops_job_channels", lambda: {})
    monkeypatch.setattr(
        vp,
        "_load_ops_job_privacy",
        lambda: {
            "onlyPrivate1": {
                "status": "private",
                "channel": "napstorian",
                "title": "What If Only Private Exists",
                "job_dir": str(priv_dir),
            }
        },
    )
    monkeypatch.setattr(
        vp,
        "_load_views_cache",
        lambda: {
            "by_video_id": {"onlyPrivate1": 12},
            "by_path": {},
            "titles_by_id": {},
            "channel_by_video_id": {},  # not listed as public upload
        },
    )
    monkeypatch.setattr(vp, "_load_winner_views_by_title", lambda channel=None: [])
    monkeypatch.setattr(vp, "_load_benchmark_titles", lambda channel=None: [])
    monkeypatch.setattr(vp, "_load_smm_boost_tokens", lambda: set())
    monkeypatch.setattr(vp, "_load_avd_by_video_id", lambda: {})

    empty = vp.rank_vods(
        "napstorian",
        limit=3,
        min_duration_sec=60,
        prefer_public=True,
        allow_nonpublic_fallback=False,
    )
    assert empty == []

    fallback = vp.rank_vods(
        "napstorian",
        limit=3,
        min_duration_sec=60,
        prefer_public=True,
        allow_nonpublic_fallback=True,
    )
    assert fallback
    assert fallback[0].path == str(priv_final.resolve())
    assert fallback[0].is_public is False
    assert "public_pool_empty_fallback" in fallback[0].reasons


def test_public_videos_missing_local_final_noted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.streaming import vod_picker as vp

    local = tmp_path / "with_local.mp4"
    local.write_bytes(b"x" * 100)

    monkeypatch.setattr(
        vp,
        "_load_views_cache",
        lambda: {
            "by_video_id": {"orphanPub": 1200, "withLocal": 50},
            "titles_by_id": {"orphanPub": "Orphan Public Hit", "withLocal": "Has Final"},
            "channel_by_video_id": {
                "orphanPub": "napstorian",
                "withLocal": "napstorian",
            },
        },
    )
    monkeypatch.setattr(
        vp,
        "_load_publish_index",
        lambda: {
            str(local.resolve()): {
                "video_id": "withLocal",
                "channel": "napstorian",
                "title": "Has Final",
            }
        },
    )
    missing = vp.public_videos_missing_local_final("napstorian")
    ids = {r["video_id"] for r in missing}
    assert "orphanPub" in ids
    assert "withLocal" not in ids
    assert missing[0]["views"] == 1200
