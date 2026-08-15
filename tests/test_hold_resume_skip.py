"""HOLD-not-fail + repair-cap skip-to-next (capacity watchdog)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def test_hold_farm_job_stamps_resumable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.agents.farm import hold_farm_job
    from src.agents.store import OpsStore

    store = OpsStore(tmp_path / "ops")
    job_dir = tmp_path / "job"
    for sub in ("script", "audio", "images", "video"):
        (job_dir / sub).mkdir(parents=True)
    (job_dir / "script" / "script.json").write_text('{"scenes":[]}', encoding="utf-8")
    job = store.create_job("What If HOLD?", status="farming", stage="visuals")
    store.update_job(job.id, job_dir=str(job_dir), meta={"channel": "napstorian"})

    monkeypatch.setattr(
        "src.agents.title_queue.TitleQueue.find_by_job_id",
        lambda self, *_a, **_k: None,
    )
    out = hold_farm_job(
        job.id,
        error="RunPod HTTP 503 temporarily unavailable",
        store=store,
        hold_class="runpod_transient",
        sync_sheet=False,
    )
    assert out["status"] == "hold"
    assert out["resumable"] is True
    held = store.get_job(job.id)
    assert held.status == "hold"
    assert (held.meta or {}).get("resumable") is True
    assert (held.meta or {}).get("resume") is True
    assert (held.meta or {}).get("hold_class") == "runpod_transient"


def test_park_for_capacity_ready_for_stills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.agents.farm import park_for_capacity
    from src.agents.repair_watchdog import AUTO_RESUME_CLASSES, classify_failure
    from src.agents.store import OpsStore

    store = OpsStore(tmp_path / "ops")
    job_dir = tmp_path / "job"
    for sub in ("script", "audio", "images", "video"):
        (job_dir / sub).mkdir(parents=True)
    (job_dir / "script" / "script.json").write_text('{"scenes":[]}', encoding="utf-8")
    (job_dir / "audio" / "voice_manifest.json").write_text("{}", encoding="utf-8")
    job = store.create_job("What If CAPACITY?", status="farming", stage="visuals")
    store.update_job(job.id, job_dir=str(job_dir), meta={"channel": "napstorian"})

    monkeypatch.setattr(
        "src.agents.title_queue.TitleQueue.find_by_job_id",
        lambda self, *_a, **_k: None,
    )
    err = "OUT_OF_STOCK — NVIDIA A40/SECURE unavailable; wait for next GREEN"
    assert classify_failure(err) == "capacity_out"
    assert "capacity_out" not in AUTO_RESUME_CLASSES

    out = park_for_capacity(job.id, error=err, store=store, sync_sheet=False)
    assert out["awaiting_green"] is True
    assert out["status"] == "ready_for_stills"
    parked = store.get_job(job.id)
    assert parked.status == "ready_for_stills"
    assert parked.stage == "awaiting_gpu"
    assert (parked.meta or {}).get("awaiting_green") is True
    assert (parked.meta or {}).get("hold_class") == "capacity_out"
    assert (parked.meta or {}).get("capacity_hold") is True



def test_list_resumable_require_explicit(tmp_path: Path):
    from src.agents.farm import list_resumable_hold_jobs
    from src.agents.store import OpsStore

    store = OpsStore(tmp_path / "ops")
    stamped = store.create_job("Stamped", status="hold", stage="hold")
    store.update_job(
        stamped.id,
        meta={"resumable": True, "hold_class": "process_crash"},
        error="dead pid",
    )
    legacy = store.create_job("Legacy", status="hold", stage="hold")
    store.update_job(legacy.id, meta={}, error="old park")
    exhausted = store.create_job("Exhausted", status="hold", stage="hold")
    store.update_job(
        exhausted.id,
        meta={
            "resumable": False,
            "repair_blocked": True,
            "repair_exhausted": True,
        },
        error="max repairs",
    )

    all_cands = list_resumable_hold_jobs(store, require_explicit=False)
    ids = {j.id for j in all_cands}
    assert stamped.id in ids
    assert legacy.id in ids
    assert exhausted.id not in ids

    explicit = list_resumable_hold_jobs(store, require_explicit=True)
    assert [j.id for j in explicit] == [stamped.id]


def test_repair_cap_exhausts_then_skips_to_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from src.agents.repair_watchdog import RepairWatchdog, DEFAULT_MAX_REPAIRS
    from src.agents.store import OpsStore

    store = OpsStore(tmp_path / "ops")

    broken = store.create_job("Broken resume", status="hold", stage="hold")
    store.update_job(
        broken.id,
        error="RunPod HTTP 503 timed out",
        job_dir=str(tmp_path / "broken"),
        meta={
            "resumable": True,
            "hold_class": "runpod_transient",
            "repair_counts": {"runpod_transient": DEFAULT_MAX_REPAIRS},
        },
    )
    (tmp_path / "broken").mkdir(parents=True)

    nxt = store.create_job("Next resume", status="hold", stage="hold")
    store.update_job(
        nxt.id,
        error="farm process pid=1 exited without video_id",
        job_dir=str(tmp_path / "next"),
        meta={"resumable": True, "hold_class": "process_crash", "repair_counts": {}},
    )
    (tmp_path / "next").mkdir(parents=True)

    spawned: list[str] = []

    def fake_spawn(job_id, **_k):
        spawned.append(job_id)
        return {"ok": True, "spawned": True, "job_id": job_id}

    monkeypatch.setattr("src.agents.repair_watchdog.spawn_farm_job", fake_spawn)
    monkeypatch.setattr(
        "src.agents.ledger.OpsLedger.alert_now",
        lambda self, **kw: {"ok": True, "sent_email": False},
    )

    rw = RepairWatchdog(store=store)
    actions = rw.scan_resumable_failures(spawn=True)
    assert any(a.get("exhausted") or a.get("blocked") for a in actions)
    held = store.get_job(broken.id)
    assert held.status == "hold"
    assert (held.meta or {}).get("repair_exhausted") is True
    assert (held.meta or {}).get("resumable") is False
    # Next stamped HOLD should still be resumed same scan.
    assert nxt.id in spawned
    assert broken.id not in spawned


def test_try_resume_skips_exhausted_to_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from src.agents.store import OpsStore
    import src.runpod.watchdog as wd

    store = OpsStore(tmp_path / "ops")
    exhausted = store.create_job("Exhausted A", status="hold", stage="hold")
    store.update_job(
        exhausted.id,
        error="RunPod HTTP 503",
        meta={
            "resumable": True,
            "hold_class": "runpod_transient",
            "repair_counts": {"runpod_transient": 99},
        },
    )
    good = store.create_job("Good B", status="hold", stage="hold")
    store.update_job(
        good.id,
        error="process died",
        meta={"resumable": True, "hold_class": "process_crash"},
    )

    class FakeRW:
        def __init__(self, store=None, **_k):
            self.store = store or OpsStore(tmp_path / "ops")

        def repair_job(self, job_id, *, spawn=True):
            if job_id == exhausted.id:
                meta = dict((self.store.get_job(job_id).meta or {}))
                meta["repair_blocked"] = True
                meta["repair_exhausted"] = True
                meta["resumable"] = False
                self.store.update_job(job_id, status="hold", stage="hold", meta=meta)
                return {
                    "ok": False,
                    "blocked": True,
                    "exhausted": True,
                    "reason": "max repairs",
                    "status": "hold",
                    "job_id": job_id,
                }
            return {
                "ok": True,
                "spawned": True,
                "requeued": True,
                "class": "process_crash",
                "job_id": job_id,
            }

    monkeypatch.setattr("src.agents.store.OpsStore", lambda *a, **k: store)
    monkeypatch.setattr(
        "src.agents.farm.spawn_farm_job",
        lambda *_a, **_k: {"ok": False, "error": "unused"},
    )
    monkeypatch.setattr(
        "src.agents.farm.list_resumable_hold_jobs",
        lambda *_a, **_k: [
            j
            for j in store.list_jobs()
            if (j.meta or {}).get("resumable") is True
            and not (j.meta or {}).get("repair_exhausted")
        ],
    )
    monkeypatch.setattr("src.agents.repair_watchdog.RepairWatchdog", FakeRW)
    monkeypatch.setattr("src.agents.farm.pid_alive", lambda _p: False)

    started, job_id, results, msg = wd._try_resume_hold_or_failed()
    assert started is True
    assert job_id == good.id
    assert any((r.get("repair") or {}).get("exhausted") for r in results)
    assert "resumed HOLD" in msg


def test_start_one_falls_through_to_pick_when_holds_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import src.runpod.watchdog as wd

    monkeypatch.setattr(
        wd,
        "_try_start_farm_priority_job",
        lambda: (False, None, [], "no farm_priority jobs"),
    )
    monkeypatch.setattr(
        wd,
        "_try_resume_hold_or_failed",
        lambda: (False, "job_x", [{"exhausted": True}], "HOLD exhausted n=1"),
    )
    monkeypatch.setattr(
        "src.agents.farm.count_ready_for_stills",
        lambda: 0,
    )

    class FakeQ:
        def inventory_approved(self):
            return {"ready": [], "skipped_have_job_id": []}

    monkeypatch.setattr("src.agents.title_queue.TitleQueue", FakeQ)
    monkeypatch.setattr(
        "src.agents.worker.pick_and_enqueue",
        lambda **kwargs: [
            {"job_id": "job_new", "status": "farming", "title": "Next Approved"}
        ],
    )

    started, job_id, pick, msg = wd._start_one_farm_job()
    assert started is True
    assert job_id == "job_new"
    assert "started job_id=job_new" in msg


def test_heal_legacy_failed_to_hold_resumable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.agents.repair_watchdog import heal_legacy_failed_jobs
    from src.agents.store import OpsStore

    store = OpsStore(tmp_path / "ops")
    job_dir = tmp_path / "job_502"
    (job_dir / "images").mkdir(parents=True)
    failed = store.create_job("Comfy 502 legacy", status="failed", stage="failed")
    store.update_job(
        failed.id,
        job_dir=str(job_dir),
        error="scene 12 failed: Comfy /prompt HTTP 502: Waiting for service",
        meta={"channel": "napstorian", "farm_phase": "visuals"},
    )
    stuck = store.create_job("Stuck queued HOLD", status="hold", stage="queued")
    store.update_job(
        stuck.id,
        error="stuck in queued for 2673s > 1800s",
        meta={"channel": "napstorian"},
    )
    parked = store.create_job("Parked human", status="hold", stage="hold")
    store.update_job(
        parked.id,
        error="PARKED: sheet requeued with empty job_id",
        meta={"park_reason": "human", "skip_auto_farm": True},
    )

    monkeypatch.setattr(
        "src.agents.title_queue.TitleQueue.find_by_job_id",
        lambda self, *_a, **_k: None,
    )
    actions = heal_legacy_failed_jobs(store=store, sync_sheet=False)
    ids = {a["job_id"] for a in actions if a.get("healed")}
    assert failed.id in ids
    assert stuck.id in ids
    assert parked.id not in ids

    f2 = store.get_job(failed.id)
    assert f2.status == "hold"
    assert (f2.meta or {}).get("resumable") is True
    assert (f2.meta or {}).get("hold_class") == "runpod_transient"

    s2 = store.get_job(stuck.id)
    assert s2.status == "hold"
    assert (s2.meta or {}).get("resumable") is True
    assert (s2.meta or {}).get("hold_class") == "process_crash"

    p2 = store.get_job(parked.id)
    assert p2.status == "hold"
    assert (p2.meta or {}).get("resumable") is not True
