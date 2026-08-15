"""Tests for SMM new-format every-video SOP compliance helper."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agents.smm_sop import (
    SOP_ALERT_TYPE,
    SOP_STAGE_GATE_TYPE,
    SopStageGateError,
    audit_stage_complete,
    check_new_format_sop,
    check_stage_sop,
    gate_stage_advance,
    job_from_dir,
    maybe_gate_pipeline_stage,
    stamp_new_format_sop_checklist,
    write_sop_compliance_md,
)
from src.agents.store import JobRecord


def _write_edit_manifest(
    job_dir: Path,
    *,
    overlays: list | None = None,
    premium: bool = False,
    compose_sfx: bool | None = None,
    selected_pack: bool = True,
) -> None:
    video = job_dir / "video"
    video.mkdir(parents=True, exist_ok=True)
    (video / "final.mp4").write_bytes(b"fake")
    meta: dict = {
        "selected_pack": selected_pack,
        "new_format": True,
        "infographic_overlays": overlays or [],
        "compose_infographic_overlays_enabled": bool(overlays),
        "premium_overlays_v1": premium,
        "loudness_normalize": True,
        "compose_date_overlay_enabled": True,
    }
    if compose_sfx is not None:
        meta["compose_sfx"] = compose_sfx
        meta["compose_sfx_meta"] = {
            "compose_sfx": compose_sfx,
            "event_count": 3 if compose_sfx else 0,
        }
    if premium:
        meta["premium_plan"] = {"pack": "premium_overlays_v1", "cards": [{"kind": "fog"}]}
    (video / "edit_manifest.json").write_text(
        json.dumps(
            {
                "final_path": str(video / "final.mp4"),
                "scenes": [{"i": 0}],
                "meta": meta,
            }
        ),
        encoding="utf-8",
    )


def _write_packaging(job_dir: Path, *, chapters: bool = True) -> None:
    meta_dir = job_dir / "youtube_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "title.txt").write_text("What If Test Title\n", encoding="utf-8")
    (meta_dir / "description.txt").write_text("Desc\n", encoding="utf-8")
    (meta_dir / "tags.txt").write_text("tudor,history\n", encoding="utf-8")
    if chapters:
        (meta_dir / "chapters.txt").write_text(
            "Chapters:\n0:00 Hook\n1:00 Context\n", encoding="utf-8"
        )


def _write_script(job_dir: Path) -> None:
    d = job_dir / "script"
    d.mkdir(parents=True, exist_ok=True)
    (d / "script.json").write_text(
        json.dumps(
            {
                "title": "T",
                "topic": "T",
                "scenes": [{"id": 1, "narration": "Once upon a time"}],
                "meta": {},
            }
        ),
        encoding="utf-8",
    )


def _write_voice(job_dir: Path) -> None:
    d = job_dir / "audio"
    d.mkdir(parents=True, exist_ok=True)
    (d / "voice_manifest.json").write_text(
        json.dumps({"files": ["a.wav"], "meta": {"backend": "kokoro"}}),
        encoding="utf-8",
    )


def test_stamp_new_format_sop_checklist_channel_sfx():
    nap = stamp_new_format_sop_checklist({}, channel="napstorian", stage="compose")
    assert nap["selected_pack"] is True
    assert nap["new_format"] is True
    assert nap["sop_checklist"]["must"]["infographic_overlays"] is True
    assert nap["compose_sfx_expected"] is True
    assert nap["sop_checklist"]["must"]["compose_sfx"] is True
    assert nap["sop_checklist"]["must"]["hook_generator"] is True
    assert nap["sop_checklist"]["must"]["asset_fetcher"] is True
    assert nap["sop_checklist"]["must"]["gate_r_advisory"] is True
    assert nap["pd_motion_expected"] is True
    assert nap["sop_checklist"]["must"]["pd_motion"] is True
    assert nap["channel_tone"] == "punchy_what_if"
    assert nap["prompts_channel"] == "napstorian"

    hist = stamp_new_format_sop_checklist({}, channel="napping_historian", stage="publish")
    assert hist["compose_sfx_expected"] is False
    assert hist["sop_checklist"]["must"]["historian_sfx_off"] is True
    assert hist["sop_checklist"]["should"]["pd_clippings"] is True
    assert hist["prompts_channel"] == "napping_historian"
    assert hist["sop_checklist"]["must"]["channel_prompts"] is True
    assert hist["sop_checklist"]["must"]["gate_r_enforce"] is False
    assert hist["pd_motion_expected"] is False
    assert hist["sop_checklist"]["must"]["pd_motion"] is False
    assert hist["channel_tone"] == "calm_sleep"
    assert hist["hook_generator"] is True
    assert hist["asset_fetcher"] is True
    assert hist["gate_r_advisory"] is True


def test_stamp_applied_for_all_known_channels():
    """Every Brand channel in KNOWN_CHANNELS gets full new-format stamps."""
    from src.agents.smm_sop import is_new_format_stamped, missing_new_format_stamp_keys
    from src.services.youtube_channel_auth import KNOWN_CHANNELS

    assert "napstorian" in KNOWN_CHANNELS
    assert "napping_historian" in KNOWN_CHANNELS
    for ch in KNOWN_CHANNELS:
        meta = stamp_new_format_sop_checklist(
            {"source": "title_queue", "channel": ch},
            channel=ch,
            stage="enqueue",
        )
        assert is_new_format_stamped(meta), (ch, missing_new_format_stamp_keys(meta))
        assert meta["selected_pack"] is True
        assert meta["new_format"] is True
        assert meta["hook_generator"] is True
        assert meta["asset_fetcher"] is True
        assert meta["gate_r_advisory"] is True
        assert meta["premium_overlays_v1"] is True
        assert meta["prompts_channel"] == ch
        assert meta["sop_checklist"]["stage"] == "enqueue"
        assert meta["sop_checklist"]["channel"] == ch
        if ch == "napping_historian":
            assert meta["compose_sfx_expected"] is False
            assert meta["pd_motion_expected"] is False
        else:
            assert meta["compose_sfx_expected"] is True
            assert meta["pd_motion_expected"] is True


def test_pick_and_enqueue_stamps_both_channels(tmp_path: Path, monkeypatch):
    """Farm enqueue dry-run: create_job meta is fully new-format stamped per channel."""
    from src.agents import worker as worker_mod
    from src.agents.smm_sop import is_new_format_stamped
    from src.agents.store import OpsStore
    from src.services.youtube_channel_auth import KNOWN_CHANNELS

    store = OpsStore(tmp_path / "ops")
    created: list[dict] = []

    class _Row:
        def __init__(self, channel: str, title: str, row_index: int):
            self.channel = channel
            self.title = title
            self.row_index = row_index
            self.notes = ""
            self.job_id = ""
            self.trend_score = 0.9
            self.approved = True
            self.policy_ok = True
            self.status = "queued"

    class FakeQueue:
        def count_buffer(self) -> int:
            return 0

        def inventory_approved(self):
            rows = [
                _Row(ch, f"What If Test {ch}?", i + 2)
                for i, ch in enumerate(KNOWN_CHANNELS)
            ]
            return {"ready": rows, "skipped_have_job_id": []}

        def pick_approved(self, limit: int = 1, channel=None, mutate_state=True):
            # One job per known channel
            return [
                _Row(ch, f"What If Test {ch}?", i + 2)
                for i, ch in enumerate(KNOWN_CHANNELS)
            ][:limit]

        def resolve_profile(self, row):
            return "epic" if row.channel == "napping_historian" else "retention"

        def update_row(self, *args, **kwargs):
            return None

    class FakeTrends:
        def __init__(self, *a, **k):
            pass

        def precheck_title(self, title, skip_row_index=None):
            return True, []

    class FakeWatchdog:
        def __init__(self, *a, **k):
            pass

        def on_stage(self, *a, **k):
            return None

    class FakeCost:
        def __init__(self, *a, **k):
            pass

        def check_can_start_job(self):
            return True, ""

        def record(self, *a, **k):
            return None

    class FakeLedger:
        def __init__(self, *a, **k):
            pass

        def write(self, *a, **k):
            return None

    real_create = store.create_job

    def tracking_create(title, **kwargs):
        job = real_create(title, **kwargs)
        created.append({"title": title, "meta": dict(job.meta or {}), "id": job.id})
        return job

    monkeypatch.setattr(worker_mod, "OpsStore", lambda: store)
    monkeypatch.setattr(worker_mod, "TitleQueue", FakeQueue)
    monkeypatch.setattr(worker_mod, "TrendsAgent", FakeTrends)
    monkeypatch.setattr(worker_mod, "WatchdogAgent", FakeWatchdog)
    monkeypatch.setattr(worker_mod, "CostGuardian", FakeCost)
    monkeypatch.setattr(worker_mod, "OpsLedger", FakeLedger)
    monkeypatch.setattr(worker_mod, "count_gpu_jobs", lambda s: 0)
    monkeypatch.setattr(worker_mod, "count_inflight_jobs", lambda s: 0)
    monkeypatch.setattr(worker_mod, "count_encoding_jobs", lambda s: 0)
    monkeypatch.setattr(worker_mod, "max_gpu_concurrent", lambda: 1)
    monkeypatch.setattr(store, "create_job", tracking_create)
    monkeypatch.setattr(
        worker_mod,
        "_agents_cfg",
        lambda: {"max_jobs_per_pick": 2, "max_concurrent_jobs": 1},
    )
    monkeypatch.setattr(
        worker_mod, "_schedule_cfg", lambda: {"buffer_target": 99}
    )

    results = worker_mod.pick_and_enqueue(limit=2, enqueue_pipeline=False)
    assert len(results) == 2
    assert len(created) == 2
    channels_seen = {c["meta"].get("channel") for c in created}
    assert channels_seen == set(KNOWN_CHANNELS)
    for c in created:
        assert is_new_format_stamped(c["meta"]), c["meta"].get("channel")
        assert c["meta"].get("sop_checklist", {}).get("stage") == "enqueue"
        assert c["meta"]["hook_generator"] is True
        assert c["meta"]["asset_fetcher"] is True
        assert c["meta"]["gate_r_advisory"] is True


def test_check_ok_with_overlays_and_sfx(tmp_path: Path):
    job_dir = tmp_path / "job_ok"
    _write_edit_manifest(
        job_dir,
        overlays=[{"scene": 2, "style": "timeline"}, {"scene": 5, "style": "map"}],
        premium=True,
        compose_sfx=True,
    )
    _write_packaging(job_dir)
    meta = stamp_new_format_sop_checklist(
        {
            "channel": "napstorian",
            "smm_pin_comment_id": "Ug123",
            "smm_chapters_ensured_at": "t",
        },
        channel="napstorian",
    )
    job = JobRecord(
        id="job_ok",
        title="What If a Tudor Queen Married a Scottish King?",
        status="public",
        job_dir=str(job_dir),
        meta=meta,
        video_id="vid_ok",
    )
    result = check_new_format_sop(job, for_public=True)
    assert result["ok"] is True
    assert result["n_failures"] == 0
    assert any(w["code"] == "pd_clippings_missing" for w in result["warnings"])
    assert all(p["type"] == SOP_ALERT_TYPE for p in result["proposals"])


def test_check_fails_without_overlays(tmp_path: Path):
    job_dir = tmp_path / "job_bare"
    _write_edit_manifest(job_dir, overlays=[], premium=False, compose_sfx=True)
    meta = stamp_new_format_sop_checklist({"channel": "napstorian"}, channel="napstorian")
    job = JobRecord(
        id="job_bare",
        title="Bare Final",
        status="private",
        job_dir=str(job_dir),
        meta=meta,
    )
    result = check_new_format_sop(job, for_public=False)
    assert result["ok"] is False
    codes = {f["code"] for f in result["failures"]}
    assert "missing_infographic_overlays" in codes
    assert "missing_premium_overlays_v1" in codes


def test_historian_sfx_on_is_hard_fail(tmp_path: Path):
    job_dir = tmp_path / "job_hist"
    _write_edit_manifest(
        job_dir,
        overlays=[{"scene": 1}],
        premium=True,
        compose_sfx=True,
    )
    meta = stamp_new_format_sop_checklist(
        {"channel": "napping_historian"}, channel="napping_historian"
    )
    job = JobRecord(
        id="job_hist",
        title="Sleep History",
        status="private",
        job_dir=str(job_dir),
        meta=meta,
    )
    result = check_new_format_sop(job, for_public=False)
    assert result["ok"] is False
    assert any(f["code"] == "historian_sfx_on" for f in result["failures"])


def test_napstorian_sfx_missing_hard_fail(tmp_path: Path):
    job_dir = tmp_path / "job_nosfx"
    _write_edit_manifest(
        job_dir,
        overlays=[{"scene": 1}],
        premium=True,
        compose_sfx=False,
    )
    meta = stamp_new_format_sop_checklist({"channel": "napstorian"}, channel="napstorian")
    job = JobRecord(
        id="job_nosfx",
        title="No SFX",
        status="private",
        job_dir=str(job_dir),
        meta=meta,
    )
    result = check_new_format_sop(job, for_public=False)
    assert any(f["code"] == "napstorian_sfx_missing" for f in result["failures"])


def test_pd_clippings_warn_only(tmp_path: Path):
    job_dir = tmp_path / "job_pd"
    _write_edit_manifest(
        job_dir,
        overlays=[{"scene": 1}],
        premium=True,
        compose_sfx=True,
    )
    meta = stamp_new_format_sop_checklist({"channel": "napstorian"}, channel="napstorian")
    job = JobRecord(
        id="job_pd",
        title="Flux Only",
        status="private",
        job_dir=str(job_dir),
        meta=meta,
    )
    result = check_new_format_sop(job, for_public=False)
    assert all(
        w["code"] != "pd_clippings_missing" or w["level"] == "should"
        for w in result["warnings"]
    )
    assert not any(f["code"] == "pd_clippings_missing" for f in result["failures"])


def test_pd_clippings_json_and_pd_heroes_detected(tmp_path: Path):
    from src.agents.smm_sop import _pd_clippings_evidence

    job_dir = tmp_path / "job_pd_yes"
    job_dir.mkdir()
    (job_dir / "images" / "pd_heroes" / "fitted").mkdir(parents=True)
    (job_dir / "images" / "pd_heroes" / "fitted" / "mary.jpg").write_bytes(b"x")
    (job_dir / "pd_clippings.json").write_text(
        json.dumps({"ok": True, "pd_clippings": True, "pd_clip_count": 8, "heroes": [{}] * 8}),
        encoding="utf-8",
    )
    ev = _pd_clippings_evidence(job_dir, {})
    assert ev["present"] is True
    assert ev["count"] >= 8
    assert "pd_clippings.json" in ev["sources"]

    _write_edit_manifest(
        job_dir,
        overlays=[{"scene": 1}],
        premium=True,
        compose_sfx=True,
    )
    meta = stamp_new_format_sop_checklist({"channel": "napstorian"}, channel="napstorian")
    job = JobRecord(
        id="job_pd_yes",
        title="With PD",
        status="private",
        job_dir=str(job_dir),
        meta=meta,
    )
    result = check_new_format_sop(job, for_public=False)
    assert result["checks"]["pd_clippings"] is True
    assert not any(w["code"] == "pd_clippings_missing" for w in result["warnings"])


def test_public_missing_packaging_fails(tmp_path: Path):
    job_dir = tmp_path / "job_pub"
    _write_edit_manifest(
        job_dir,
        overlays=[{"scene": 1}],
        premium=True,
        compose_sfx=True,
    )
    meta = stamp_new_format_sop_checklist({"channel": "napstorian"}, channel="napstorian")
    job = JobRecord(
        id="job_pub",
        title="Public Bare",
        status="public",
        job_dir=str(job_dir),
        meta=meta,
        video_id="vid_pub",
    )
    result = check_new_format_sop(job, for_public=True)
    codes = {f["code"] for f in result["failures"]}
    assert "missing_packaging" in codes
    assert "missing_chapters_path" in codes


def test_stage_script_and_tts_gates(tmp_path: Path):
    job_dir = tmp_path / "job_stages"
    meta = stamp_new_format_sop_checklist({"channel": "napstorian"}, channel="napstorian")
    job = job_from_dir(job_dir, channel="napstorian", meta=meta)

    script_fail = check_stage_sop(job, "script")
    assert script_fail["ok"] is False
    assert script_fail["remediation"]["resend_stage"] == "script"

    _write_script(job_dir)
    script_ok = check_stage_sop(job, "script")
    assert script_ok["ok"] is True

    tts_fail = check_stage_sop(job, "tts")
    assert tts_fail["ok"] is False
    assert tts_fail["remediation"]["action"] == "resend_stage"
    _write_voice(job_dir)
    assert check_stage_sop(job, "tts")["ok"] is True


def test_compose_stage_gate_resend_and_compliance_md(tmp_path: Path):
    job_dir = tmp_path / "job_compose_gate"
    _write_edit_manifest(job_dir, overlays=[], premium=False, compose_sfx=True)
    meta = stamp_new_format_sop_checklist({"channel": "napstorian"}, channel="napstorian")
    job = job_from_dir(job_dir, channel="napstorian", meta=meta, status="farming")

    dig = check_stage_sop(job, "compose", smm_cfg={"selected_pack_default": True})
    assert dig["ok"] is False
    assert dig["remediation"]["resend_stage"] == "compose"
    assert dig["proposals"][0]["type"] == SOP_STAGE_GATE_TYPE

    path = write_sop_compliance_md(job, stage_digest=dig)
    assert path is not None and path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "SOP compliance" in text
    assert "resend" in text.lower()


def test_gate_stage_advance_hard_raises(tmp_path: Path):
    job_dir = tmp_path / "job_hard"
    _write_edit_manifest(job_dir, overlays=[], premium=False, compose_sfx=True)
    meta = stamp_new_format_sop_checklist({"channel": "napstorian"}, channel="napstorian")
    job = job_from_dir(job_dir, channel="napstorian", meta=meta)

    with pytest.raises(SopStageGateError) as ei:
        gate_stage_advance(
            job,
            completed_stage="compose",
            next_stage="package",
            smm_cfg={"sop_stage_gates": True, "sop_stage_gates_hard": True},
            hard=True,
        )
    assert ei.value.digest.get("remediation", {}).get("resend_stage") == "compose"


def test_gate_stage_advance_soft_does_not_raise(tmp_path: Path):
    job_dir = tmp_path / "job_soft"
    _write_edit_manifest(job_dir, overlays=[], premium=False, compose_sfx=True)
    meta = stamp_new_format_sop_checklist({"channel": "napstorian"}, channel="napstorian")
    job = job_from_dir(job_dir, channel="napstorian", meta=meta)
    dig = gate_stage_advance(
        job,
        completed_stage="compose",
        next_stage="package",
        smm_cfg={"sop_stage_gates": True, "sop_stage_gates_hard": False},
        hard=False,
    )
    assert dig["ok"] is False
    assert dig.get("blocked") is False
    assert (job_dir / "ops" / "SOP_COMPLIANCE.md").is_file()


def test_maybe_gate_entering_publish_checks_compose(tmp_path: Path):
    job_dir = tmp_path / "job_enter_pub"
    _write_edit_manifest(
        job_dir,
        overlays=[{"scene": 1}],
        premium=True,
        compose_sfx=True,
    )
    meta = stamp_new_format_sop_checklist({"channel": "napstorian"}, channel="napstorian")
    job = job_from_dir(job_dir, channel="napstorian", meta=meta)
    dig = maybe_gate_pipeline_stage(
        job,
        entering_stage="publish",
        smm_cfg={"sop_stage_gates": True, "sop_stage_gates_hard": False},
        hard=False,
    )
    assert dig.get("skipped") is not True
    assert dig["stage"] == "compose"
    assert dig["ok"] is True


def test_title_promise_soft_warn_when_cold_open_mismatches(tmp_path: Path):
    """Title tokens missing from cold open → soft warn only (never hard fail)."""
    job_dir = tmp_path / "job_title_promise"
    script_dir = job_dir / "script"
    script_dir.mkdir(parents=True)
    (script_dir / "script.json").write_text(
        json.dumps(
            {
                "title": "What If Anne Boleyn Ruled England Alone",
                "hook": "A sealed letter changes nothing about weather.",
                "scenes": [
                    {
                        "index": 0,
                        "chapter_id": 1,
                        "text": "Rain falls on empty cobblestones in a forgotten alley.",
                    },
                    {
                        "index": 1,
                        "chapter_id": 1,
                        "text": "Candlelight flickers while dust settles on a shelf.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    meta = stamp_new_format_sop_checklist({"channel": "napstorian"}, channel="napstorian")
    job = JobRecord(
        id="job_title_promise",
        title="What If Anne Boleyn Ruled England Alone",
        status="scripting",
        job_dir=str(job_dir),
        meta=meta,
    )
    result = check_stage_sop(job, stage="script", smm_cfg={"sop_stage_gates": True})
    assert result["ok"] is True  # soft — does not hard-fail script stage
    assert any(
        w["code"] == "title_promise_weak_in_cold_open" for w in result["warnings"]
    )
    tp = result["checks"]["title_promise_cold_open"]
    assert tp["ok"] is False
    assert "anne" in tp["missing"] or "boleyn" in tp["missing"]


def test_title_promise_ok_when_tokens_present(tmp_path: Path):
    from src.agents.smm_sop import _title_promise_in_cold_open

    job_dir = tmp_path / "job_title_ok"
    script_dir = job_dir / "script"
    script_dir.mkdir(parents=True)
    (script_dir / "script.json").write_text(
        json.dumps(
            {
                "title": "What If a Tudor Queen Married a Scottish King",
                "hook": "Mary Tudor nearly wed the Scottish king.",
                "scenes": [
                    {
                        "index": 0,
                        "chapter_id": 1,
                        "text": (
                            "What if a Tudor queen married a Scottish king — "
                            "uniting the crowns before the Union?"
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    tp = _title_promise_in_cold_open(job_dir)
    assert tp["ok"] is True
    assert "tudor" in tp["matched"]
    assert "scottish" in tp["matched"]


def test_package_stage_and_audit(tmp_path: Path):
    job_dir = tmp_path / "job_pkg"
    _write_packaging(job_dir)
    meta = stamp_new_format_sop_checklist({"channel": "napstorian"}, channel="napstorian")
    job = job_from_dir(job_dir, channel="napstorian", meta=meta)
    dig = audit_stage_complete(
        job,
        completed_stage="package",
        smm_cfg={"sop_stage_gates": True},
        hard=False,
    )
    assert dig["ok"] is True
    assert dig.get("compliance_path")
