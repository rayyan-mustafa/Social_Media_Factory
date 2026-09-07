"""Brief harvest and the sales feedback loop back into it."""

from __future__ import annotations

import json
import random

import pytest

from src.microstock import brief_harvest, paths, stock_smm
from src.microstock.ledger import AssetLedger


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "BRIEF_STOCK_PATH", tmp_path / "brief_stock.json")
    monkeypatch.setattr(paths, "SALES_BENCHMARKS_PATH", tmp_path / "sales.json")
    monkeypatch.setattr(stock_smm, "REPORTS_DIR", tmp_path / "reports")


def test_refill_reaches_target():
    result = brief_harvest.maybe_refill(target=20, use_llm=False)
    assert result["refilled"] and brief_harvest.stock_count() == 20


def test_refill_is_a_noop_when_stock_is_healthy():
    brief_harvest.maybe_refill(target=10, use_llm=False)
    assert brief_harvest.maybe_refill(target=5, use_llm=False)["reason"] == "stock_healthy"


def test_dry_run_changes_nothing():
    result = brief_harvest.maybe_refill(target=10, use_llm=False, dry_run=True)
    assert result["reason"] == "dry_run" and brief_harvest.stock_count() == 0


def test_consume_removes_from_stock():
    brief_harvest.maybe_refill(target=10, use_llm=False)
    assert len(brief_harvest.consume(4)) == 4
    assert brief_harvest.stock_count() == 6


def test_consume_more_than_available_is_safe():
    brief_harvest.maybe_refill(target=3, use_llm=False)
    assert len(brief_harvest.consume(99)) == 3
    assert brief_harvest.stock_count() == 0


def test_duplicate_subjects_are_not_added_twice():
    brief = {"subject": "cloud rack", "prompt": "p", "niche": "b2b_saas_ui"}
    assert brief_harvest.add([brief]) == 1
    assert brief_harvest.add([brief]) == 0


def test_briefs_carry_the_vectorisation_constraints():
    brief_harvest.maybe_refill(target=5, use_llm=False)
    prompt = brief_harvest.load_stock()[0]["prompt"]
    assert "no text" in prompt and "white background" in prompt


def test_sales_bias_is_clamped_both_ways():
    paths.SALES_BENCHMARKS_PATH.write_text(json.dumps({"niches": {
        "b2b_saas_ui": {"score": 1000.0},
        "abstract_geometric": {"score": 0.0001},
    }}), encoding="utf-8")
    bias = brief_harvest.load_sales_bias()
    assert bias["b2b_saas_ui"] <= brief_harvest.MAX_SALES_BIAS
    assert bias["abstract_geometric"] >= brief_harvest.MIN_SALES_BIAS


def test_no_sales_history_means_no_bias():
    assert brief_harvest.load_sales_bias() == {}


def test_winning_niche_is_picked_more_often():
    paths.SALES_BENCHMARKS_PATH.write_text(json.dumps({"niches": {
        "b2b_saas_ui": {"score": 10.0},
        "abstract_geometric": {"score": 0.01},
    }}), encoding="utf-8")
    rng = random.Random(11)
    picks = [brief_harvest.pick_niche(rng)["name"] for _ in range(600)]
    assert picks.count("b2b_saas_ui") > picks.count("abstract_geometric") * 3


def test_report_ingestion_scores_niches(tmp_path):
    ledger = AssetLedger(tmp_path / "l.json")
    for i in range(2):
        ledger.upsert(f"s{i}", niche="b2b_saas_ui")
        ledger.queue_for_platforms(f"s{i}", ["vecteezy"])
    stock_smm.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (stock_smm.REPORTS_DIR / "r.csv").write_text(
        "File Name,Downloads,Royalty (USD)\ns0.svg,4,$3.20\ns1.svg,2,$1.60\n", encoding="utf-8"
    )
    result = stock_smm.maybe_refresh_sales(ledger=ledger)
    assert result["refreshed"]
    assert result["niches"]["b2b_saas_ui"]["revenue_usd"] == pytest.approx(4.8)


def test_currency_and_thousands_separators_parse():
    assert stock_smm._to_number("$1,234.56") == pytest.approx(1234.56)
    assert stock_smm._to_number("") == 0.0
    assert stock_smm._to_number(None) == 0.0


def test_no_reports_is_a_clean_noop():
    assert stock_smm.maybe_refresh_sales()["reason"] == "no_reports"


def test_score_is_per_asset_not_absolute():
    """20 assets earning well must beat 300 assets earning badly."""
    scored = stock_smm.score_niches({
        "small": {"downloads": 0, "revenue_usd": 100.0, "assets": 20},
        "large": {"downloads": 0, "revenue_usd": 150.0, "assets": 300},
    })
    assert scored["small"]["score"] > scored["large"]["score"]
