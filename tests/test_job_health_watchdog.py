"""Unit tests for job_health_watchdog auto-heal rules."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.agents.job_health_watchdog import (
    _needs_compose_sfx,
    _publish_terminal_status,
    _publish_wanted,
    _sfx_applied,
    heal_job,
)


def _job(**kwargs):
    defaults = {
        "id": "job_test123",
        "title": "Test",
        "status": "farming",
        "stage": "edit",
        "video_id": None,
        "job_dir": "/tmp/nonexistent",
        "meta": {},
    }
    defaults.update(kwargs)
    rec = MagicMock()
    for k, v in defaults.items():
        setattr(rec, k, v)
    return rec


def test_sfx_applied_detects_pre_sfx(tmp_path: Path):
    video = tmp_path / "video"
    video.mkdir(parents=True)
    pre = video / "final_pre_sfx.mp4"
    pre.write_bytes(b"x" * 2000)
    assert _sfx_applied(tmp_path) is True


def test_publish_wanted_respects_flags():
    j = _job(meta={"farm_flags": {"publish": True}})
    assert _publish_wanted(j) is True
    j2 = _job(meta={"farm_flags": {"publish": False}})
    assert _publish_wanted(j2) is False


def test_needs_compose_sfx_napstorian():
    j = _job(meta={"channel": "napstorian", "compose_sfx_expected": True})
    assert _needs_compose_sfx(j) is True


def test_heal_dead_farm_queues_resume(tmp_path: Path):
    job_dir = tmp_path / "job"
    video = job_dir / "video"
    video.mkdir(parents=True)
    script = job_dir / "script"
    script.mkdir(parents=True)
    (script / "script.json").write_text('{"scenes":[]}', encoding="utf-8")
    (video / "final.mp4").write_bytes(b"x" * 2000)

    job = _job(
        id="job_dead1",
        job_dir=str(job_dir),
        status="farming",
        stage="edit",
        meta={"farm_pid": None, "farm_flags": {"publish": True}},
    )
    store = MagicMock()
    store.get_job.return_value = job
    ledger = MagicMock()

    with patch(
        "src.agents.job_health_watchdog._finish_publish_for_job",
        return_value={"job_id": "job_dead1", "action": "publish_only", "ok": True},
    ) as pub:
        with patch("src.agents.job_health_watchdog._ffmpeg_pids_for_job", return_value=[]):
            with patch("src.agents.job_health_watchdog._farm_pids_for_job", return_value=[]):
                with patch(
                    "src.agents.job_health_watchdog._needs_compose_sfx",
                    return_value=False,
                ):
                    result = heal_job(job, store=store, ledger=ledger, spawn=True)

    assert result is not None
    assert result.get("action") == "publish_only"
    pub.assert_called_once()


def test_heal_skips_permanent_skip():
    job = _job(error="PERMANENT SKIP — Tudor-only; do not spawn")
    store = MagicMock()
    ledger = MagicMock()
    assert heal_job(job, store=store, ledger=ledger) is None


def test_publish_terminal_status_with_video_id():
    j = _job(status="farming", video_id="abc123")
    assert _publish_terminal_status(j) is True
    j2 = _job(status="private", video_id="abc123")
    assert _publish_terminal_status(j2) is True
    j3 = _job(status="farming", video_id=None)
    assert _publish_terminal_status(j3) is False


def test_heal_skips_farming_with_video_id():
    """Uploaded job stuck in farming must not re-farm visuals."""
    job = _job(
        id="job_pub1",
        status="farming",
        stage="visuals",
        video_id="GVUJpH0wRlk",
        meta={"farm_pid": None, "farm_phase": "visuals"},
    )
    store = MagicMock()
    ledger = MagicMock()
    with patch("src.agents.job_health_watchdog._ffmpeg_pids_for_job", return_value=[]):
        with patch("src.agents.job_health_watchdog._farm_pids_for_job", return_value=[]):
            assert heal_job(job, store=store, ledger=ledger, spawn=True) is None
    store.update_job.assert_not_called()
