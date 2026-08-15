"""Tests for autonomous ops agents (file-backed, no external APIs required)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.agents.cost_guardian import CostGuardian
from src.agents.packaging_qa import check_packaging
from src.agents.policy_agent import PolicyAgent
from src.agents.retention_auditor import RetentionAuditor
from src.agents.smm_agent import SocialMediaManager
from src.agents.store import OpsStore
from src.agents.title_queue import TitleQueue
from src.agents.trends_agent import TrendsAgent
from src.agents.watchdog import WatchdogAgent
from src.domain.models import (
    Outline,
    OutlineChapter,
    Scene,
    ScriptResult,
    ScriptValidation,
)


def test_ops_store_job_and_event(tmp_path: Path):
    store = OpsStore(tmp_path / "ops")
    job = store.create_job("What If Rome Never Fell?", status="queued")
    assert job.id.startswith("job_")
    store.update_job(job.id, status="scripting", stage="scripting")
    got = store.get_job(job.id)
    assert got and got.status == "scripting"
    ev = store.add_event(
        agent="watchdog", event_type="stage", message="ok", job_id=job.id
    )
    assert ev.agent == "watchdog"
    assert store.month_spend_total() == 0.0


def test_title_queue_approve_and_pick(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("SHEET_CHANNELS", "napstorian,napping_historian")
    import src.agents.title_queue as tq
    from src.services import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    monkeypatch.setattr(
        tq, "local_queue_path", lambda ch: tmp_path / f"title_queue_{ch}.csv"
    )
    monkeypatch.setattr(tq, "legacy_local_queue_path", lambda: tmp_path / "legacy.csv")
    q = TitleQueue()
    q.append_titles(
        [
            {
                "title": "What If the Tudors Never Ruled England?",
                "policy_ok": True,
                "approved": False,
                "trend_score": 0.9,
                "competitor_source": "test",
            }
        ],
        channel="napstorian",
    )
    rows = q.list_rows(channel="napstorian")
    assert len(rows) == 1
    assert q.pick_approved() == []
    q.update_row(rows[0].row_index, approved=True, channel="napstorian")
    picked = q.pick_approved()
    assert len(picked) == 1
    assert "Tudors" in picked[0].title
    settings_mod.get_settings.cache_clear()


def test_policy_title_allowlist():
    agent = PolicyAgent()
    ok, errs = agent.title_policy_ok("What If Anne Boleyn Survived?")
    assert ok
    ok2, errs2 = agent.title_policy_ok("Crypto Trading Signals Daily")
    assert not ok2
    assert errs2


def test_policy_gate_b_with_history_script(tmp_path: Path, monkeypatch):
    store = OpsStore(tmp_path / "ops")
    agent = PolicyAgent(store=store)
    # Skip network refresh — inject snapshot
    from src.agents.store import PolicySnapshot
    from datetime import datetime, timezone

    agent.store.save_policy_snapshot(
        PolicySnapshot(
            version=1,
            content_hash="abc",
            fetched_at=datetime.now(timezone.utc).isoformat(),
            rule_pack={
                "max_age_hours": 24,
                "topic_allowlist": ["history", "what if", "tudor"],
                "rules": {},
            },
            compile_ok=True,
        )
    )
    script = ScriptResult(
        topic="What If Anne Boleyn Outlived Henry?",
        title="What If Anne Boleyn Outlived Henry?",
        hook="A sealed letter changes Tudor succession.",
        outline=Outline(
            title="t",
            chapters=[
                OutlineChapter(id=1, title="Hook", target_sentences=1, pacing_phase="a")
            ],
        ),
        scenes=[
            Scene(
                index=0,
                text="Anne reads the letter by candlelight.",
                visual_prompt="still",
                chapter_id=1,
            )
        ],
        validation=ScriptValidation(
            ok=True,
            scene_count=1,
            min_scenes=1,
            max_scenes=10,
            target_scenes=1,
            estimated_duration_s=3,
        ),
    )

    def fake_refresh(force=False):
        return agent.store.latest_policy_snapshot()

    monkeypatch.setattr(agent, "refresh", fake_refresh)
    gate = agent.run_gate_b(
        script=script,
        title=script.title,
        description="AI-generated documentary. Altered media disclosed.",
        ai_disclosure=True,
        refresh_if_stale=False,
    )
    assert gate.ok


def test_trends_harvest_dry_run():
    rows = TrendsAgent().harvest_titles(dry_run=True)
    assert isinstance(rows, list)
    # May be empty when no videos clear the viral bar / no API key
    for r in rows:
        src = str(r.get("competitor_source") or "")
        assert src not in ("llm_original", "original_bank", "original")
        assert "youtube.com/" in src.lower() or src.startswith("http")
        views = r.get("source_video_views")
        if views not in (None, ""):
            assert int(float(views)) >= 100000


def test_good_video_bar_and_highest_view_bind():
    from src.agents.trends_agent import (
        _passes_good_video_bar,
        _effective_video_views_bar,
        _pick_provenance_item,
        _video_filter_cfg,
    )

    cfg = {
        "power_filter": {
            "good_video_views": 100000,
            "good_video_vs_channel_avg": 1.0,
            "min_video_likes": 500,
        }
    }
    vf = _video_filter_cfg(cfg)
    assert vf["good_video_views"] == 100000
    assert vf["good_video_vs_channel_avg"] == 1.0

    weak = {
        "video_url": "https://www.youtube.com/watch?v=weak",
        "video_views": 50000,
        "video_likes": 2000,
        "source_avg_recent_views": 200000,
        "blocked_title": "What If Rome Never Fell?",
        "topics": ["Rome Never Fell"],
        "kind": "competitor_video",
    }
    # bar = max(100k, 200k*1.0) = 200k → 50k fails
    assert _effective_video_views_bar(weak, cfg) == 200000
    assert not _passes_good_video_bar(weak, cfg)

    strong_low = {
        "video_url": "https://www.youtube.com/watch?v=low",
        "video_views": 250000,
        "video_likes": 2000,
        "source_avg_recent_views": 200000,
        "blocked_title": "What If Rome Never Fell?",
        "topics": ["Rome Never Fell"],
        "kind": "competitor_video",
    }
    strong_high = {
        "video_url": "https://www.youtube.com/watch?v=high",
        "video_views": 900000,
        "video_likes": 9000,
        "source_avg_recent_views": 200000,
        "blocked_title": "What If Rome Never Fell Documentary?",
        "topics": ["Rome Never Fell"],
        "kind": "competitor_video",
    }
    assert _passes_good_video_bar(strong_low, cfg)
    assert _passes_good_video_bar(strong_high, cfg)

    url, item = _pick_provenance_item(
        "What If Rome Never Fell Forever?",
        [strong_low, strong_high],
        prefer_video=True,
    )
    assert "high" in url
    assert item is strong_high

    # Absolute floor when avg unknown
    no_avg = {
        "video_url": "https://www.youtube.com/watch?v=abs",
        "video_views": 150000,
        "video_likes": 600,
    }
    assert _effective_video_views_bar(no_avg, cfg) == 100000
    assert _passes_good_video_bar(no_avg, cfg)
    low_likes = {**no_avg, "video_likes": 10}
    assert not _passes_good_video_bar(low_likes, cfg)


def test_backfill_competitor_source(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("SHEET_CHANNELS", "napstorian")
    import src.agents.title_queue as tq
    from src.services import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    monkeypatch.setattr(
        tq, "local_queue_path", lambda ch: tmp_path / f"title_queue_{ch}.csv"
    )
    monkeypatch.setattr(tq, "legacy_local_queue_path", lambda: tmp_path / "legacy.csv")
    q = TitleQueue()
    q.append_titles(
        [
            {
                "title": "What If Anne Boleyn Outlived Henry?",
                "competitor_source": "llm_original",
                "policy_ok": True,
                "trend_score": 0.7,
            },
            {
                "title": "What If the Soviet Union Collapsed in 1953?",
                "competitor_source": "original_bank",
                "policy_ok": True,
                "trend_score": 0.6,
            },
        ],
        channel="napstorian",
    )
    store = OpsStore(tmp_path / "ops")
    summary = TrendsAgent(store=store, queue=q).backfill_competitor_sources(
        dry_run=False, use_api=False
    )
    assert summary["updated"] == 2
    rows = q.list_rows()
    assert all("youtube.com/" in r.competitor_source for r in rows)
    assert all(r.competitor_source != "llm_original" for r in rows)
    assert all("%40" not in r.competitor_source for r in rows)
    # Rotation: two rows should not both collapse to the identical URL when
    # many competitors are available
    assert len({r.competitor_source for r in rows}) >= 2
    settings_mod.get_settings.cache_clear()


def test_competitors_query_helpers_and_due():
    from src.agents.competitors_agent import (
        CompetitorsAgent,
        _queries_from_title,
        _norm_name,
    )

    qs = _queries_from_title("What If Anne Boleyn Had Survived the Tower?")
    assert any("what if" in q for q in qs)
    assert _norm_name("@HistoryWhatIf") == _norm_name("historywhatif")

    # Interval gate: recent last_refresh + enough IDs → not due
    agent = CompetitorsAgent.__new__(CompetitorsAgent)
    agent.cfg = {
        "competitors": [
            {"channel_id": "UCaaaaaaaaaaaaaaaaaaaaaa", "label": "a"},
            {"channel_id": "UCbbbbbbbbbbbbbbbbbbbbbb", "label": "b"},
            {"channel_id": "UCcccccccccccccccccccccc", "label": "c"},
        ],
        "refresh": {
            "last_refresh_at": "2099-01-01T00:00:00+00:00",
            "min_interval_hours": 168,
            "thin_below": 2,
        },
    }
    due, reason = CompetitorsAgent._is_due(
        agent,
        agent.cfg["refresh"],
        {"min_interval_hours": 168, "thin_competitors_below": 2},
        force=False,
    )
    assert not due
    assert "not_due" in reason

    due2, reason2 = CompetitorsAgent._is_due(
        agent, agent.cfg["refresh"], {}, force=True
    )
    assert due2 and reason2 == "force"


def test_watchdog_stuck_and_smm(tmp_path: Path):
    store = OpsStore(tmp_path / "ops")
    wd = WatchdogAgent(store=store)
    job = store.create_job("What If History?", status="visuals", stage="visuals")
    # Force old updated_at
    data = json.loads((store.root / "jobs.json").read_text())
    data[0]["updated_at"] = "2020-01-01T00:00:00+00:00"
    (store.root / "jobs.json").write_text(json.dumps(data), encoding="utf-8")
    actions = wd.scan_stuck_jobs()
    assert any(a["job_id"] == job.id for a in actions)

    smm = SocialMediaManager(store=store)
    insight = smm.evaluate_video(
        video_id="x",
        title="What If History?",
        metrics={"avd_pct": 20, "first_60s_retention_pct": 40, "ctr_pct": 2},
    )
    assert insight.proposals
    drafts = smm.draft_comment_replies([{"id": "1", "text": "Great video"}])
    assert drafts and "draft" in drafts[0]["draft_reply"].lower()


def test_watchdog_skips_private_waiting_on_human(tmp_path: Path):
    store = OpsStore(tmp_path / "ops")
    wd = WatchdogAgent(store=store)
    job = store.create_job("What If History?", status="private", stage="private")
    data = json.loads((store.root / "jobs.json").read_text())
    data[0]["updated_at"] = "2020-01-01T00:00:00+00:00"
    (store.root / "jobs.json").write_text(json.dumps(data), encoding="utf-8")
    actions = wd.scan_stuck_jobs()
    assert not any(a["job_id"] == job.id for a in actions)
    assert store.get_job(job.id).status == "private"


def test_farm_spawn_sets_pid_and_flags(tmp_path: Path, monkeypatch):
    from src.agents import farm as farm_mod

    store = OpsStore(tmp_path / "ops")
    job = store.create_job("What If Tudors?", status="queued", stage="queued")

    class FakeProc:
        pid = 424242

    def fake_popen(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(farm_mod, "ROOT", tmp_path)
    monkeypatch.setattr(farm_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(farm_mod, "farm_flags_from_config", lambda: {
        "mock_images": False,
        "publish": True,
        "publish_dry_run": False,
        "test_mode": False,
    })

    out = farm_mod.spawn_farm_job(job.id, store=store)
    assert out["ok"] and out["spawned"]
    assert out["farm_pid"] == 424242
    updated = store.get_job(job.id)
    assert updated.status == "farming"
    assert updated.meta.get("farm_pid") == 424242
    assert "run_farm_job" in " ".join(updated.meta.get("farm_cmd") or [])
    # Idempotent: live pid skips second spawn
    monkeypatch.setattr(farm_mod, "pid_alive", lambda pid: True)
    again = farm_mod.spawn_farm_job(job.id, store=store)
    assert again.get("already_running")


def test_sleep_beat_defaults_enable_farm(monkeypatch, tmp_path: Path):
    from src.agents import sleep_factory as sf

    captured: dict = {}

    def fake_pick(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(sf, "pick_and_enqueue", fake_pick)
    monkeypatch.setattr(sf, "reconcile_farms", lambda **kw: {"dead": [], "spawned": []})
    monkeypatch.setattr(
        sf,
        "_agents_cfg",
        lambda: {
            "max_jobs_per_pick": 1,
            "sleep_beat": {
                "enqueue_pipeline": True,
                "run_pipeline": True,
                "harvest_if_queue_low": False,
                "min_queued_titles": 5,
                "max_picks_per_beat": 1,
                "competitors_refresh_if_due": False,
            },
        },
    )

    class FakePolicy:
        def __init__(self, *a, **k):
            pass

        def refresh(self):
            from types import SimpleNamespace

            return SimpleNamespace(version=1, compile_ok=True)

    class FakeWD:
        def __init__(self, *a, **k):
            pass

        def scan_stuck_jobs(self):
            return []

    class FakeSched:
        def __init__(self, *a, **k):
            pass

        def status(self):
            return {"buffer_target": 8}

        def assign_slot_for_job(self, **k):
            return {}

    class FakeCost:
        def __init__(self, *a, **k):
            pass

        def check_can_start_job(self):
            return True, "ok"

    monkeypatch.setattr(sf, "PolicyAgent", FakePolicy)
    monkeypatch.setattr(sf, "WatchdogAgent", FakeWD)
    monkeypatch.setattr(sf, "ScheduleAgent", FakeSched)
    monkeypatch.setattr(sf, "CostGuardian", FakeCost)
    monkeypatch.setattr(sf, "OpsStore", lambda: OpsStore(tmp_path / "ops"))
    monkeypatch.setattr(sf, "OpsLedger", lambda store: type("L", (), {"write": lambda *a, **k: {}})())
    monkeypatch.setattr(sf, "TitleQueue", lambda: type("Q", (), {
        "list_rows": lambda self: [],
        "find_by_job_id": lambda self, j: None,
    })())
    monkeypatch.setattr(sf, "ROOT", tmp_path)
    monkeypatch.setattr(sf, "max_concurrent_jobs", lambda: 1)
    # Bypass stills smoke gate so this unit test can assert farm config knobs.
    import src.runpod.guards as g

    monkeypatch.setattr(
        g, "farm_may_use_runpod_pod_stills", lambda **k: (True, "test bypass")
    )

    out = sf.run_beat()
    assert out["config"]["do_farm"] is True
    assert captured.get("enqueue_pipeline") is True

    out2 = sf.run_beat(run_pipeline=False)
    assert out2["config"]["do_farm"] is False


def test_sleep_beat_blocks_farm_without_smoke_ok(monkeypatch, tmp_path: Path):
    from src.agents import sleep_factory as sf

    captured: dict = {}

    def fake_pick(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(sf, "pick_and_enqueue", fake_pick)
    monkeypatch.setattr(sf, "reconcile_farms", lambda **kw: {"dead": [], "spawned": []})
    monkeypatch.setattr(
        sf,
        "_agents_cfg",
        lambda: {
            "max_jobs_per_pick": 1,
            "sleep_beat": {
                "enqueue_pipeline": True,
                "run_pipeline": True,
                "harvest_if_queue_low": False,
                "min_queued_titles": 5,
                "max_picks_per_beat": 1,
                "competitors_refresh_if_due": False,
            },
        },
    )

    class FakePolicy:
        def __init__(self, *a, **k):
            pass

        def refresh(self):
            from types import SimpleNamespace

            return SimpleNamespace(version=1, compile_ok=True)

    class FakeWD:
        def __init__(self, *a, **k):
            pass

        def scan_stuck_jobs(self):
            return []

    class FakeSched:
        def __init__(self, *a, **k):
            pass

        def status(self):
            return {"buffer_target": 8}

        def assign_slot_for_job(self, **k):
            return {}

    class FakeCost:
        def __init__(self, *a, **k):
            pass

        def check_can_start_job(self):
            return True, "ok"

    monkeypatch.setattr(sf, "PolicyAgent", FakePolicy)
    monkeypatch.setattr(sf, "WatchdogAgent", FakeWD)
    monkeypatch.setattr(sf, "ScheduleAgent", FakeSched)
    monkeypatch.setattr(sf, "CostGuardian", FakeCost)
    monkeypatch.setattr(sf, "OpsStore", lambda: OpsStore(tmp_path / "ops"))
    monkeypatch.setattr(sf, "OpsLedger", lambda store: type("L", (), {"write": lambda *a, **k: {}})())
    monkeypatch.setattr(sf, "TitleQueue", lambda: type("Q", (), {
        "list_rows": lambda self: [],
        "find_by_job_id": lambda self, j: None,
    })())
    monkeypatch.setattr(sf, "ROOT", tmp_path)
    monkeypatch.setattr(sf, "max_concurrent_jobs", lambda: 1)

    import src.runpod.guards as g

    monkeypatch.setattr(
        g, "farm_may_use_runpod_pod_stills", lambda **k: (False, "smoke gate closed")
    )

    out = sf.run_beat()
    assert out["config"]["do_farm"] is False
    assert out["config"]["stills_smoke_gate_ok"] is False
    assert captured.get("enqueue_pipeline") is False


def test_cost_guardian_and_retention(tmp_path: Path):
    store = OpsStore(tmp_path / "ops")
    g = CostGuardian(store=store)
    ok, _ = g.check_can_start_job(estimated_usd=0)
    assert ok
    store.record_spend(category="runpod", amount_usd=100)
    # Override cfg monthly via monkeypatch on instance
    g.cfg = {"monthly_budget_usd": 50}
    ok2, msg = g.check_can_start_job(estimated_usd=1)
    assert not ok2

    script = ScriptResult(
        topic="What If History?",
        title="What If History?",
        outline=Outline(
            title="t",
            chapters=[
                OutlineChapter(id=1, title="H", target_sentences=1, pacing_phase="a")
            ],
        ),
        scenes=[
            Scene(
                index=0,
                text="The final visual should be a slow zoom.",
                visual_prompt="x",
                chapter_id=1,
            )
        ],
        validation=ScriptValidation(
            ok=True,
            scene_count=1,
            min_scenes=1,
            max_scenes=5,
            target_scenes=1,
            estimated_duration_s=3,
        ),
    )
    audit = RetentionAuditor(store=store).audit_script(script)
    assert not audit["ok"]

    qa = check_packaging(title="What If History Changed?", script=script)
    assert "ok" in qa


def test_repair_watchdog_classify_and_infinite_visual():
    from src.agents.repair_watchdog import (
        INFINITE_REPAIR_CLASSES,
        classify_failure,
    )

    assert (
        classify_failure(
            "scene 31 failed: failed after 3 attempts: validation failed after generate: x/scene_031.jpg"
        )
        == "visual_validation"
    )
    assert "visual_validation" in INFINITE_REPAIR_CLASSES
    assert classify_failure("RunPod HTTP 403 Forbidden — RUNPOD_API_KEY") == "auth"
    assert (
        classify_failure("farm process pid=1 exited without video_id", "validation failed after generate")
        == "visual_validation"
    )
    assert classify_failure("VoiceModule failed:\nkokoro boom") == "tts"
    assert (
        classify_failure(
            "PublishModule failed:\nGate A FAILED — no upload:\n- duration 782.7s outside Gate A band"
        )
        == "publish"
    )
    assert (
        classify_failure("Expecting value: line 4 column 31 (char 278)")
        == "script_json"
    )
    assert (
        classify_failure(
            "JSON parse failed after 3 attempt(s)",
            "File .../script.py ... _generate_outline\nchat_json",
        )
        == "script_json"
    )


def test_repair_watchdog_script_json_empty_progress(tmp_path: Path, monkeypatch):
    from src.agents.repair_watchdog import RepairWatchdog, job_has_meaningful_progress
    from src.agents.store import OpsStore

    store = OpsStore(tmp_path / "ops")
    job_dir = tmp_path / "job_empty"
    for sub in ("script", "audio", "images", "video"):
        (job_dir / sub).mkdir(parents=True)
    job = store.create_job("What If July 20th?", status="failed", stage="failed")
    store.update_job(
        job.id,
        error="Expecting value: line 4 column 31 (char 278)",
        job_dir=str(job_dir),
        meta={"farm_phase": "prep", "channel": "napstorian"},
    )
    assert job_has_meaningful_progress(store.get_job(job.id)) is False

    spawned = {"n": 0}

    def fake_spawn(job_id, **_k):
        spawned["n"] += 1
        return {"ok": True, "spawned": True, "job_id": job_id}

    monkeypatch.setattr("src.agents.repair_watchdog.spawn_farm_job", fake_spawn)
    rw = RepairWatchdog(store=store)
    # Full scan disabled in cfg by default in repo; call targeted scan.
    actions = rw.scan_script_json_failures(spawn=True)
    assert any(a.get("job_id") == job.id and a.get("spawned") for a in actions)
    assert spawned["n"] == 1
    refreshed = store.get_job(job.id)
    assert refreshed.status == "queued"


def test_repair_watchdog_script_json_skips_with_assets(tmp_path: Path, monkeypatch):
    from src.agents.repair_watchdog import RepairWatchdog
    from src.agents.store import OpsStore

    store = OpsStore(tmp_path / "ops")
    job_dir = tmp_path / "job_assets"
    (job_dir / "script").mkdir(parents=True)
    (job_dir / "script" / "script.json").write_text('{"scenes":[]}', encoding="utf-8")
    job = store.create_job("Has Assets", status="failed", stage="failed")
    store.update_job(
        job.id,
        error="Expecting value: line 1",
        job_dir=str(job_dir),
        meta={"farm_phase": "prep"},
    )

    monkeypatch.setattr(
        "src.agents.repair_watchdog.spawn_farm_job",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    actions = RepairWatchdog(store=store).scan_script_json_failures(spawn=True)
    assert any(a.get("skipped") and a.get("job_id") == job.id for a in actions)


def test_repair_watchdog_script_json_stranded_queued(tmp_path: Path, monkeypatch):
    """Queued + last_repair_class=script_json + dead pid must re-spawn as prep."""
    from src.agents.repair_watchdog import RepairWatchdog
    from src.agents.store import OpsStore

    store = OpsStore(tmp_path / "ops")
    job_dir = tmp_path / "job_stranded"
    for sub in ("script", "audio", "images", "video"):
        (job_dir / sub).mkdir(parents=True)
    job = store.create_job("Ottoman stranded", status="queued", stage="queued")
    store.update_job(
        job.id,
        job_dir=str(job_dir),
        meta={
            "farm_phase": "full",
            "farm_pid": 99999999,  # dead
            "last_repair_class": "script_json",
            "repair_counts": {"script_json": 1},
        },
    )

    seen: dict[str, Any] = {}

    def fake_spawn(job_id, **kw):
        seen["job_id"] = job_id
        seen["phase"] = kw.get("phase")
        return {"ok": True, "spawned": True, "job_id": job_id}

    monkeypatch.setattr("src.agents.repair_watchdog.spawn_farm_job", fake_spawn)
    monkeypatch.setattr("src.agents.repair_watchdog.pid_alive", lambda _p: False)
    actions = RepairWatchdog(store=store).scan_script_json_failures(spawn=True)
    assert any(a.get("job_id") == job.id and a.get("spawned") for a in actions)
    assert seen.get("phase") == "prep"


def test_repair_watchdog_holds_after_max_script_json_repairs(tmp_path: Path, monkeypatch):
    from src.agents.repair_watchdog import RepairWatchdog, SCRIPT_JSON_MAX_AUTO_REPAIRS
    from src.agents.store import OpsStore

    store = OpsStore(tmp_path / "ops")
    job_dir = tmp_path / "job_max"
    for sub in ("script", "audio", "images", "video"):
        (job_dir / sub).mkdir(parents=True)
    job = store.create_job("Maxed JSON", status="failed", stage="failed")
    store.update_job(
        job.id,
        error="JSON parse failed after 3 attempt(s): Could not parse JSON",
        job_dir=str(job_dir),
        meta={
            "farm_phase": "prep",
            "last_repair_class": "script_json",
            "repair_counts": {"script_json": SCRIPT_JSON_MAX_AUTO_REPAIRS},
            "script_json_policy": "v2_failover",
        },
    )
    monkeypatch.setattr(
        "src.agents.repair_watchdog.spawn_farm_job",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    actions = RepairWatchdog(store=store).scan_script_json_failures(spawn=True)
    assert any(a.get("blocked") for a in actions)
    held = store.get_job(job.id)
    assert held.status == "hold"
    assert (held.meta or {}).get("repair_exhausted") is True
    assert (held.meta or {}).get("resumable") is False


def test_repair_watchdog_blocks_publish_no_spawn(tmp_path: Path, monkeypatch):
    from src.agents.repair_watchdog import RepairWatchdog
    from src.agents.store import OpsStore

    store = OpsStore(tmp_path / "ops")
    job = store.create_job("What If Gate A?", status="failed", stage="failed")
    store.update_job(
        job.id,
        error="PublishModule failed:\nGate A FAILED — duration too short",
        job_dir=str(tmp_path / "job"),
        meta={},
    )
    (tmp_path / "job" / "video").mkdir(parents=True)
    (tmp_path / "job" / "video" / "final.mp4").write_bytes(b"x" * 2000)

    spawned = {"n": 0}

    def fake_spawn(*_a, **_k):
        spawned["n"] += 1
        return {"spawned": True}

    monkeypatch.setattr("src.agents.repair_watchdog.spawn_farm_job", fake_spawn)
    # no SMTP spam in test
    monkeypatch.setattr(
        "src.agents.ledger.OpsLedger.alert_now",
        lambda self, **kw: {"ok": True, "sent_email": False},
    )

    rw = RepairWatchdog(store=store)
    out = rw.repair_job(job.id, spawn=True)
    assert out.get("blocked") is True
    assert out.get("status") == "hold"
    assert spawned["n"] == 0
    held = store.get_job(job.id)
    assert held.status == "hold"
    assert (held.meta or {}).get("repair_blocked") is True


def test_on_stage_error_defaults_to_hold_not_failed(tmp_path: Path):
    """Bugfix: error must not overwrite explicit HOLD to failed."""
    from src.agents.store import OpsStore
    from src.agents.watchdog import WatchdogAgent

    store = OpsStore(tmp_path / "ops")
    job = store.create_job("Hold Me", status="farming", stage="visuals")
    wd = WatchdogAgent(store)
    wd.on_stage(job.id, "hold", status="hold", error="RunPod timed out")
    refreshed = store.get_job(job.id)
    assert refreshed.status == "hold"
    assert refreshed.error == "RunPod timed out"


def test_hold_farm_job_resumable_not_failed(tmp_path: Path, monkeypatch):
    from src.agents.farm import hold_farm_job, list_resumable_hold_jobs
    from src.agents.store import OpsStore

    store = OpsStore(tmp_path / "ops")
    job_dir = tmp_path / "job"
    (job_dir / "script").mkdir(parents=True)
    (job_dir / "script" / "script.json").write_text('{"scenes":[]}', encoding="utf-8")
    (job_dir / "audio").mkdir(parents=True)
    (job_dir / "audio" / "voice_manifest.json").write_text("{}", encoding="utf-8")
    job = store.create_job("Resume Me", status="farming", stage="visuals", job_dir=str(job_dir))
    monkeypatch.setattr("src.agents.farm.TitleQueue.find_by_job_id", lambda self, *_a, **_k: None)

    out = hold_farm_job(
        job.id,
        error="RunPod job timed out during stills",
        store=store,
        hold_class="runpod_transient",
        reason="stills_timeout",
        sync_sheet=False,
    )
    assert out["status"] == "hold"
    assert out["resumable"] is True
    refreshed = store.get_job(job.id)
    assert refreshed.status == "hold"
    assert refreshed.status != "failed"
    assert (refreshed.meta or {}).get("resumable") is True
    assert (refreshed.meta or {}).get("last_good_stage") == "visuals"
    assert any(j.id == job.id for j in list_resumable_hold_jobs(store))


def test_reconcile_dead_pid_holds_not_fails(tmp_path: Path, monkeypatch):
    from src.agents import farm as farm_mod
    from src.agents.store import OpsStore

    store = OpsStore(tmp_path / "ops")
    job = store.create_job(
        "Dead PID",
        status="farming",
        stage="edit",
        meta={"farm_pid": 99999999},
        job_dir=str(tmp_path / "job"),
    )
    monkeypatch.setattr(farm_mod, "pid_alive", lambda _p: False)
    monkeypatch.setattr(farm_mod.TitleQueue, "find_by_job_id", lambda self, *_a, **_k: None)

    out = farm_mod.reconcile_farms(store=store, spawn_queued=False)
    assert len(out["dead"]) == 1
    assert out["dead"][0]["status"] == "hold"
    refreshed = store.get_job(job.id)
    assert refreshed.status == "hold"
    assert (refreshed.meta or {}).get("resumable") is True


def test_repair_watchdog_resumes_hold_stills(tmp_path: Path, monkeypatch):
    from src.agents.repair_watchdog import RepairWatchdog
    from src.agents.store import OpsStore

    store = OpsStore(tmp_path / "ops")
    job_dir = tmp_path / "job"
    (job_dir / "script").mkdir(parents=True)
    (job_dir / "script" / "script.json").write_text('{"scenes":[]}', encoding="utf-8")
    (job_dir / "audio").mkdir(parents=True)
    (job_dir / "audio" / "voice_manifest.json").write_text("{}", encoding="utf-8")
    job = store.create_job("HOLD Stills", status="hold", stage="hold")
    store.update_job(
        job.id,
        error="RunPod job timed out",
        job_dir=str(job_dir),
        meta={
            "resumable": True,
            "hold_class": "runpod_transient",
            "last_good_stage": "visuals",
            "resume": True,
        },
    )
    seen: dict[str, Any] = {}

    def fake_spawn(job_id, **kw):
        seen["job_id"] = job_id
        seen["phase"] = kw.get("phase")
        return {"ok": True, "spawned": True, "job_id": job_id, "farm_phase": kw.get("phase")}

    monkeypatch.setattr("src.agents.repair_watchdog.spawn_farm_job", fake_spawn)
    monkeypatch.setattr(
        "src.agents.ledger.OpsLedger.alert_now",
        lambda self, **kw: {"ok": True, "sent_email": False},
    )
    actions = RepairWatchdog(store=store).scan_resumable_failures(spawn=True)
    assert any(a.get("job_id") == job.id and a.get("spawned") for a in actions)
    assert seen.get("phase") == "visuals"
    assert store.get_job(job.id).status == "queued"


def test_image_ready_no_luma_gate(tmp_path: Path):
    from PIL import Image

    from src.services.visuals_ai import VisualModule

    vm = VisualModule.__new__(VisualModule)

    class _S:
        image_min_bytes = 100

    vm.s = _S()  # type: ignore[attr-defined]
    dark = tmp_path / "dark.jpg"
    Image.new("RGB", (64, 64), (5, 5, 5)).save(dark, quality=90)
    assert dark.stat().st_size >= 100
    assert vm._image_ready(dark) is True
    assert vm._is_valid_image(dark) is True
    missing = tmp_path / "nope.jpg"
    assert vm._image_ready(missing) is False
