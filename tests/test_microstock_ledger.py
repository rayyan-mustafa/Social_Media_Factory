"""Asset ledger — idempotency is what makes a crashed beat safe to resume."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.microstock.ledger import AssetLedger, sha256_file


@pytest.fixture()
def ledger(tmp_path: Path) -> AssetLedger:
    return AssetLedger(tmp_path / "ledger.json")


def test_sha256_is_stable_and_content_derived(tmp_path: Path):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"same"); b.write_bytes(b"same")
    assert sha256_file(a) == sha256_file(b)
    b.write_bytes(b"different")
    assert sha256_file(a) != sha256_file(b)


def test_upsert_merges_rather_than_replaces(ledger: AssetLedger):
    ledger.upsert("x", prompt="p", niche="n")
    ledger.upsert("x", clean_svg="/tmp/x.svg")
    record = ledger.get("x")
    assert record["prompt"] == "p" and record["clean_svg"] == "/tmp/x.svg"


def test_unknown_stage_is_rejected(ledger: AssetLedger):
    with pytest.raises(ValueError):
        ledger.set_stage("x", "not_a_real_stage")


def test_already_processed_tracks_stage_order(ledger: AssetLedger):
    ledger.set_stage("x", "traced")
    assert ledger.already_processed("x", "generated")
    assert ledger.already_processed("x", "traced")
    assert not ledger.already_processed("x", "gate_passed")


def test_never_uploads_the_same_asset_twice(ledger: AssetLedger):
    ledger.upsert("x")
    ledger.queue_for_platforms("x", ["adobe_stock"])
    assert not ledger.already_uploaded("x", "adobe_stock")
    ledger.mark_uploaded("x", "adobe_stock")
    assert ledger.already_uploaded("x", "adobe_stock")


def test_queue_skips_platforms_already_delivered(ledger: AssetLedger):
    ledger.upsert("x")
    ledger.queue_for_platforms("x", ["adobe_stock", "vecteezy"])
    ledger.mark_uploaded("x", "adobe_stock")
    ledger.queue_for_platforms("x", ["adobe_stock", "vecteezy"])
    assert ledger.get("x")["platforms_pending"] == ["vecteezy"]


def test_stage_becomes_distributed_only_when_queue_drains(ledger: AssetLedger):
    ledger.upsert("x")
    ledger.queue_for_platforms("x", ["adobe_stock", "vecteezy"])
    ledger.mark_uploaded("x", "adobe_stock")
    assert ledger.get("x")["stage"] == "queued"
    ledger.mark_uploaded("x", "vecteezy")
    assert ledger.get("x")["stage"] == "distributed"


def test_pending_for_platform_is_fifo(ledger: AssetLedger):
    for name in ("a", "b", "c"):
        ledger.upsert(name)
        ledger.queue_for_platforms(name, ["vecteezy"])
    assert [r["asset_id"] for r in ledger.pending_for_platform("vecteezy")] == ["a", "b", "c"]


def test_lifetime_count_drives_freepik_tier_cap(ledger: AssetLedger):
    for name in ("a", "b"):
        ledger.upsert(name)
        ledger.queue_for_platforms(name, ["freepik"])
        ledger.mark_uploaded(name, "freepik")
    assert ledger.uploaded_count("freepik") == 2
    assert ledger.uploaded_count("adobe_stock") == 0


def test_marking_unknown_asset_raises(ledger: AssetLedger):
    with pytest.raises(KeyError):
        ledger.mark_uploaded("ghost", "vecteezy")


def test_survives_a_corrupt_ledger_file(tmp_path: Path):
    path = tmp_path / "l.json"
    path.write_text("{ not json", encoding="utf-8")
    assert AssetLedger(path).all() == []
