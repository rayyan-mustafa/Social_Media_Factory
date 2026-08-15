"""Tests for Phase A/B split: stop-after-voice + ready_for_stills + GREEN visuals."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.agents.farm import (
    IDLE_STATUSES,
    READY_FOR_STILLS_STATUSES,
    content_kernel_ready,
    count_encoding_jobs,
    count_ready_for_stills,
    list_ready_for_derivatives,
    list_ready_for_stills,
    mark_ready_for_derivatives,
    max_gpu_concurrent,
    normalize_farm_phase,
    voice_artifacts_ready,
)
from src.agents.store import OpsStore
from src.domain.models import Outline, OutlineChapter, Scene, ScriptResult, ScriptValidation
from src.runpod.watchdog import decide_auto_start, prep_voice_enabled, prep_voice_max
from src.services.pipeline import Pipeline, PipelineError, PipelineResult


def _minimal_script(topic: str = "T") -> ScriptResult:
    return ScriptResult(
        topic=topic,
        title=topic,
        hook="hook",
        outline=Outline(
            title=topic,
            chapters=[OutlineChapter(id=1, title="c1", target_sentences=1)],
        ),
        scenes=[
            Scene(index=0, text="hello world", visual_prompt="a castle"),
        ],
        validation=ScriptValidation(
            ok=True,
            scene_count=1,
            min_scenes=1,
            max_scenes=12,
            target_scenes=5,
            estimated_duration_s=10.0,
        ),
    )


def test_normalize_farm_phase():
    assert normalize_farm_phase("prep") == "prep"
    assert normalize_farm_phase("stop_after_voice") == "prep"
    assert normalize_farm_phase("from_visuals") == "visuals"
    assert normalize_farm_phase("visuals") == "visuals"
    assert normalize_farm_phase(None) == "full"
    assert normalize_farm_phase("full") == "full"


def test_ready_for_stills_is_idle_not_encoding(tmp_path: Path):
    store = OpsStore(tmp_path / "ops")
    job = store.create_job("What If X?", status="ready_for_stills", stage="awaiting_gpu")
    assert job.status in READY_FOR_STILLS_STATUSES
    assert job.status in IDLE_STATUSES
    assert count_encoding_jobs(store) == 0
    assert count_ready_for_stills(store) == 1
    assert list_ready_for_stills(store)[0].id == job.id


def test_voice_artifacts_ready(tmp_path: Path):
    assert voice_artifacts_ready(None) is False
    assert voice_artifacts_ready(tmp_path) is False
    (tmp_path / "script").mkdir()
    (tmp_path / "audio").mkdir()
    (tmp_path / "script" / "script.json").write_text("{}", encoding="utf-8")
    assert voice_artifacts_ready(tmp_path) is False
    (tmp_path / "audio" / "voice_manifest.json").write_text("{}", encoding="utf-8")
    assert voice_artifacts_ready(tmp_path) is True


def test_pipeline_stop_after_voice_skips_visuals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    job_dir = tmp_path / "job"
    script_dir = job_dir / "script"
    audio_dir = job_dir / "audio"
    script_dir.mkdir(parents=True)
    audio_dir.mkdir(parents=True)
    script = _minimal_script("What If Prep?")
    script_path = script_dir / "script.json"
    script_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")

    def fake_voice(self, sp, ad, **kw):
        man = Path(ad) / "voice_manifest.json"
        man.write_text(json.dumps({"scenes": []}), encoding="utf-8")
        return man

    monkeypatch.setattr(Pipeline, "_run_voice", fake_voice)

    class FakeBed:
        def mix_voice_manifest(self, voice_manifest, script_path=None, **kw):
            mixed = Path(voice_manifest).parent / "mixed" / "voice_manifest_mixed.json"
            mixed.parent.mkdir(parents=True, exist_ok=True)
            mixed.write_text(json.dumps({"scenes": []}), encoding="utf-8")
            return MagicMock(mixed_manifest=mixed, meta={"ok": True})

    monkeypatch.setattr("src.services.audio_bed.AudioBedModule", FakeBed)

    class BoomVisuals:
        def synthesize_script(self, *a, **k):
            raise AssertionError("visuals must not run on stop_after_voice")

    # Import path used inside Pipeline.run
    import src.services.visuals_ai as visuals_mod

    monkeypatch.setattr(visuals_mod, "VisualModule", BoomVisuals)

    result = Pipeline().run(
        "What If Prep?",
        job_dir=job_dir,
        stop_after_voice=True,
        publish=False,
        resume=True,
        mock_images=True,
    )
    assert isinstance(result, PipelineResult)
    assert result.stopped_after == "voice"
    assert result.visual_manifest is None
    assert result.final_path is None
    assert (job_dir / "pipeline_manifest.json").exists()
    payload = json.loads((job_dir / "pipeline_manifest.json").read_text(encoding="utf-8"))
    assert payload["stopped_after"] == "voice"


def test_pipeline_mutual_exclusion():
    with pytest.raises(PipelineError, match="mutually exclusive"):
        Pipeline().run("T", stop_after_voice=True, from_visuals=True)


def test_pipeline_from_visuals_requires_artifacts(tmp_path: Path):
    with pytest.raises(PipelineError, match="script.json"):
        Pipeline().run("T", job_dir=tmp_path / "empty", from_visuals=True, publish=False)


def test_decide_auto_start_allows_voice_ready_pending_msg():
    ok, _reasons = decide_auto_start(
        classification="GREEN",
        auto_start=True,
        dry_run=False,
        smoke_ok=True,
        smoke_msg="ok",
        concurrent_busy=False,
        concurrent_msg="",
        budget_ok=True,
        budget_msg="",
        pending_ok=True,
        pending_msg="voice_ready=1",
        green_streak=1,
        min_green_streak=1,
    )
    assert ok is True


def test_prep_voice_env_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("RUNPOD_PREP_VOICE", raising=False)
    monkeypatch.delenv("RUNPOD_PREP_VOICE_MAX", raising=False)
    assert prep_voice_enabled() is True
    monkeypatch.setattr(
        "src.runpod.watchdog.ops_ready.prep_voice_buffer_target", lambda: 1
    )
    assert prep_voice_max() == 1
    monkeypatch.setenv("RUNPOD_PREP_VOICE", "0")
    assert prep_voice_enabled() is False
    monkeypatch.setattr(
        "src.runpod.watchdog.ops_ready.prep_voice_buffer_target", lambda: 2
    )
    assert prep_voice_max() == 2


def test_gpu_and_prep_locks_separate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.agents import farm as farm_mod

    monkeypatch.setenv("RUNPOD_PREP_WHILE_GPU", "1")
    monkeypatch.setenv("MAX_GPU_CONCURRENT", "1")
    monkeypatch.setenv("MAX_PREP_CONCURRENT", "1")
    monkeypatch.setattr(farm_mod, "pid_alive", lambda pid: bool(pid))

    store = OpsStore(tmp_path / "ops")
    gpu = store.create_job(
        "GPU Job",
        status="farming",
        stage="visuals",
        meta={"farm_pid": 1111, "farm_phase": "full"},
    )
    assert farm_mod.count_gpu_jobs(store) == 1
    assert farm_mod.count_prep_jobs(store) == 0
    assert farm_mod.count_inflight_jobs(store) == 1
    ok_gpu, _ = farm_mod.gpu_slot_available(store)
    assert ok_gpu is False
    # GPU on RunPod stills → prep allowed
    ok_prep, msg = farm_mod.prep_slot_available(store)
    assert ok_prep is True, msg

    # GPU still on local scripting → prep blocked (avoid dual Kokoro)
    store.update_job(gpu.id, stage="scripting")
    ok_local, msg_local = farm_mod.prep_slot_available(store)
    assert ok_local is False
    assert "local VPS" in msg_local

    store.update_job(gpu.id, stage="visuals")
    prep = store.create_job(
        "Prep Job",
        status="farming",
        stage="tts",
        meta={"farm_pid": 2222, "farm_phase": "prep"},
    )
    assert farm_mod.count_prep_jobs(store) == 1
    assert farm_mod.count_gpu_jobs(store) == 1
    # Prep does not consume the GPU inflight slot.
    assert farm_mod.count_inflight_jobs(store) == 1
    ok_prep2, msg2 = farm_mod.prep_slot_available(store)
    assert ok_prep2 is False
    assert "prep" in msg2

    # Park prep → prep_lock frees; with PREP_WHILE_GPU=0 still blocked by GPU
    store.update_job(
        prep.id,
        status="ready_for_stills",
        stage="awaiting_gpu",
        meta={"farm_phase": "prep", "farm_pid": None},
    )
    monkeypatch.setattr(farm_mod, "pid_alive", lambda pid: bool(pid and int(pid) == 1111))
    assert farm_mod.count_prep_jobs(store) == 0
    monkeypatch.setenv("RUNPOD_PREP_WHILE_GPU", "0")
    ok_prep3, msg3 = farm_mod.prep_slot_available(store)
    assert ok_prep3 is False
    assert "RUNPOD_PREP_WHILE_GPU=0" in msg3


def test_zombie_null_farm_pid_does_not_hold_gpu_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Rome TTS failure mode: farming/tts with farm_pid=null must not block MAX_GPU=1."""
    from src.agents import farm as farm_mod

    monkeypatch.setenv("MAX_GPU_CONCURRENT", "1")
    monkeypatch.setattr(farm_mod, "pid_alive", lambda pid: bool(pid))

    store = OpsStore(tmp_path / "ops")
    zombie = store.create_job(
        "What If Rome Never Fell But Transformed?",
        status="farming",
        stage="tts",
        meta={"farm_pid": None, "farm_phase": "full"},
    )
    assert farm_mod._is_encoding_job(zombie) is False
    assert farm_mod.count_gpu_jobs(store) == 0
    ok, msg = farm_mod.gpu_slot_available(store)
    assert ok is True
    assert "gpu_lock free" in msg

    # Dead PID also frees the lock.
    store.create_job(
        "Dead PID job",
        status="farming",
        stage="visuals",
        meta={"farm_pid": 999001, "farm_phase": "full"},
    )
    monkeypatch.setattr(farm_mod, "pid_alive", lambda pid: False)
    assert farm_mod.count_gpu_jobs(store) == 0

    # Only a live PID holds the slot.
    store.create_job(
        "Live GPU",
        status="farming",
        stage="visuals",
        meta={"farm_pid": 4242, "farm_phase": "full"},
    )
    monkeypatch.setattr(farm_mod, "pid_alive", lambda pid: int(pid or 0) == 4242)
    assert farm_mod.count_gpu_jobs(store) == 1


