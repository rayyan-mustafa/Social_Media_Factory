"""Unit tests: SMM channel_winners bias for Harvest (no network)."""

from __future__ import annotations

from src.agents.smm_harvest_bridge import (
    apply_winner_bias_to_row,
    classify_evergreen,
    load_evergreen_cfg,
    load_smm_winner_signals,
    prefer_queries_for_winners,
    summarize_bias,
    tag_winner_evergreen,
    winner_bias_for_title,
)
from src.agents.store import OpsStore


def _sample_bench() -> dict:
    return {
        "channel_winners": {
            "updated_at": "2026-08-08T06:15:06+00:00",
            "channel_id": "test_channel",
            "top": [
                {
                    "title": "What If Henry VIII Spared Anne Boleyn at the Last Second?",
                    "views": 1400,
                },
                {
                    "title": "Lady Jane Grey: The 13-Day Queen Mary I Had to Kill",
                    "views": 240,
                },
                {
                    "title": "Henry VIII's Horrifying Illnesses You Didn't Know!",
                    "views": 420,
                },
            ],
            "patterns": {
                "title_hooks": [
                    "The Letter Anne Boleyn Tried to Hide",
                    "why everyone feared the Henry VIII whisper",
                ],
                "median_winner_views": 420,
                "what_if_title_share": 0.3,
                "evergreen_share": 0.75,
                "evergreen_hooks": [
                    "The Letter Anne Boleyn Tried to Hide",
                ],
            },
        },
        "winner_views_median": 420,
    }


def _eg_cfg(**overrides) -> dict:
    base = {
        "evergreen_bias": True,
        "evergreen_boost": 0.08,
        "ephemeral_penalty": 0.1,
        "evergreen_query_inject": True,
        "evergreen_themes": ["Tudor court", "sealed letters", "sleep history"],
        "ephemeral_demote_patterns": [
            "this week",
            "breaking",
            "meme",
            "reacts to",
        ],
    }
    base.update(overrides)
    return load_evergreen_cfg(base)


def test_load_smm_winner_signals_entities():
    sig = load_smm_winner_signals(_sample_bench())
    assert sig["has_winners"]
    assert "Henry VIII" in sig["entities"]
    assert "Anne Boleyn" in sig["entities"]
    assert sig["titles"]
    assert sig.get("evergreen_bias") is True
    assert sig.get("evergreen_themes") or sig.get("evergreen_titles")


def test_opsstore_read_api(tmp_path):
    store = OpsStore(tmp_path / "ops")
    store.save_benchmarks(_sample_bench())
    sig = store.get_smm_winner_signals()
    assert sig["has_winners"]
    assert sig["entities"]


def test_winner_bias_boosts_matching_title():
    sig = load_smm_winner_signals(_sample_bench())
    hit = winner_bias_for_title(
        "What If Henry VIII Never Met Anne Boleyn?", sig
    )
    miss = winner_bias_for_title(
        "What If the Library of Alexandria Never Burned?", sig
    )
    assert hit["boost"] > 0
    assert hit["tag"].startswith("smm_winner_bias=")
    assert miss["boost"] < hit["boost"]


def test_apply_bias_and_summarize():
    sig = load_smm_winner_signals(_sample_bench())
    rows = [
        apply_winner_bias_to_row(
            {
                "title": "What If Henry VIII Spared Thomas Cromwell?",
                "trend_score": 0.55,
                "notes": "original",
            },
            sig,
        ),
        apply_winner_bias_to_row(
            {
                "title": "What If Rome Never Fell?",
                "trend_score": 0.55,
                "notes": "original",
            },
            sig,
        ),
    ]
    counts = summarize_bias(rows)
    assert counts["biased"] >= 1
    assert "smm_winner_bias=" in (rows[0].get("notes") or "")
    assert "evergreen" in counts


def test_prefer_queries_orders_winner_themes():
    sig = load_smm_winner_signals(_sample_bench())
    qs = prefer_queries_for_winners(
        ["viking saga documentary", "Henry VIII Tudor court what if"],
        sig,
    )
    assert qs
    assert "Henry" in qs[0] or "henry" in qs[0].lower() or "Tudor" in qs[0]


