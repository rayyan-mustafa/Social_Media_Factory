"""Unit tests: availability learn + GREEN-only watchdog decisions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.runpod import capacity
from src.runpod.capacity import (
    CapacityDeferError,
    assert_farm_capacity_green,
    check_farm_capacity_green,
    farm_capacity_is_green,
)
from src.runpod.learn import (
    HourBucket,
    build_heatmap,
    load_benchmark_rows,
    recompute_availability_learn,
    suggest_windows,
)
from src.runpod.watchdog import (
    WatchdogState,
    compute_next_probe_at,
    decide_auto_start,
    expected_probes_per_day,
    probe_is_due,
    run_watchdog_tick,
)
from tests.test_runpod_capacity import FakeClient


def _row(ts: str, classification: str) -> dict:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return {
        "scheme": "runpod_stills_benchmark_v1",
        "ts_utc": ts,
        "classification": classification,
        "decision": "proceed" if classification == "GREEN" else "skip",
        "allow_create": classification == "GREEN",
        "month_utc": dt.month,
        "season": "JJA",
        "levels": [
            {
                "gpu_type_id": "NVIDIA RTX A5000",
                "cloud_type": "COMMUNITY",
                "stock_status": "Low" if classification == "GREEN" else "None",
                "available": classification == "GREEN",
            }
        ],
    }


def test_build_heatmap_and_exploratory_suggest(tmp_path: Path):
    jsonl = tmp_path / "bench.jsonl"
    lines = []
    for day in range(7, 14):
        for hour in (8, 9, 10):
            lines.append(
                json.dumps(
                    _row(
                        f"2026-08-{day:02d}T{hour:02d}:15:00+00:00",
                        "GREEN",
                    )
                )
            )
        lines.append(
            json.dumps(_row(f"2026-08-{day:02d}T20:00:00+00:00", "RED"))
        )
    jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rows = load_benchmark_rows(
        path=jsonl,
        lookback_days=30,
        now=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
    )
    assert len(rows) >= 20
    heat = build_heatmap(rows)
    assert heat["4:08"].green >= 1  # Fri Aug 7 = weekday 4
    # With default maturity (5 days / 8 samples), 7 days × 1 sample/hour is
    # almost mature on days but shy on samples — exploratory still works.
    sug = suggest_windows(heat, primary_hours=3, backup_hours=2, require_mature=False)
    assert sug["exploratory_primary"] is not None
    start = int(sug["exploratory_primary"]["window_utc"].split(":")[0])
    assert start in {7, 8, 9, 10}


def test_mature_before_promote_rejects_single_green_day(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("RUNPOD_LEARN_MATURE_MIN_DAYS", "5")
    monkeypatch.setenv("RUNPOD_LEARN_MATURE_MIN_SAMPLES", "8")
    monkeypatch.setenv("RUNPOD_LEARN_MATURE_MIN_GREEN_RATE", "0.35")
    # One lucky GREEN day must not promote
    heat = {
        "0:08": HourBucket(
            green=3, yellow=0, red=0, total=3, green_dates={"2026-08-01"}
        )
    }
    sug = suggest_windows(heat, primary_hours=1, backup_hours=1)
    assert sug["primary"] is None
    assert heat["0:08"].is_mature() is False


def test_mature_hour_can_promote(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RUNPOD_LEARN_MATURE_MIN_DAYS", "5")
    monkeypatch.setenv("RUNPOD_LEARN_MATURE_MIN_SAMPLES", "8")
    monkeypatch.setenv("RUNPOD_LEARN_MATURE_MIN_GREEN_RATE", "0.35")
    dates = {f"2026-08-{d:02d}" for d in range(1, 10)}
    heat = {
        "0:08": HourBucket(
            green=10, yellow=1, red=1, total=12, green_dates=dates
        ),
        "0:09": HourBucket(
            green=9, yellow=1, red=1, total=11, green_dates=dates
        ),
        "0:10": HourBucket(
            green=9, yellow=2, red=0, total=11, green_dates=dates
        ),
        "0:11": HourBucket(
            green=8, yellow=2, red=1, total=11, green_dates=dates
        ),
    }
    sug = suggest_windows(heat, primary_hours=4, backup_hours=1)
    assert sug["primary"] is not None
    assert sug["primary"]["mature"] is True
    assert sug["primary"]["start_hour_utc"] == 8


def test_learn_requires_min_samples_before_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    jsonl = tmp_path / "bench.jsonl"
    heat = tmp_path / "heat.json"
    learned = tmp_path / "learned.json"
    jsonl.write_text(
        json.dumps(_row("2026-08-07T08:00:00+00:00", "GREEN")) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RUNPOD_LEARN_MIN_SAMPLES", "50")
    monkeypatch.setenv("RUNPOD_LEARN_AUTO_APPLY", "1")
    payload = recompute_availability_learn(
        jsonl_path=jsonl,
        heatmap_path=heat,
        learned_path=learned,
        lookback_days=30,
        now=datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc),
        rotate=False,
    )
    assert payload["sample_count"] == 1
    assert payload["enough_samples"] is False
    assert payload["promotable"] is False
    assert payload["windows_applied_to_schedule"] is False
    assert heat.exists()
    assert not learned.exists()


def test_farm_capacity_green_only_blocks_yellow(monkeypatch: pytest.MonkeyPatch):
    """Mixed Community factory: YELLOW still blocked even with ALLOW_SECURE=1."""
    client = FakeClient(
        {
            ("NVIDIA RTX A5000", "COMMUNITY"): None,
            ("NVIDIA GeForce RTX 3090", "COMMUNITY"): "None",
            ("NVIDIA RTX A5000", "SECURE"): "High",
            ("NVIDIA A40", "SECURE"): "Low",
        }
    )
    monkeypatch.setenv("RUNPOD_ALLOW_SECURE", "1")
    monkeypatch.setattr(capacity, "stills_secure_only_mode", lambda: False)
    monkeypatch.setattr(
        capacity,
        "stills_probe_levels",
        lambda: [
            ("NVIDIA RTX A5000", "COMMUNITY"),
            ("NVIDIA GeForce RTX 3090", "COMMUNITY"),
            ("NVIDIA RTX A5000", "SECURE"),
            ("NVIDIA A40", "SECURE"),
        ],
    )
    with pytest.raises(CapacityDeferError, match="green-light only"):
        assert_farm_capacity_green(client=client, log_jsonl=False)

    ok, msg, result = check_farm_capacity_green(client=client, log_jsonl=False)
    assert ok is False
    assert result is not None and result.classification == "YELLOW"
    assert not farm_capacity_is_green(result)


def test_farm_capacity_a40_secure_only_allows_secure_stock(
    monkeypatch: pytest.MonkeyPatch,
):
    """A40 Secure-only factory: A40 Secure stock → farm GREEN."""
    client = FakeClient({("NVIDIA A40", "SECURE"): "Medium"})
    monkeypatch.setattr(capacity, "stills_secure_only_mode", lambda: True)
    monkeypatch.setattr(
        capacity, "stills_probe_levels", lambda: [("NVIDIA A40", "SECURE")]
    )
    result = assert_farm_capacity_green(client=client, log_jsonl=False)
    assert result is not None
    assert result.classification == "GREEN"
    assert farm_capacity_is_green(result)


def test_farm_capacity_green_allows_green(monkeypatch: pytest.MonkeyPatch):
    client = FakeClient(
        {
            ("NVIDIA RTX A5000", "COMMUNITY"): "Low",
            ("NVIDIA GeForce RTX 3090", "COMMUNITY"): "None",
            ("NVIDIA RTX A5000", "SECURE"): "None",
            ("NVIDIA A40", "SECURE"): "None",
        }
    )
    monkeypatch.setattr(capacity, "stills_secure_only_mode", lambda: False)
    monkeypatch.setattr(
        capacity,
        "stills_probe_levels",
        lambda: [
            ("NVIDIA RTX A5000", "COMMUNITY"),
            ("NVIDIA GeForce RTX 3090", "COMMUNITY"),
            ("NVIDIA RTX A5000", "SECURE"),
            ("NVIDIA A40", "SECURE"),
        ],
    )
    result = assert_farm_capacity_green(client=client, log_jsonl=False)
    assert result is not None
    assert result.classification == "GREEN"
    assert farm_capacity_is_green(result)


def test_decide_auto_start_green_only():
    base = dict(
        auto_start=True,
        dry_run=False,
        soft_cap=0,
        hard_cap=0,
        smoke_ok=True,
        smoke_msg="ok",
        concurrent_busy=False,
        concurrent_msg="ok",
        budget_ok=True,
        budget_msg="ok",
        pending_ok=True,
        pending_msg="pending_ready=3",
        cooldown_ok=True,
        cooldown_msg="ok",
        green_streak=1,
        min_green_streak=1,
        stock_ok=True,
        stock_msg="Community stock=LOW",
    )
    ok, reasons = decide_auto_start(
        classification="YELLOW", auto_starts_today=0, **base
    )
    assert ok is False
    assert any("green-light" in r for r in reasons)

    ok, _ = decide_auto_start(
        classification="RED", auto_starts_today=0, **base
    )
    assert ok is False

    ok, reasons = decide_auto_start(
        classification="GREEN", auto_starts_today=0, **base
    )
    assert ok is True
    assert any("sheet-driven" in r for r in reasons)

    # No pending ideas → no start (even when GREEN)
    ok, reasons = decide_auto_start(
        classification="GREEN",
        auto_starts_today=0,
        **{**base, "pending_ok": False, "pending_msg": "no pending sheet ideas"},
    )
    assert ok is False
    assert any("pending" in r for r in reasons)

    # After success + GREEN + pending → chain next (caps disabled)
    ok, reasons = decide_auto_start(
        classification="GREEN",
        auto_starts_today=5,
        opportunistic=True,
        has_success_today=True,
        **base,
    )
    assert ok is True
    assert any("chain" in r or "sheet-driven" in r for r in reasons)

    # YELLOW never starts
    ok, _ = decide_auto_start(
        classification="YELLOW",
        auto_starts_today=0,
        **base,
    )
    assert ok is False

    # Optional emergency ceiling still works when set
    ok, reasons = decide_auto_start(
        classification="GREEN",
        auto_starts_today=2,
        soft_cap=1,
        hard_cap=2,
        opportunistic=True,
        has_success_today=True,
        **{k: v for k, v in base.items() if k not in ("soft_cap", "hard_cap")},
    )
    assert ok is False
    assert any("ceiling" in r for r in reasons)


def test_notify_farm_success_schedules_wake(tmp_path: Path):
    from src.runpod.watchdog import notify_farm_success, WatchdogState

    state_path = tmp_path / "state.json"
    WatchdogState(
        auto_starts_utc_date="2026-08-07",
        auto_starts_today=1,
        next_probe_ts="2026-08-07T20:00:00+00:00",
    ).save(state_path)
    now = datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)
    st = notify_farm_success("job_abc", state_path=state_path, now=now)
    assert st.last_success_job_id == "job_abc"
    assert st.last_success_ts is not None
    # Wake earlier than the old 20:00 next probe
    assert st.next_probe_ts is not None
    assert st.next_probe_ts < "2026-08-07T20:00:00+00:00"


def test_probe_due_and_cadence_estimate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RUNPOD_PROBE_INTERVAL_IN_WINDOW_MIN", "10")
    monkeypatch.setenv("RUNPOD_PROBE_INTERVAL_OUT_WINDOW_MIN", "10")
    monkeypatch.setenv("RUNPOD_PROBE_OUT_WINDOW_JITTER_MIN", "0")
    now = datetime(2026, 8, 7, 8, 0, tzinfo=timezone.utc)
    state = WatchdogState(next_probe_ts="2026-08-07T09:00:00+00:00")
    due, _ = probe_is_due(state, now=now)
    assert due is False
    due, _ = probe_is_due(state, now=now, force=True)
    assert due is True

    import random

    nxt = compute_next_probe_at(
        now=now, in_window=True, rng=random.Random(0)
    )
    assert (nxt - now).total_seconds() == 10 * 60

    out = compute_next_probe_at(
        now=now, in_window=False, rng=random.Random(1)
    )
    delta_min = (out - now).total_seconds() / 60
    assert delta_min == 10

    est = expected_probes_per_day()
    assert est["daily_job_caps_disabled"] is True
    assert est["cron_recommended"] == "*/10"
    assert est["est_probes_per_day_total"] >= 100


def _mock_watchdog_gates(
    monkeypatch: pytest.MonkeyPatch, *, pending: int = 2, voice_ready: int = 0
):
    monkeypatch.setattr(
        "src.runpod.watchdog._smoke_gate_ok", lambda: (True, "ok")
    )
    monkeypatch.setattr(
        "src.runpod.watchdog._concurrent_busy", lambda: (False, "ok")
    )
    monkeypatch.setattr(
        "src.runpod.watchdog._budget_ok", lambda: (True, "ok")
    )
    monkeypatch.setattr(
        "src.runpod.watchdog._pending_ideas_ok",
        lambda: (
            pending > 0,
            f"pending_ready={pending}" if pending else "no pending sheet ideas",
            pending,
        ),
    )
    monkeypatch.setattr(
        "src.agents.farm.count_ready_for_stills",
        lambda *a, **k: voice_ready,
    )
    # Never touch live jobs.json / Sheets from unit tests.
    monkeypatch.setattr(
        "src.runpod.watchdog._maybe_repair_farm_jobs",
        lambda **kwargs: {"skipped": True, "reason": "test stub", "n": 0},
    )
    monkeypatch.setattr(
        "src.runpod.watchdog._maybe_repair_script_json",
        lambda **kwargs: {"skipped": True, "reason": "test stub"},
    )
    monkeypatch.setattr(
        "src.runpod.watchdog._maybe_prep_voice",
        lambda **kwargs: {"skipped": True, "reason": "test stub"},
    )
    monkeypatch.setattr(
        "src.runpod.watchdog._maybe_smm_scan",
        lambda **kwargs: {"skipped": True, "reason": "test stub"},
    )
    monkeypatch.setattr(
        "src.runpod.watchdog.recompute_availability_learn",
        lambda **kwargs: {
            "sample_count": 2,
            "enough_samples": False,
            "promotable": False,
            "suggested_primary_window_utc": "08:00-12:00",
            "suggested_backup_window_utc": "03:00-06:00",
            "windows_applied_to_schedule": False,
        },
    )
    monkeypatch.setattr(
        "src.agents.idea_stock.maybe_refill_idea_stock",
        lambda **kwargs: {"skipped": True, "reason": "test stub", "stock": 15},
    )


def test_watchdog_dry_run_no_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state_path = tmp_path / "state.json"
    monkeypatch.setenv("RUNPOD_WATCHDOG", "1")
    monkeypatch.setenv("RUNPOD_AUTO_START", "1")
    monkeypatch.setenv("RUNPOD_AUTO_MAX_JOBS_PER_DAY", "0")
    monkeypatch.setenv("RUNPOD_AUTO_MAX_JOBS_PER_DAY_HARD", "0")

    client = FakeClient(
        {
            ("NVIDIA RTX A5000", "COMMUNITY"): "Medium",
            ("NVIDIA GeForce RTX 3090", "COMMUNITY"): "Low",
            ("NVIDIA RTX A5000", "SECURE"): "Low",
            ("NVIDIA A40", "SECURE"): "Medium",
        }
    )

    started_calls: list = []

    def boom_start():
        started_calls.append(1)
        raise AssertionError("must not start farm in dry-run")

    monkeypatch.setattr(
        "src.runpod.watchdog._start_one_farm_job", boom_start
    )
    _mock_watchdog_gates(monkeypatch, pending=2)
    monkeypatch.setattr(
        capacity,
        "append_benchmark_jsonl",
        lambda *a, **k: tmp_path / "x.jsonl",
    )

    result = run_watchdog_tick(
        dry_run=True,
        force_probe=True,
        skip_harvest=True,
        client=client,
        state_path=state_path,
        now=datetime(2026, 8, 7, 8, 30, tzinfo=timezone.utc),
    )
    assert result.probed is True
    assert result.classification == "GREEN"
    assert result.started is False
    assert result.dry_run is True
    assert result.action == "dry_run"
    assert started_calls == []
    assert "would_start=True" in " ".join(result.reasons)


def test_watchdog_no_pending_ideas_no_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state_path = tmp_path / "state.json"
    monkeypatch.setenv("RUNPOD_WATCHDOG", "1")
    monkeypatch.setenv("RUNPOD_AUTO_START", "1")
    monkeypatch.setenv("RUNPOD_AUTO_MAX_JOBS_PER_DAY", "0")
    client = FakeClient(
        {
            ("NVIDIA RTX A5000", "COMMUNITY"): "High",
            ("NVIDIA GeForce RTX 3090", "COMMUNITY"): "Low",
            ("NVIDIA RTX A5000", "SECURE"): "None",
            ("NVIDIA A40", "SECURE"): "Medium",
        }
    )
    started_calls: list = []
    monkeypatch.setattr(
        "src.runpod.watchdog._start_one_farm_job",
        lambda: started_calls.append(1) or (True, "job_x", [], "started"),
    )
    _mock_watchdog_gates(monkeypatch, pending=0)
    monkeypatch.setattr(
        capacity,
        "append_benchmark_jsonl",
        lambda *a, **k: tmp_path / "x.jsonl",
    )
    result = run_watchdog_tick(
        dry_run=False,
        force_probe=True,
        skip_harvest=True,
        client=client,
        state_path=state_path,
        now=datetime(2026, 8, 7, 8, 30, tzinfo=timezone.utc),
    )
    assert result.classification == "GREEN"
    assert result.started is False
    assert result.pending_ready == 0
    assert started_calls == []
    assert any("pending" in r for r in result.reasons)


def test_watchdog_green_pending_would_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """After success path: GREEN + pending → live start (mocked)."""
    state_path = tmp_path / "state.json"
    WatchdogState(
        auto_starts_utc_date="2026-08-07",
        auto_starts_today=1,
        last_success_ts="2026-08-07T08:00:00+00:00",
        last_start_ts="2026-08-07T07:00:00+00:00",
        green_streak=1,
    ).save(state_path)
    monkeypatch.setenv("RUNPOD_WATCHDOG", "1")
    monkeypatch.setenv("RUNPOD_AUTO_START", "1")
    monkeypatch.setenv("RUNPOD_AUTO_MAX_JOBS_PER_DAY", "0")
    monkeypatch.setenv("RUNPOD_AUTO_JOB_COOLDOWN_MIN", "0")
    client = FakeClient(
        {
            ("NVIDIA RTX A5000", "COMMUNITY"): "Medium",
            ("NVIDIA GeForce RTX 3090", "COMMUNITY"): "Low",
            ("NVIDIA RTX A5000", "SECURE"): "None",
            ("NVIDIA A40", "SECURE"): "Medium",
        }
    )
    started_calls: list = []
    monkeypatch.setattr(
        "src.runpod.watchdog._start_one_farm_job",
        lambda: started_calls.append(1)
        or (True, "job_next", [{"job_id": "job_next"}], "started"),
    )
    _mock_watchdog_gates(monkeypatch, pending=4)
    monkeypatch.setattr(
        capacity,
        "append_benchmark_jsonl",
        lambda *a, **k: tmp_path / "x.jsonl",
    )
    result = run_watchdog_tick(
        dry_run=False,
        force_probe=True,
        skip_harvest=True,
        client=client,
        state_path=state_path,
        now=datetime(2026, 8, 7, 8, 30, tzinfo=timezone.utc),
    )
    assert result.classification == "GREEN"
    assert result.started is True
    assert result.job_id == "job_next"
    assert started_calls == [1]


def test_watchdog_blocks_yellow_auto_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state_path = tmp_path / "state.json"
    monkeypatch.setenv("RUNPOD_WATCHDOG", "1")
    monkeypatch.setenv("RUNPOD_AUTO_START", "1")
    # Mixed Community factory semantics — YELLOW must not auto-start.
    monkeypatch.setattr(capacity, "stills_secure_only_mode", lambda: False)
    monkeypatch.setattr(
        capacity,
        "stills_probe_levels",
        lambda: [
            ("NVIDIA RTX A5000", "COMMUNITY"),
            ("NVIDIA GeForce RTX 3090", "COMMUNITY"),
            ("NVIDIA RTX A5000", "SECURE"),
            ("NVIDIA A40", "SECURE"),
        ],
    )
    client = FakeClient(
        {
            ("NVIDIA RTX A5000", "COMMUNITY"): None,
            ("NVIDIA GeForce RTX 3090", "COMMUNITY"): None,
            ("NVIDIA RTX A5000", "SECURE"): "High",
            ("NVIDIA A40", "SECURE"): "High",
        }
    )
    started_calls: list = []
    monkeypatch.setattr(
        "src.runpod.watchdog._start_one_farm_job",
        lambda: started_calls.append(1) or (True, "job_x", [], "started"),
    )
    _mock_watchdog_gates(monkeypatch, pending=3)
    monkeypatch.setattr(
        capacity,
        "append_benchmark_jsonl",
        lambda *a, **k: tmp_path / "x.jsonl",
    )

    result = run_watchdog_tick(
        dry_run=False,
        force_probe=True,
        skip_harvest=True,
        client=client,
        state_path=state_path,
        now=datetime(2026, 8, 7, 8, 30, tzinfo=timezone.utc),
    )
    assert result.classification == "YELLOW"
    assert result.started is False
    assert result.action == "blocked"
    assert started_calls == []


def test_idea_stock_refill_skips_when_full(monkeypatch: pytest.MonkeyPatch):
    from src.agents import idea_stock as idea_mod

    class FakeQ:
        def count_idea_stock(self, *, channel=None):
            return 15

        def count_pending_ready(self, *, channel=None):
            return 4

        def list_rows(self, *, channel=None):
            return []

    monkeypatch.setattr(idea_mod, "publish_schedule_enabled", lambda: True)
    monkeypatch.setattr(idea_mod, "channel_in_publish_subset", lambda _ch: True)
    monkeypatch.setattr(
        idea_mod, "configured_sheet_channels", lambda: ["napstorian", "napping_historian"]
    )
    monkeypatch.setattr(idea_mod, "idea_stock_target", lambda: 15)
    out = idea_mod.maybe_refill_idea_stock(
        queue=FakeQ(), dry_run=False, skip_hygiene=True
    )
    assert out["skipped"] is True
    assert out["triggered"] is True
    assert out["channels"]["napstorian"]["skipped"] is True
    assert "full" in str(out["channels"]["napstorian"]["reason"])
    assert out["channels"]["napping_historian"]["skipped"] is True


def test_crontab_hint_is_ten_minutes():
    from src.runpod.watchdog import install_crontab_hint

    hint = install_crontab_hint()
    assert "*/10" in hint
    assert "*/5" not in hint
    assert "runpod_watchdog" in hint

def test_start_one_farm_job_prefers_resume_hold(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """GREEN start priority: resume HOLD before new approved sheet titles."""
    from src.agents.store import OpsStore
    from src.runpod import watchdog as wd

    store = OpsStore(tmp_path / "ops")
    job = store.create_job("Held Title", status="hold", stage="hold")
    store.update_job(
        job.id,
        error="RunPod job timed out",
        meta={
            "resumable": True,
            "hold_class": "runpod_transient",
            "resume": True,
            "last_good_stage": "visuals",
        },
    )

    monkeypatch.setattr(
        "src.agents.farm.list_resumable_hold_jobs",
        lambda _store=None, **_kw: [store.get_job(job.id)],
    )
    monkeypatch.setattr("src.agents.farm.count_ready_for_stills", lambda: 0)
    monkeypatch.setattr(
        wd,
        "_try_start_farm_priority_job",
        lambda: (False, None, [], "no farm_priority jobs"),
    )

    pick_called = {"n": 0}

    def boom_pick(**_k):
        pick_called["n"] += 1
        raise AssertionError("must not pick new title when HOLD resumable")

    monkeypatch.setattr("src.agents.worker.pick_and_enqueue", boom_pick)

    def fake_repair(self, job_id, *, spawn=True):
        return {
            "ok": True,
            "job_id": job_id,
            "class": "runpod_transient",
            "spawned": True,
            "requeued": True,
        }

    monkeypatch.setattr(
        "src.agents.repair_watchdog.RepairWatchdog.repair_job",
        fake_repair,
    )

    # No queued repair waiting — only HOLD resume path.
    monkeypatch.setattr(
        "src.agents.store.OpsStore.list_jobs",
        lambda self, status=None: [] if status == "queued" else [store.get_job(job.id)],
    )

    started, jid, results, msg = wd._start_one_farm_job()
    assert started is True
    assert jid == job.id
    assert "resumed" in msg.lower()
    assert pick_called["n"] == 0
    assert results and results[0].get("resume_hold") is True