def test_reconcile_holds_stale_null_pid_tts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from src.agents import farm as farm_mod

    store = OpsStore(tmp_path / "ops")
    store.create_job(
        "Rome TTS zombie",
        status="farming",
        stage="tts",
        job_dir=str(tmp_path / "rome_job"),
        meta={"farm_pid": None, "farm_phase": "full", "resumable": True},
    )
    # edit_done without pid must NOT be auto-held (compose finished).
    store.create_job(
        "Scottish King done",
        status="farming",
        stage="edit_done",
        job_dir=str(tmp_path / "sk_job"),
        meta={"farm_pid": None, "farm_phase": "visuals", "gpu_lock_released": True},
    )
    (tmp_path / "sk_job" / "video").mkdir(parents=True)
    (tmp_path / "sk_job" / "video" / "final.mp4").write_bytes(b"\x00" * 2000)

    monkeypatch.setattr(farm_mod, "pid_alive", lambda pid: False)
    out = farm_mod.reconcile_farms(store=store, spawn_queued=False)
    dead_ids = {d["job_id"] for d in (out.get("dead") or [])}
    jobs = {j.title: j for j in store.list_jobs()}
    assert jobs["Rome TTS zombie"].status == "hold"
    assert any(d.get("reason") == "stale_null_farm_pid" for d in (out.get("dead") or []))
    assert jobs["Scottish King done"].status == "farming"


