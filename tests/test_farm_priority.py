"""Farm priority queue — prefer ops list before sheet picks."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_enqueue_and_next_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.agents import farm_priority as fp
    from src.agents.store import OpsStore

    monkeypatch.setattr(fp, "PRIORITY_PATH", tmp_path / "farm_priority_jobs.json")
    monkeypatch.setattr(fp, "OPS_DIR", tmp_path)

    store = OpsStore(tmp_path / "ops")
    j1 = store.create_job("Tudor stale", status="scheduled", stage="scheduled")
    store.update_job(
        j1.id,
        video_id="XN-E5w1vw7s",
        job_dir=str(tmp_path / "j1"),
        meta={"channel": "napstorian", "selected_pack": True, "new_format": True},
    )
    j2 = store.create_job("Mongols stale", status="scheduled", stage="scheduled")
    store.update_job(
        j2.id,
        video_id="ZzgIdKDAfgI",
        job_dir=str(tmp_path / "j2"),
        meta={"channel": "napstorian"},
    )

    out = fp.enqueue_priority_jobs(
        [
            {"job_id": j1.id, "priority": 0, "phase": "visuals", "note": "Tudor"},
            {"job_id": j2.id, "priority": 1, "phase": "visuals", "note": "Mongols"},
        ],
        reason="test",
        store=store,
    )
    assert out["payload"]["jobs"][0]["job_id"] == j1.id
    j1b = store.get_job(j1.id)
    assert j1b.status == "queued"
    assert (j1b.meta or {}).get("farm_priority") is True
    assert (j1b.meta or {}).get("preserve_scheduled_video_id") is True
    assert (j1b.meta or {}).get("selected_pack") is True
    assert j1b.video_id == "XN-E5w1vw7s"

    nxt = fp.next_priority_job(store)
    assert nxt is not None
    assert nxt["job_id"] == j1.id
    assert nxt["phase"] == "visuals"


def test_enqueue_fresh_upload_clears_preserve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.agents import farm_priority as fp
    from src.agents.store import OpsStore

    monkeypatch.setattr(fp, "PRIORITY_PATH", tmp_path / "farm_priority_jobs.json")
    monkeypatch.setattr(fp, "OPS_DIR", tmp_path)

    store = OpsStore(tmp_path / "ops")
    j1 = store.create_job("Tudor fresh", status="scheduled", stage="scheduled")
    store.update_job(
        j1.id,
        video_id="XN-E5w1vw7s",
        job_dir=str(tmp_path / "j1"),
        meta={"channel": "napstorian"},
    )

    out = fp.enqueue_priority_jobs(
        [
            {
                "job_id": j1.id,
                "priority": 0,
                "phase": "visuals",
                "note": "Tudor",
                "preserve_scheduled_video_id": False,
                "schedule_at_local": "2026-08-10T04:00:00+05:00",
            },
        ],
        reason="fresh upload test",
        store=store,
    )
    assert out["payload"]["jobs"][0]["preserve_scheduled_video_id"] is False
    assert out["payload"]["jobs"][0]["publish"] is True
    j1b = store.get_job(j1.id)
    assert j1b.video_id is None
    meta = j1b.meta or {}
    assert meta.get("preserve_scheduled_video_id") is False
    assert meta.get("needs_yt_media_replace") in (False, None)
    assert meta.get("public_approved") is True
    assert meta.get("schedule_slot_locked") is True
    assert meta.get("force_publish_at_utc") == "2026-08-09T23:00:00Z"
    assert meta.get("legacy_video_id") == "XN-E5w1vw7s"


def test_assign_slot_honors_locked_force_publish(tmp_path: Path):
    from src.agents.schedule_agent import ScheduleAgent
    from src.agents.store import OpsStore

    store = OpsStore(tmp_path / "ops")
    job = store.create_job("Locked slot", status="private", stage="private")
    store.update_job(
        job.id,
        video_id="newVid12345",
        meta={
            "channel": "napstorian",
            "public_approved": True,
            "schedule_slot_locked": True,
            "force_publish_at_utc": "2026-08-09T23:00:00Z",
            "scheduled_at_local": "2026-08-10T04:00:00+05:00",
            "publish_at_utc": "2026-08-09T23:00:00Z",
        },
    )
    agent = ScheduleAgent(store=store)
    agent.cfg = {
        "timezone": "Asia/Karachi",
        "publish_hour_local": 8,
        "publish_minute_local": 0,
        "cadence_days": 1,
        "require_public_approved_for_all": True,
        "channels": {
            "napstorian": {"publish_hour_local": 8, "preferred_hours": [8]},
        },
    }
    result = agent.assign_slot_for_job(
        job_id=job.id,
        video_id="newVid12345",
        public_approved=True,
        dry_run=True,
        apply_youtube=False,
    )
    assert result["ok"] is True
    assert result["publish_at_utc"] == "2026-08-09T23:00:00Z"
    assert "2026-08-10T04:00:00" in result["scheduled_at_local"]


def test_start_one_farm_prefers_priority(monkeypatch: pytest.MonkeyPatch):
    import src.runpod.watchdog as wd

    monkeypatch.setattr(
        wd,
        "_try_start_farm_priority_job",
        lambda: (True, "job_pri", [{"job_id": "job_pri"}], "started farm_priority"),
    )
    monkeypatch.setattr(
        wd,
        "_try_resume_hold_or_failed",
        lambda: (_ for _ in ()).throw(AssertionError("should not resume")),
    )

    started, jid, results, msg = wd._start_one_farm_job()
    assert started is True
    assert jid == "job_pri"
    assert "farm_priority" in msg
