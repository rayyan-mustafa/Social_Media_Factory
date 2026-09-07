"""Stage chain and the unattended beat."""

from __future__ import annotations

import pytest

from src.microstock import brief_harvest, config, paths, stock_smm
from src.microstock.ledger import AssetLedger
from src.microstock.pipeline import process_asset, process_batch, summarise
from src.microstock.stock_factory import run_beat


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name, sub in (
        ("RAW_PNG_DIR", "raw"), ("TRACED_SVG_DIR", "traced"), ("CLEAN_SVG_DIR", "clean"),
        ("REJECTED_DIR", "rejected"), ("OPS_DIR", "ops"), ("OUTBOX_DIR", "outbox"),
        ("OUTPUT_DIR", "out"),
    ):
        monkeypatch.setattr(paths, name, tmp_path / sub)
    monkeypatch.setattr(paths, "ASSET_LEDGER_PATH", tmp_path / "ops" / "ledger.json")
    monkeypatch.setattr(paths, "BRIEF_STOCK_PATH", tmp_path / "ops" / "briefs.json")
    monkeypatch.setattr(paths, "SALES_BENCHMARKS_PATH", tmp_path / "ops" / "sales.json")
    monkeypatch.setattr(paths, "BEAT_LAST_PATH", tmp_path / "ops" / "beat_last.json")
    monkeypatch.setattr(paths, "_MANAGED_DIRS", tuple(tmp_path / s for s in
        ("out", "raw", "traced", "clean", "rejected", "outbox", "ops")))
    monkeypatch.setattr(stock_smm, "REPORTS_DIR", tmp_path / "ops" / "reports")


def test_single_asset_runs_the_whole_chain():
    result = process_asset("flat vector cloud icon", niche="b2b_saas_ui", backend="mock")
    assert result.passed
    assert result.stage == "gate_passed"
    assert result.png.is_file() and result.clean_svg.is_file()


def test_asset_id_is_content_derived_and_stable():
    a = process_asset("identical prompt", backend="mock")
    b = process_asset("identical prompt", backend="mock")
    assert a.asset_id == b.asset_id


def test_ledger_records_every_stage():
    result = process_asset("flat vector server", backend="mock")
    record = AssetLedger().get(result.asset_id)
    assert record["stage"] == "gate_passed"
    assert record["clean_svg"] and record["png"]


def test_unreadable_source_fails_without_crashing(tmp_path):
    bad = tmp_path / "not_an_image.png"
    bad.write_text("nope", encoding="utf-8")
    result = process_asset("x", backend="mock", source_png=bad)
    assert not result.passed and result.error


def test_missing_source_is_reported():
    result = process_asset("x", backend="mock", source_png="/nonexistent/x.png")
    assert not result.passed and "not found" in result.error


def test_batch_summarises_correctly():
    results = process_batch(
        [{"prompt": f"flat vector icon {i}", "niche": "b2b_saas_ui"} for i in range(3)],
        backend="mock",
    )
    summary = summarise(results)
    assert summary["total"] == 3 and summary["passed"] == 3
    assert summary["cost_usd"] == 0.0


def test_beat_refuses_while_master_switch_is_off(monkeypatch):
    monkeypatch.setattr(config, "is_enabled", lambda: False)
    result = run_beat()
    assert result["ran"] is False and "master switch" in result["reason"]


def test_beat_runs_end_to_end_with_gates_skipped():
    result = run_beat(max_assets=2, backend="mock", skip_gates=True, use_llm=False)
    assert result["ran"] is True
    assert result["steps"]["produce"]["passed"] == 2


def test_beat_writes_a_snapshot():
    run_beat(max_assets=1, backend="mock", skip_gates=True, use_llm=False)
    assert paths.BEAT_LAST_PATH.is_file()


def test_beat_refills_briefs_before_producing():
    run_beat(max_assets=2, backend="mock", skip_gates=True, use_llm=False)
    assert brief_harvest.stock_count() > 0


def test_beat_distribution_stays_inert_while_disarmed():
    result = run_beat(max_assets=1, backend="mock", skip_gates=True, use_llm=False)
    assert result["steps"]["enqueue"]["reason"] == "no_enabled_platforms"
    assert result["steps"]["release"]["platforms"] == []


def test_beat_dry_run_produces_nothing():
    result = run_beat(max_assets=2, backend="mock", skip_gates=True, use_llm=False, dry_run=True)
    assert result["steps"]["produce"]["reason"] == "dry_run"


def test_tagging_failure_does_not_lose_the_asset():
    """No tagger key is the normal state — assets must stay at gate_passed."""
    result = run_beat(max_assets=1, backend="mock", skip_gates=True, use_llm=False)
    assert result["steps"]["produce"]["passed"] == 1
    assert result["ledger_counts"].get("gate_passed") == 1