def test_max_gpu_concurrent_hard_capped_at_one(monkeypatch: pytest.MonkeyPatch):
    """Video A on GPU ⇒ Video B must never get a second pod slot."""
    monkeypatch.setenv("MAX_GPU_CONCURRENT", "5")
    assert max_gpu_concurrent() == 1
    monkeypatch.setenv("MAX_GPU_CONCURRENT", "1")
    assert max_gpu_concurrent() == 1


def test_spawn_second_gpu_blocked_by_gpu_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """HARD: while Video A holds visuals/full, Video B spawn is refused."""
    from src.agents import farm as farm_mod

    monkeypatch.setenv("MAX_GPU_CONCURRENT", "1")
    monkeypatch.setattr(farm_mod, "pid_alive", lambda pid: bool(pid))
    monkeypatch.setattr(farm_mod, "ROOT", tmp_path)

    store = OpsStore(tmp_path / "ops")
    store.create_job(
        "Video A on RunPod",
        status="farming",
        stage="visuals",
        meta={"farm_pid": 9001, "farm_phase": "full"},
    )
    job_dir = tmp_path / "jobs" / "b"
    (job_dir / "script").mkdir(parents=True)
    (job_dir / "audio").mkdir(parents=True)
    (job_dir / "script" / "script.json").write_text("{}", encoding="utf-8")
    (job_dir / "audio" / "voice_manifest.json").write_text("{}", encoding="utf-8")
    b = store.create_job(
        "Video B waiting",
        status="ready_for_stills",
        stage="awaiting_gpu",
        job_dir=str(job_dir),
        meta={"farm_phase": "prep"},
    )

    pops: list = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            pops.append(cmd)
            self.pid = 9999

    monkeypatch.setattr(farm_mod.subprocess, "Popen", FakePopen)

    out = farm_mod.spawn_farm_job(
        b.id, store=store, phase="visuals", skip_capacity_gate=True
    )
    assert out["ok"] is False
    assert out["spawned"] is False
    assert "gpu_lock blocked" in str(out.get("error") or "")
    assert pops == []


