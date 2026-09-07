"""Drip queue — production outruns what the best platforms will accept."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.microstock import config, drip
from src.microstock.ledger import AssetLedger


@pytest.fixture()
def ledger(tmp_path: Path) -> AssetLedger:
    return AssetLedger(tmp_path / "ledger.json")


@pytest.fixture()
def armed(monkeypatch):
    """Enable adobe (cap 40), vecteezy (cap 200) and freepik (lifetime 200)."""
    def fake_platforms(*, enabled_only=True, tier=None):
        rows = [
            {"name": "adobe_stock", "priority": 1, "enabled": True,
             "tier": "manual_review", "daily_cap": 40},
            {"name": "vecteezy", "priority": 2, "enabled": True,
             "tier": "auto", "daily_cap": 200},
            {"name": "freepik", "priority": 6, "enabled": True,
             "tier": "manual_review", "daily_cap": 20,
             "lifetime_cap_until_approved": 200},
        ]
        if tier:
            rows = [r for r in rows if r["tier"] == tier]
        return rows

    monkeypatch.setattr(config, "load_platforms", fake_platforms)
    monkeypatch.setattr(
        config, "platform_by_name",
        lambda name: next((r for r in fake_platforms(enabled_only=False) if r["name"] == name), None),
    )
    monkeypatch.setattr(config, "release_order", lambda: ["adobe_stock", "vecteezy", "freepik"])


def _seed(ledger: AssetLedger, count: int) -> list[str]:
    ids = [f"a{i:03d}" for i in range(count)]
    for asset_id in ids:
        ledger.upsert(asset_id, stage="gate_passed", niche="b2b_saas_ui")
    return ids


def test_enqueue_is_inert_without_enabled_platforms(ledger: AssetLedger):
    _seed(ledger, 5)
    assert drip.enqueue([f"a{i:03d}" for i in range(5)], ledger=ledger)["queued"] == 0


def test_only_gate_passed_assets_are_queued(ledger: AssetLedger, armed):
    ledger.upsert("good", stage="gate_passed")
    ledger.upsert("bad", stage="gate_failed")
    result = drip.enqueue(["good", "bad"], ledger=ledger)
    assert result["queued"] == 1
    assert ledger.get("bad").get("platforms_pending") in (None, [])


def test_daily_cap_limits_release(ledger: AssetLedger, armed):
    """120 assets pending, Adobe takes 40 per day — the rest stay queued."""
    ids = _seed(ledger, 120)
    drip.enqueue(ids, ledger=ledger)
    adobe = next(r for r in drip.plan(ledger=ledger) if r["platform"] == "adobe_stock")
    assert adobe["pending"] == 120
    assert adobe["releasing"] == 40
    assert adobe["backlog"] == 80


def test_release_order_is_highest_royalty_first(ledger: AssetLedger, armed):
    drip.enqueue(_seed(ledger, 10), ledger=ledger)
    assert [r["platform"] for r in drip.plan(ledger=ledger)] == [
        "adobe_stock", "vecteezy", "freepik",
    ]


def test_allowance_shrinks_as_uploads_land(ledger: AssetLedger, armed):
    ids = _seed(ledger, 50)
    drip.enqueue(ids, ledger=ledger)
    assert drip.allowance("adobe_stock", ledger=ledger) == 40
    for asset_id in ids[:10]:
        ledger.mark_uploaded(asset_id, "adobe_stock")
    assert drip.allowance("adobe_stock", ledger=ledger) == 30


def test_freepik_lifetime_cap_is_enforced(ledger: AssetLedger, armed, monkeypatch):
    """Freepik Tier 1 allows 200 files total, whatever the daily cap says."""
    ids = _seed(ledger, 30)
    drip.enqueue(ids, ledger=ledger)
    # Pretend 195 have already gone lifetime; only 5 may follow.
    monkeypatch.setattr(AssetLedger, "uploaded_count", lambda self, p: 195 if p == "freepik" else 0)
    assert drip.allowance("freepik", ledger=ledger) == 5


def test_lifetime_cap_reached_blocks_entirely(ledger: AssetLedger, armed, monkeypatch):
    drip.enqueue(_seed(ledger, 10), ledger=ledger)
    monkeypatch.setattr(AssetLedger, "uploaded_count", lambda self, p: 200 if p == "freepik" else 0)
    assert drip.allowance("freepik", ledger=ledger) == 0


def test_backlog_drains_fifo_across_beats(ledger: AssetLedger, armed):
    ids = _seed(ledger, 60)
    drip.enqueue(ids, ledger=ledger)
    first = next(r for r in drip.plan(ledger=ledger) if r["platform"] == "adobe_stock")
    assert first["asset_ids"] == ids[:40]
    for asset_id in first["asset_ids"]:
        ledger.mark_uploaded(asset_id, "adobe_stock")
    # Next day: cap resets, remaining 20 go out.
    ledger.uploaded_today = lambda platform, day=None: 0  # type: ignore[assignment]
    second = next(r for r in drip.plan(ledger=ledger) if r["platform"] == "adobe_stock")
    assert second["asset_ids"] == ids[40:60]


def test_confirm_manual_upload_marks_delivered(ledger: AssetLedger, armed):
    ids = _seed(ledger, 3)
    drip.enqueue(ids, ledger=ledger)
    result = drip.confirm_manual_upload("adobe_stock", ids, ledger=ledger)
    assert result["confirmed"] == 3
    assert all(ledger.already_uploaded(a, "adobe_stock") for a in ids)


def test_confirm_is_idempotent(ledger: AssetLedger, armed):
    ids = _seed(ledger, 2)
    drip.enqueue(ids, ledger=ledger)
    drip.confirm_manual_upload("adobe_stock", ids, ledger=ledger)
    assert drip.confirm_manual_upload("adobe_stock", ids, ledger=ledger)["confirmed"] == 0


def test_staging_alone_does_not_mark_delivered(ledger: AssetLedger, armed, monkeypatch, tmp_path):
    """A staged Tier B batch is not delivered until a human confirms."""
    ids = _seed(ledger, 2)
    drip.enqueue(ids, ledger=ledger)
    monkeypatch.setattr(
        "src.microstock.outbox.stage_batch",
        lambda platform, assets, **kw: {"staged": len(assets), "platform": platform},
    )
    drip.release(ledger=ledger, dry_run=False)
    assert not ledger.already_uploaded(ids[0], "adobe_stock")