def test_winner_signals_channel_scoped_no_cross_bleed():
    """Historian harvest must not silently use napstorian winners."""
    bench = {
        "channel_winners": {
            "updated_at": "2026-08-08T06:15:06+00:00",
            "channel": "napstorian",
            "top": [{"title": "What If Anne Boleyn Lived?", "views": 1400}],
            "patterns": {"title_hooks": ["What If Anne Boleyn Lived?"]},
        },
        "channel_winners_by_channel": {
            "napstorian": {
                "updated_at": "2026-08-08T06:15:06+00:00",
                "channel": "napstorian",
                "top": [{"title": "What If Anne Boleyn Lived?", "views": 1400}],
                "patterns": {"title_hooks": ["What If Anne Boleyn Lived?"]},
            },
            "napping_historian": {
                "updated_at": "2026-08-08T06:15:06+00:00",
                "channel": "napping_historian",
                "top": [{"title": "What If Rome Never Fell?", "views": 90}],
                "patterns": {"title_hooks": ["What If Rome Never Fell?"]},
            },
        },
    }
    hist = load_smm_winner_signals(bench, channel="napping_historian")
    nap = load_smm_winner_signals(bench, channel="napstorian")
    assert hist["has_winners"] is True
    assert "Rome" in hist["titles"][0]
    assert "Anne" in nap["titles"][0]
    assert hist["titles"][0] != nap["titles"][0]
    # Empty historian by_channel → no napstorian bleed
    empty_hist = dict(bench)
    empty_hist["channel_winners_by_channel"] = {
        "napstorian": bench["channel_winners_by_channel"]["napstorian"]
    }
    bare = load_smm_winner_signals(empty_hist, channel="napping_historian")
    assert bare["has_winners"] is False


def test_classify_evergreen_vs_ephemeral():
    eg2 = classify_evergreen(
        "The Sealed Letter of the Tudor Court",
        channel="napping_historian",
        evergreen_cfg=_eg_cfg(),
    )
    ep = classify_evergreen(
        "Breaking: Tudor Meme Reacts to This Week's News",
        channel="napstorian",
        evergreen_cfg=_eg_cfg(),
    )
    assert eg2["kind"] == "evergreen"
    assert eg2["score"] > 0
    assert ep["kind"] == "ephemeral"
    assert ep["score"] < eg2["score"]


def test_evergreen_bias_boosts_lasting_title():
    cfg = _eg_cfg()
    sig = load_smm_winner_signals(_sample_bench(), evergreen_cfg=cfg)
    lasting = winner_bias_for_title(
        "What If Henry VIII Never Opened Anne Boleyn's Sealed Letter?",
        sig,
        channel="napstorian",
        evergreen_cfg=cfg,
    )
    flash = winner_bias_for_title(
        "Breaking: Henry VIII Meme Reacts to This Week's News",
        sig,
        channel="napstorian",
        evergreen_cfg=cfg,
    )
    assert lasting["boost"] > flash["boost"]
    assert lasting["evergreen_kind"] == "evergreen"
    assert flash["evergreen_kind"] == "ephemeral"
    assert any("evergreen" in m for m in lasting["matched"])


def test_evergreen_bias_off_skips_extra_lift():
    cfg_off = _eg_cfg(evergreen_bias=False)
    off = winner_bias_for_title(
        "The Sealed Letter of the Tudor Court",
        None,
        evergreen_cfg=cfg_off,
    )
    on = winner_bias_for_title(
        "The Sealed Letter of the Tudor Court",
        None,
        evergreen_cfg=_eg_cfg(),
    )
    assert off["boost"] == 0.0
    assert on["boost"] > 0
    assert on["evergreen_kind"] == "evergreen"


def test_prefer_queries_demotes_ephemeral():
    cfg = _eg_cfg()
    sig = load_smm_winner_signals(_sample_bench(), evergreen_cfg=cfg)
    qs = prefer_queries_for_winners(
        [
            "breaking tudor meme this week",
            "Henry VIII Tudor court sealed letter",
            "viking saga documentary",
        ],
        sig,
        invent_style="what_if",
        channel="napstorian",
        evergreen_cfg=cfg,
    )
    assert qs
    assert "breaking" not in qs[0].lower()
    assert any(
        "tudor" in q.lower() or "henry" in q.lower() or "letter" in q.lower()
        for q in qs[:3]
    )


def test_tag_winner_evergreen_share():
    tagged = tag_winner_evergreen(
        [
            {"title": "The Letter Anne Boleyn Tried to Hide", "views": 100},
            {"title": "Breaking Meme Reacts to This Week's News", "views": 50},
        ],
        channel="napstorian",
        evergreen_cfg=_eg_cfg(),
    )
    assert tagged["evergreen_share"] >= 0.5
    assert tagged["ephemeral_share"] >= 0.5
    assert tagged["top"][0]["evergreen_kind"] == "evergreen"
    assert tagged["top"][1]["evergreen_kind"] == "ephemeral"


def test_historian_sleep_angle_classified_evergreen():
    eg = classify_evergreen(
        "Calm Tudor Court Secrets for Sleep — Sealed Letters",
        channel="napping_historian",
        evergreen_cfg=_eg_cfg(),
    )
    assert eg["kind"] == "evergreen"
    assert eg["channel_angle"] == "sleep_bedtime_history"
