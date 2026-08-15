"""Channel empire registry, expansion gate, network revenue."""

from __future__ import annotations

from pathlib import Path


def test_empire_loads_and_production_only_core():
    from src.agents.channel_empire import (
        all_known_channel_names,
        check_channel_skin_ready,
        load_empire,
        production_channel_names,
        staged_channel_names,
    )

    emp = load_empire()
    assert emp.get("channels")
    prod = production_channel_names()
    assert "napstorian" in prod
    assert "napping_historian" in prod
    assert "art_mysteries" not in prod
    staged = staged_channel_names(wave=1)
    assert "art_mysteries" in staged
    assert "forgotten_empires" in staged
    assert "science_history" in staged
    assert "royal_courts" in staged
    names = all_known_channel_names()
    assert "money_history" in names
    skin = check_channel_skin_ready("art_mysteries")
    assert skin["ready"] is True, skin


def test_wave1_and_wave2_skins_ready():
    from src.agents.channel_empire import check_channel_skin_ready

    for ch in (
        "art_mysteries",
        "forgotten_empires",
        "science_history",
        "royal_courts",
        "business_empires",
        "money_history",
        "war_tech_history",
        "philosophy_sleep",
    ):
        skin = check_channel_skin_ready(ch)
        assert skin["ready"] is True, (ch, skin)


def test_money_disclaimer_and_business_overlay_sop_exist():
    root = Path(__file__).resolve().parents[1]
    assert (root / "config/sop/money_history_disclaimer.txt").is_file()
    assert (root / "config/sop/business_empires_overlays.txt").is_file()
    assert (root / "config/prompts/money_history/outline.txt").is_file()
    assert (root / "config/competitors_business_empires.json").is_file()


def test_core_harden_blocks_expansion_without_metrics(tmp_path, monkeypatch):
    from src.agents import channel_empire as ce

    # Force missing scorecard → bars not ok → expansion blocked
    monkeypatch.setattr(ce, "_load_json", lambda path: {})
    monkeypatch.setattr(
        ce,
        "_recent_archival_pass_rate",
        lambda limit_jobs=12: {
            "ok": True,
            "pass_rate": 0.8,
            "n_pass": 80,
            "n_scenes": 100,
            "n_jobs_scanned": 1,
            "n_rmagine_policy_jobs": 1,
        },
    )
    harden = ce.evaluate_core_harden()
    assert harden["ready_for_wave1"] is False
    assert harden["archival_ok"] is True
    gate = ce.evaluate_expansion_gate("art_mysteries")
    assert gate["allowed"] is False


def test_network_revenue_locked_until_six():
    from src.agents.network_revenue import affiliate_allowed_for_channel, layer_allowed
    from src.agents.channel_empire import network_revenue_status

    status = network_revenue_status()
    # Default state lists 2 monetized → locked
    assert status["unlocked"] is False
    assert status["monetized_channels"] < status["required"]
    ads = layer_allowed("adsense")
    assert ads["allowed"] is False
    aff = affiliate_allowed_for_channel("money_history")
    assert aff["allowed"] is False


def test_distill_empire_niches():
    from src.services.query_distill import distill_query_phases, extract_historical_figure

    assert extract_historical_figure("Marie Curie in the laboratory") == "Marie Curie"
    art = distill_query_phases("a forged masterpiece oil painting in the Louvre")
    assert "painting" in art["phase1"].lower() or "painting" in art["phase2"].lower()
    sci = distill_query_phases("brass telescope scientific instrument")
    assert "instrument" in sci["phase1"].lower() or "telescope" in sci["phase1"].lower()
    money = distill_query_phases("historical gold coin mint")
    assert "coin" in money["phase1"].lower() or "coinage" in money["phase2"].lower()


def test_configured_sheet_channels_excludes_staged(monkeypatch):
    from src.agents import sheet_channels as sc

    monkeypatch.delenv("SHEET_CHANNELS", raising=False)
    monkeypatch.setattr(
        sc,
        "_agents_cfg",
        lambda: {
            "sheet_channels": [
                {"name": "napstorian"},
                {"name": "napping_historian"},
                {
                    "name": "art_mysteries",
                    "production_enabled": False,
                    "prompts_dir": "config/prompts/art_mysteries",
                },
            ]
        },
    )
    names = sc.configured_sheet_channels()
    assert "napstorian" in names
    assert "art_mysteries" not in names