def test_gpu_lock_shared_across_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """napstorian stills hold the same gpu_lock that blocks napping_historian."""
    from src.agents import farm as farm_mod

    monkeypatch.setenv("MAX_GPU_CONCURRENT", "1")
    monkeypatch.setattr(farm_mod, "pid_alive", lambda pid: bool(pid))
    monkeypatch.setattr(farm_mod, "ROOT", tmp_path)

    store = OpsStore(tmp_path / "ops")
    store.create_job(
        "Napstorian on RunPod",
        status="farming",
        stage="visuals",
        meta={
            "farm_pid": 9001,
            "farm_phase": "visuals",
            "channel": "napstorian",
        },
    )
    job_dir = tmp_path / "jobs" / "nh"
    (job_dir / "script").mkdir(parents=True)
    (job_dir / "audio").mkdir(parents=True)
    (job_dir / "script" / "script.json").write_text("{}", encoding="utf-8")
    (job_dir / "audio" / "voice_manifest.json").write_text("{}", encoding="utf-8")
    b = store.create_job(
        "Napping Historian waiting",
        status="ready_for_stills",
        stage="awaiting_gpu",
        job_dir=str(job_dir),
        meta={"farm_phase": "prep", "channel": "napping_historian"},
    )

    assert farm_mod.count_gpu_jobs(store) == 1
    ok, msg = farm_mod.gpu_slot_available(store)
    assert ok is False
    assert "gpu_lock busy" in msg

    pops: list = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            pops.append(cmd)
            self.pid = 9999

    monkeypatch.setattr(farm_mod.subprocess, "Popen", FakePopen)

    out = farm_mod.spawn_farm_job(
        b.id, store=store, phase="visuals", skip_capacity_gate=True
    )
    assert out["ok"] is False
    assert out["spawned"] is False
    assert "gpu_lock blocked" in str(out.get("error") or "")
    assert pops == []


def test_prep_lock_shared_across_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """One channel's Phase A prep fills the shared prep_lock for the other."""
    from src.agents import farm as farm_mod

    monkeypatch.setenv("MAX_PREP_CONCURRENT", "1")
    monkeypatch.setattr(farm_mod, "pid_alive", lambda pid: bool(pid))

    store = OpsStore(tmp_path / "ops")
    store.create_job(
        "Napstorian prep",
        status="farming",
        stage="scripting",
        meta={
            "farm_pid": 7001,
            "farm_phase": "prep",
            "channel": "napstorian",
        },
    )
    assert farm_mod.count_prep_jobs(store) == 1
    ok, msg = farm_mod.prep_slot_available(store)
    assert ok is False
    assert "prep_lock busy" in msg


