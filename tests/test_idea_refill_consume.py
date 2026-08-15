"""Tests for consume-replace idea refill count logic."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_refill_need_max_of_stock_and_consume():
    from src.agents.idea_stock import refill_need_for_channel

    # Buffer only
    assert refill_need_for_channel(stock=10, target=15, consume_credits=0) == {
        "stock_need": 5,
        "consume_need": 0,
        "need": 5,
    }
    # Consume replace even when stock full
    assert refill_need_for_channel(stock=15, target=15, consume_credits=3) == {
        "stock_need": 0,
        "consume_need": 3,
        "need": 3,
    }
    # Both — take the larger
    assert refill_need_for_channel(stock=12, target=15, consume_credits=2) == {
        "stock_need": 3,
        "consume_need": 2,
        "need": 3,
    }
    assert refill_need_for_channel(stock=14, target=15, consume_credits=4) == {
        "stock_need": 1,
        "consume_need": 4,
        "need": 4,
    }
    # Nothing to do
    assert refill_need_for_channel(stock=15, target=15, consume_credits=0)["need"] == 0


def test_credit_and_debit_refill_persists(tmp_path: Path):
    from src.agents.idea_stock import (
        credit_consume_refills,
        debit_consume_refills,
        load_refill_credits,
    )

    bal = credit_consume_refills(
        {"napstorian": 2, "napping_historian": 1}, root=tmp_path
    )
    assert bal == {"napstorian": 2, "napping_historian": 1}
    assert load_refill_credits(root=tmp_path) == bal

    bal2 = debit_consume_refills({"napstorian": 1}, root=tmp_path)
    assert bal2 == {"napstorian": 1, "napping_historian": 1}

    bal3 = debit_consume_refills(
        {"napstorian": 5, "napping_historian": 1}, root=tmp_path
    )
    assert bal3 == {}


def test_refill_harvests_consume_credits_when_stock_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("SHEET_CHANNELS", "napstorian,napping_historian")
    monkeypatch.setenv("PUBLISH_SCHEDULE", "1")
    monkeypatch.setenv("IDEA_STOCK_TARGET", "2")

    import src.agents.idea_stock as idea_mod
    import src.agents.sheet_channels as sc
    import src.agents.title_queue as tq
    from src.services import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    path_fn = lambda ch: tmp_path / f"title_queue_{ch}.csv"
    monkeypatch.setattr(sc, "local_queue_path", path_fn)
    monkeypatch.setattr(tq, "local_queue_path", path_fn)
    monkeypatch.setattr(sc, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")
    monkeypatch.setattr(tq, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")

    credits_root = tmp_path / "ops"
    credits_root.mkdir()
    idea_mod.credit_consume_refills(
        {"napstorian": 2, "napping_historian": 1}, root=credits_root
    )

    q = tq.TitleQueue()
    for ch, n in (("napstorian", 2), ("napping_historian", 2)):
        q.append_titles(
            [
                {
                    "title": f"Stock {ch} {i}",
                    "policy_ok": True,
                    "approved": False,
                    "trend_score": 0.4,
                }
                for i in range(n)
            ],
            channel=ch,
        )

    calls: list[tuple[str | None, int | None]] = []

    class FakeTrends:
        def __init__(self, *a, **k):
            pass

        def harvest_titles(self, *, dry_run=False, channel=None, limit=None):
            calls.append((channel, limit))
            rows = [
                {
                    "title": f"Replace {channel} {i}",
                    "policy_ok": True,
                    "approved": False,
                    "trend_score": 0.6,
                    "status": "queued",
                }
                for i in range(int(limit or 1))
            ]
            q.append_titles(rows, channel=channel)
            return rows

    monkeypatch.setattr(idea_mod, "TrendsAgent", FakeTrends)
    monkeypatch.setattr(idea_mod, "OpsStore", lambda: object())
    monkeypatch.setattr(
        idea_mod,
        "OpsLedger",
        lambda *_a, **_k: type("L", (), {})(),
    )

    out = idea_mod.maybe_refill_idea_stock(
        queue=q, dry_run=False, credits_root=credits_root, skip_hygiene=True
    )
    assert out["harvested"] == 3
    assert out["channels"]["napstorian"]["need"] == 2
    assert out["channels"]["napstorian"]["consume_need"] == 2
    assert out["channels"]["napstorian"]["stock_need"] == 0
    assert out["channels"]["napping_historian"]["need"] == 1
    assert ("napstorian", 2) in calls
    assert ("napping_historian", 1) in calls
    assert idea_mod.load_refill_credits(root=credits_root) == {}
    settings_mod.get_settings.cache_clear()


def test_maybe_arm_credits_refill_per_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.agents import idea_stock as idea_mod
    from src.agents import store as store_mod
    from src.agents.schedule_agent import maybe_arm_public_approved_schedule
    from src.agents.store import OpsStore

    ops = tmp_path / "ops"
    ops.mkdir()
    monkeypatch.setattr(store_mod, "OPS_DIR", ops)
    monkeypatch.setattr(idea_mod, "OPS_DIR", ops)

    store = OpsStore(ops)
    j1 = store.create_job("Nap title", status="private")
    store.update_job(
        j1.id,
        video_id="vid_nap",
        meta={"public_approved": True, "channel": "napstorian"},
    )
    j2 = store.create_job("Hist title", status="private")
    store.update_job(
        j2.id,
        video_id="vid_hist",
        meta={"public_approved": True, "channel": "napping_historian"},
    )

    class FakeSched:
        def __init__(self, *a, **k):
            self.cfg = {"arm_coalesce_minutes": 0}

        def status(self):
            return {"ok": True}

        def assign_slot_for_job(self, **kwargs):
            job = store.get_job(kwargs["job_id"])
            ch = (job.meta or {}).get("channel")
            store.update_job(kwargs["job_id"], status="scheduled")
            return {
                "ok": True,
                "job_id": kwargs["job_id"],
                "video_id": kwargs["video_id"],
                "channel": ch,
            }

    class FakeQueue:
        def find_by_job_id(self, _job_id):
            return None

    monkeypatch.setattr("src.agents.schedule_agent.ScheduleAgent", FakeSched)

    out = maybe_arm_public_approved_schedule(
        dry_run=False,
        force=True,
        apply_youtube=False,
        coalesce_minutes=0,
        store=store,
        queue=FakeQueue(),  # type: ignore[arg-type]
    )
    assert out["armed_ok"] == 2
    assert out["armed_ok_by_channel"] == {
        "napstorian": 1,
        "napping_historian": 1,
    }
    assert out.get("refill_credits") == {
        "napstorian": 1,
        "napping_historian": 1,
    }
    assert idea_mod.load_refill_credits(root=ops) == {
        "napstorian": 1,
        "napping_historian": 1,
    }


def test_assign_slot_drops_sheet_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Successful arm archives+removes the consumed idea row (both channels)."""
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("SHEET_CHANNELS", "napstorian,napping_historian")

    import src.agents.sheet_channels as sc
    import src.agents.sheet_hygiene as hyg
    import src.agents.title_queue as tq
    from src.agents.schedule_agent import ScheduleAgent
    from src.agents.store import OpsStore
    from src.services import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    path_fn = lambda ch: tmp_path / f"title_queue_{ch}.csv"
    monkeypatch.setattr(sc, "local_queue_path", path_fn)
    monkeypatch.setattr(tq, "local_queue_path", path_fn)
    monkeypatch.setattr(sc, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")
    monkeypatch.setattr(tq, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")

    ops = tmp_path / "ops"
    ops.mkdir()
    monkeypatch.setattr(hyg, "OPS_DIR", ops)

    store = OpsStore(ops)
    job = store.create_job("What If Drop Me?", status="private")
    store.update_job(
        job.id,
        video_id="vid_drop",
        meta={"public_approved": True, "channel": "napstorian"},
    )

    q = tq.TitleQueue()
    q.append_titles(
        [
            {
                "title": "What If Drop Me?",
                "policy_ok": True,
                "approved": True,
                "public_approved": True,
                "status": "private",
                "job_id": job.id,
                "video_id": "vid_drop",
            },
            {
                "title": "What If Keep Me?",
                "policy_ok": True,
                "approved": False,
                "status": "queued",
            },
        ],
        channel="napstorian",
    )

    sched = ScheduleAgent(store=store, queue=q)
    monkeypatch.setattr(sched, "can_arm_schedule", lambda **_k: (True, "ok"))
    monkeypatch.setattr(
        sched,
        "next_slot",
        lambda **_k: __import__("datetime").datetime(
            2026, 8, 10, 4, 0, tzinfo=__import__("datetime").timezone.utc
        ),
    )

    out = sched.assign_slot_for_job(
        job_id=job.id,
        video_id="vid_drop",
        public_approved=True,
        dry_run=False,
        apply_youtube=False,
    )
    assert out["ok"] is True
    assert out.get("sheet_drop", {}).get("ok") is True
    left = q.list_rows(channel="napstorian")
    assert len(left) == 1
    assert left[0].title == "What If Keep Me?"
    assert store.get_job(job.id).status == "scheduled"
    settings_mod.get_settings.cache_clear()

