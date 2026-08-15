"""Historian documentary_mystery invent vs napstorian what_if (no network)."""

from __future__ import annotations

from src.agents.smm_harvest_bridge import (
    prefer_queries_for_winners,
    winner_bias_for_title,
)
from src.agents.trends_agent import (
    _format_documentary_title,
    _format_invented_title,
    _is_what_if_title,
    _title_invent_cfg,
    _topic_to_original_title,
)


def test_title_invent_cfg_historian_defaults_documentary():
    cfg = _title_invent_cfg({}, channel="napping_historian")
    assert cfg["style"] == "documentary_mystery"
    assert cfg["forbid_what_if_prefix"] is True
    assert "What If" not in (cfg.get("query_inject_template") or "")


def test_title_invent_cfg_from_competitors_json():
    hist = _title_invent_cfg(
        {
            "title_invent": {
                "style": "documentary_mystery",
                "forbid_what_if_prefix": True,
            }
        },
        channel="napping_historian",
    )
    nap = _title_invent_cfg(
        {"title_invent": {"style": "what_if"}},
        channel="napstorian",
    )
    assert hist["style"] == "documentary_mystery"
    assert nap["style"] == "what_if"


def test_documentary_format_never_forces_what_if():
    cfg = {"style": "documentary_mystery", "forbid_what_if_prefix": True}
    assert _format_invented_title(
        "The Secret History of the Tudor Court", invent_cfg=cfg
    ).startswith("The Secret")
    cleaned = _format_documentary_title(
        "What If Anne Boleyn Survived the Tower?"
    )
    assert not _is_what_if_title(cleaned)
    assert "Anne Boleyn" in cleaned or "Dark History" in cleaned


def test_what_if_format_still_forces_prefix():
    cfg = {"style": "what_if"}
    t = _format_invented_title("Rome Never Fell", invent_cfg=cfg)
    assert t.lower().startswith("what if")
    assert t.endswith("?")


def test_topic_to_title_respects_style():
    doc = _topic_to_original_title(
        "Anne Boleyn",
        invent_cfg={"style": "documentary_mystery"},
    )
    assert doc
    assert not _is_what_if_title(doc)
    wi = _topic_to_original_title(
        "Anne Boleyn", invent_cfg={"style": "what_if"}
    )
    assert _is_what_if_title(wi)


def test_prefer_queries_documentary_skips_what_if_inject():
    signals = {
        "has_winners": True,
        "entities": ["Anne Boleyn", "Henry VIII"],
        "eras": ["Tudor"],
        "topic_phrases": ["Letter Anne Boleyn"],
    }
    qs = prefer_queries_for_winners(
        ["tudor history documentary"],
        signals,
        invent_style="documentary_mystery",
        query_inject_template="{theme} history documentary mystery",
    )
    assert qs
    assert not any(q.lower().startswith("what if") for q in qs)
    assert any("anne boleyn" in q.lower() or "documentary" in q.lower() for q in qs)


def test_prefer_queries_what_if_still_injects():
    signals = {
        "has_winners": True,
        "entities": ["Anne Boleyn"],
        "eras": [],
        "topic_phrases": [],
    }
    qs = prefer_queries_for_winners(
        ["viking saga"],
        signals,
        invent_style="what_if",
    )
    assert any(q.lower().startswith("what if") for q in qs)


def test_winner_bias_rejects_what_if_for_documentary():
    signals = {
        "has_winners": True,
        "entities": ["Anne Boleyn"],
        "eras": ["Tudor"],
        "titles": ["What If Anne Boleyn Survived?"],
        "topic_phrases": ["Anne Boleyn Survived"],
        "packaging_patterns": ["what if"],
    }
    bad = winner_bias_for_title(
        "What If Anne Boleyn Escaped the Tower?",
        signals,
        invent_style="documentary_mystery",
    )
    assert bad.get("reject") is True
    good = winner_bias_for_title(
        "The Dark Secret Anne Boleyn Took to the Tower",
        signals,
        invent_style="documentary_mystery",
    )
    assert not good.get("reject")
    assert good["boost"] > 0