def test_compose_releases_gpu_lock_for_next_pod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """After stills (pod dead), Video A compose must not block Video B RunPod."""
    from src.agents import farm as farm_mod
    from src.runpod import watchdog as wd

    monkeypatch.setenv("MAX_GPU_CONCURRENT", "1")
    monkeypatch.setenv("RUNPOD_PREP_WHILE_GPU", "1")
    monkeypatch.setattr(farm_mod, "pid_alive", lambda pid: bool(pid))
    monkeypatch.setattr(farm_mod, "ROOT", tmp_path)

    store = OpsStore(tmp_path / "ops")
    a_dir = tmp_path / "jobs" / "a"
    (a_dir / "images").mkdir(parents=True)
    (a_dir / "images" / "visual_manifest.json").write_text("{}", encoding="utf-8")
    # Stale stage=scripting (pre-fix ops) but stills already on disk — Anne case.
    store.create_job(
        "Video A composing",
        status="farming",
        stage="scripting",
        job_dir=str(a_dir),
        meta={"farm_pid": 9001, "farm_phase": "full"},
    )
    assert farm_mod.count_gpu_jobs(store) == 0
    assert farm_mod.count_inflight_jobs(store) == 0
    ok_gpu, msg = farm_mod.gpu_slot_available(store)
    assert ok_gpu is True, msg
    # Prep still blocked — ffmpeg + Kokoro on 4vCPU is unsafe.
    ok_prep, prep_msg = farm_mod.prep_slot_available(store)
    assert ok_prep is False
    assert "local VPS" in prep_msg

    _real_gpu = farm_mod.count_gpu_jobs
    _real_inflight = farm_mod.count_inflight_jobs
    monkeypatch.setattr(
        "src.agents.farm.count_gpu_jobs",
        lambda *a, **k: _real_gpu(store),
    )
    monkeypatch.setattr(
        "src.agents.farm.count_inflight_jobs",
        lambda *a, **k: _real_inflight(store),
    )
    busy, busy_msg = wd._concurrent_busy()
    assert busy is False, busy_msg

    b_dir = tmp_path / "jobs" / "b"
    (b_dir / "script").mkdir(parents=True)
    (b_dir / "audio").mkdir(parents=True)
    (b_dir / "script" / "script.json").write_text("{}", encoding="utf-8")
    (b_dir / "audio" / "voice_manifest.json").write_text("{}", encoding="utf-8")
    b = store.create_job(
        "Video B ready",
        status="ready_for_stills",
        stage="awaiting_gpu",
        job_dir=str(b_dir),
        meta={"farm_phase": "prep"},
    )
    pops: list = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            pops.append(cmd)
            self.pid = 9999

    monkeypatch.setattr(farm_mod.subprocess, "Popen", FakePopen)
    out = farm_mod.spawn_farm_job(
        b.id, store=store, phase="visuals", skip_capacity_gate=True
    )
    assert out["ok"] is True
    assert out["spawned"] is True
    assert pops and "--from-visuals" in pops[0]


def test_ready_for_derivatives_hook(tmp_path: Path):
    job_dir = tmp_path / "job"
    (job_dir / "script").mkdir(parents=True)
    (job_dir / "audio").mkdir(parents=True)
    (job_dir / "script" / "script.json").write_text("{}", encoding="utf-8")
    (job_dir / "audio" / "voice_manifest.json").write_text("{}", encoding="utf-8")
    assert content_kernel_ready(job_dir) is True
    meta = mark_ready_for_derivatives({"farm_phase": "prep"})
    assert meta["ready_for_derivatives"] is True
    assert meta["content_kernel"] is True
    assert "podcast_audio" in meta["derivative_modules_planned"]

    store = OpsStore(tmp_path / "ops")
    store.create_job(
        "Kernel parked",
        status="ready_for_stills",
        stage="awaiting_gpu",
        job_dir=str(job_dir),
        meta=meta,
    )
    assert len(list_ready_for_derivatives(store)) == 1


def test_concurrent_busy_ignores_prep_only(monkeypatch: pytest.MonkeyPatch):
    from src.runpod import watchdog as wd

    monkeypatch.setattr("src.agents.farm.count_gpu_jobs", lambda: 0)
    monkeypatch.setattr("src.agents.farm.max_gpu_concurrent", lambda: 1)
    busy, msg = wd._concurrent_busy()
    assert busy is False
    assert "gpu=0" in msg

    monkeypatch.setattr("src.agents.farm.count_gpu_jobs", lambda: 1)
    busy2, msg2 = wd._concurrent_busy()
    assert busy2 is True
    assert "gpu_lock busy" in msg2


def test_concurrent_busy_ignores_queued_inflight(monkeypatch: pytest.MonkeyPatch):
    """Queued script repairs must not fake gpu_lock busy when pod is free."""
    from src.runpod import watchdog as wd

    monkeypatch.setattr("src.agents.farm.count_gpu_jobs", lambda: 0)
    # Even if inflight were high, concurrent busy is GPU-only now.
    monkeypatch.setattr("src.agents.farm.count_inflight_jobs", lambda: 9)
    monkeypatch.setattr("src.agents.farm.max_gpu_concurrent", lambda: 1)
    busy, msg = wd._concurrent_busy()
    assert busy is False
    assert "gpu=0" in msg


def test_spawn_prep_skips_green_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.agents import farm as farm_mod

    store = OpsStore(tmp_path / "ops")
    job = store.create_job("Prep Title", status="queued", stage="queued")

    def boom_green(*a, **k):
        raise AssertionError("prep must not call GREEN capacity gate")

    monkeypatch.setattr(
        "src.runpod.capacity.check_farm_capacity_green", boom_green
    )
    monkeypatch.setattr(
        "src.runpod.guards.image_backend_is_runpod_pod", lambda: True
    )
    monkeypatch.setattr(farm_mod, "ROOT", tmp_path)
    monkeypatch.setattr(
        farm_mod,
        "farm_flags_from_config",
        lambda: {
            "mock_images": False,
            "publish": True,
            "publish_dry_run": False,
            "test_mode": False,
        },
    )

    pops: list = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            pops.append(cmd)
            self.pid = 4242

    monkeypatch.setattr(farm_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(farm_mod, "pid_alive", lambda pid: False)

    out = farm_mod.spawn_farm_job(job.id, store=store, phase="prep")
    assert out["ok"] is True
    assert out["spawned"] is True
    assert out["farm_phase"] == "prep"
    assert "--stop-after-voice" in pops[0]
    assert "--no-publish" in pops[0]


def test_spawn_visuals_adds_from_visuals_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from src.agents import farm as farm_mod

    store = OpsStore(tmp_path / "ops")
    job_dir = tmp_path / "jobs" / "voice_ready"
    (job_dir / "script").mkdir(parents=True)
    (job_dir / "audio").mkdir(parents=True)
    (job_dir / "script" / "script.json").write_text("{}", encoding="utf-8")
    (job_dir / "audio" / "voice_manifest.json").write_text("{}", encoding="utf-8")
    job = store.create_job(
        "Visuals Title",
        status="ready_for_stills",
        stage="awaiting_gpu",
        job_dir=str(job_dir),
    )

    monkeypatch.setattr(farm_mod, "ROOT", tmp_path)
    monkeypatch.setattr(
        farm_mod,
        "farm_flags_from_config",
        lambda: {
            "mock_images": False,
            "publish": True,
            "publish_dry_run": False,
            "test_mode": False,
        },
    )
    pops: list = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            pops.append(cmd)
            self.pid = 5252

    monkeypatch.setattr(farm_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(farm_mod, "pid_alive", lambda pid: False)

    out = farm_mod.spawn_farm_job(
        job.id, store=store, phase="visuals", skip_capacity_gate=True
    )
    assert out["ok"] is True
    assert out["farm_phase"] == "visuals"
    assert "--from-visuals" in pops[0]
    assert "--resume" in pops[0]
    updated = store.get_job(job.id)
    assert updated.stage == "visuals"
