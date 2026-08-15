"""Regression: disk_guard threshold + always-on xfade + post-public purge."""

from __future__ import annotations

import json
from pathlib import Path

from src.runpod import disk_guard as dg
from src.runpod.disk_guard import (
    DEFAULT_POLICY,
    always_prune_xfade_tmps,
    cleanup_public_job_media,
    target_reached,
    threshold_crossed,
)


def test_threshold_crossed_at_85_percent() -> None:
    policy = dict(DEFAULT_POLICY)
    snap = {
        "used_percent": 85.0,
        "free_gb": 20.0,
        "used_bytes": 85,
        "free_bytes": 15,
        "total_bytes": 100,
    }
    assert threshold_crossed(snap, policy) is True


def test_threshold_crossed_when_free_gb_low() -> None:
    policy = dict(DEFAULT_POLICY)
    snap = {
        "used_percent": 50.0,
        "free_gb": 7.5,
        "used_bytes": 50,
        "free_bytes": 8,
        "total_bytes": 100,
    }
    assert threshold_crossed(snap, policy) is True


def test_threshold_not_crossed_when_healthy() -> None:
    policy = dict(DEFAULT_POLICY)
    snap = {
        "used_percent": 40.0,
        "free_gb": 40.0,
        "used_bytes": 40,
        "free_bytes": 60,
        "total_bytes": 100,
    }
    assert threshold_crossed(snap, policy) is False


def test_target_reached_at_75_and_15g() -> None:
    policy = dict(DEFAULT_POLICY)
    ok = {
        "used_percent": 74.0,
        "free_gb": 16.0,
    }
    assert target_reached(ok, policy) is True
    not_yet = {
        "used_percent": 80.0,
        "free_gb": 16.0,
    }
    assert target_reached(not_yet, policy) is False


def test_compulsory_defaults() -> None:
    assert DEFAULT_POLICY.get("compulsory") is True
    assert float(DEFAULT_POLICY["trigger_used_percent"]) == 85.0
    assert DEFAULT_POLICY.get("xfade_always_when_final") is True
    assert DEFAULT_POLICY.get("public_purge_enabled") is True
    assert DEFAULT_POLICY.get("public_purge_final_mp4") is True
    assert DEFAULT_POLICY.get("public_purge_keep_live_pool_finals") is True
    assert DEFAULT_POLICY.get("public_purge_keep_all_public_finals") is False
    assert DEFAULT_POLICY.get("protect_live_vods") is True


def test_always_prune_xfade_when_final_exists(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    jobs = root / "output" / "jobs"
    job = jobs / "job_done"
    video = job / "video"
    xfade = video / "_xfade_tmp"
    xfade.mkdir(parents=True)
    (xfade / "xf_0001.mp4").write_bytes(b"x" * 4096)
    (video / "final.mp4").write_bytes(b"final")

    farming = jobs / "job_farming"
    fx = farming / "video" / "_xfade_tmp"
    fx.mkdir(parents=True)
    (fx / "xf_0001.mp4").write_bytes(b"y" * 2048)
    # No final.mp4 on farming job

    ops = root / "output" / "ops"
    ops.mkdir(parents=True)
    (ops / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "id": "done",
                    "status": "private",
                    "job_dir": str(job),
                },
                {
                    "id": "farm",
                    "status": "farming",
                    "job_dir": str(farming),
                },
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(dg, "ROOT", root)
    monkeypatch.setattr(dg, "JOBS_DIR", jobs)
    monkeypatch.setattr(dg, "OPS_DIR", ops)
    monkeypatch.setattr(dg, "JOBS_JSON", ops / "jobs.json")
    monkeypatch.setattr(dg, "_active_farm_out_dirs", lambda: set())

    out = always_prune_xfade_tmps(dry_run=False, policy=dict(DEFAULT_POLICY))
    assert out["deleted_count"] >= 1
    assert not xfade.exists()
    assert (video / "final.mp4").is_file()
    assert fx.exists(), "farming xfade must survive (no final + protect status)"


def test_always_prune_skips_without_final(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    jobs = root / "output" / "jobs"
    job = jobs / "mid_compose"
    xfade = job / "video" / "_xfade_tmp"
    xfade.mkdir(parents=True)
    (xfade / "xf_0001.mp4").write_bytes(b"z" * 1024)
    ops = root / "output" / "ops"
    ops.mkdir(parents=True)
    (ops / "jobs.json").write_text("[]", encoding="utf-8")

    monkeypatch.setattr(dg, "ROOT", root)
    monkeypatch.setattr(dg, "JOBS_DIR", jobs)
    monkeypatch.setattr(dg, "OPS_DIR", ops)
    monkeypatch.setattr(dg, "JOBS_JSON", ops / "jobs.json")
    monkeypatch.setattr(dg, "_active_farm_out_dirs", lambda: set())

    out = always_prune_xfade_tmps(dry_run=False, policy=dict(DEFAULT_POLICY))
    assert out["deleted_count"] == 0
    assert xfade.exists()


def test_cleanup_public_purges_farm_final_by_default(
    tmp_path: Path, monkeypatch
) -> None:
    """Post-public purge drops farm finals; Live library is the durable copy."""
    root = tmp_path / "repo"
    jobs = root / "output" / "jobs"
    job = jobs / "pub_live"
    (job / "video").mkdir(parents=True)
    (job / "video" / "_xfade_tmp").mkdir(parents=True)
    (job / "video" / "scenes").mkdir(parents=True)
    (job / "video" / "final.mp4").write_bytes(b"f" * 4096)
    (job / "video" / "_xfade_tmp" / "xf.mp4").write_bytes(b"x" * 4096)
    (job / "video" / "scenes" / "scene_001.mp4").write_bytes(b"s" * 2048)
    (job / "publish_manifest.json").write_text("{}", encoding="utf-8")

    ops = root / "output" / "ops"
    ops.mkdir(parents=True)
    (ops / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "id": "pub",
                    "status": "public",
                    "video_id": "abc123XYZ01",
                    "job_dir": str(job),
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(dg, "ROOT", root)
    monkeypatch.setattr(dg, "JOBS_DIR", jobs)
    monkeypatch.setattr(dg, "OPS_DIR", ops)
    monkeypatch.setattr(dg, "JOBS_JSON", ops / "jobs.json")
    monkeypatch.setattr(dg, "LIVE_VODS_DIR", root / "output" / "live_vods")
    monkeypatch.setattr(dg, "_active_farm_out_dirs", lambda: set())
    monkeypatch.setattr(dg, "_live_force_lock_paths", lambda: set())
    monkeypatch.setattr(dg, "_playlist_final_paths", lambda: set())
    monkeypatch.setattr(dg, "_top_public_final_paths", lambda top_n=8: set())
    monkeypatch.setattr(dg, "_public_job_final_paths", lambda: set())
    monkeypatch.setattr(dg, "_live_vods_final_paths", lambda: set())

    out = cleanup_public_job_media(
        job,
        dry_run=False,
        policy=dict(DEFAULT_POLICY),
        require_public_status=True,
        job_status="public",
    )
    assert out.get("ok") is True
    assert not (job / "video" / "final.mp4").exists(), "farm final purged by default"
    assert not (job / "video" / "_xfade_tmp").exists()
    assert not (job / "video" / "scenes").exists()
    assert (job / "publish_manifest.json").is_file()


def test_cleanup_public_keeps_final_when_on_live_playlist(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    jobs = root / "output" / "jobs"
    job = jobs / "pub_live"
    (job / "video").mkdir(parents=True)
    (job / "video" / "_xfade_tmp").mkdir(parents=True)
    (job / "video" / "scenes").mkdir(parents=True)
    final = job / "video" / "final.mp4"
    final.write_bytes(b"f" * 4096)
    (job / "video" / "_xfade_tmp" / "xf.mp4").write_bytes(b"x" * 4096)
    (job / "video" / "scenes" / "scene_001.mp4").write_bytes(b"s" * 2048)
    (job / "publish_manifest.json").write_text("{}", encoding="utf-8")

    ops = root / "output" / "ops"
    ops.mkdir(parents=True)
    monkeypatch.setattr(dg, "ROOT", root)
    monkeypatch.setattr(dg, "JOBS_DIR", jobs)
    monkeypatch.setattr(dg, "OPS_DIR", ops)
    monkeypatch.setattr(dg, "JOBS_JSON", ops / "jobs.json")
    monkeypatch.setattr(dg, "_active_farm_out_dirs", lambda: set())
    monkeypatch.setattr(dg, "_live_force_lock_paths", lambda: set())
    monkeypatch.setattr(
        dg, "_live_protect_final_paths", lambda policy=None: {final.resolve()}
    )

    out = cleanup_public_job_media(
        job,
        dry_run=False,
        policy=dict(DEFAULT_POLICY),
        require_public_status=True,
        job_status="public",
    )
    assert out.get("ok") is True
    assert final.exists(), "playlist-protected Live head kept"
    assert not (job / "video" / "_xfade_tmp").exists()
    assert not (job / "video" / "scenes").exists()
    assert (job / "publish_manifest.json").is_file()


def test_cleanup_public_can_drop_final_when_live_keep_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    jobs = root / "output" / "jobs"
    job = jobs / "pub_old"
    (job / "video").mkdir(parents=True)
    (job / "script").mkdir(parents=True)
    (job / "ops").mkdir(parents=True)
    (job / "audio").mkdir(parents=True)
    (job / "images").mkdir(parents=True)
    (job / "youtube_meta").mkdir(parents=True)
    (job / "video" / "_xfade_tmp").mkdir(parents=True)
    (job / "video" / "scenes").mkdir(parents=True)

    (job / "publish_manifest.json").write_text("{}", encoding="utf-8")
    (job / "script" / "script.json").write_text('{"ok":1}', encoding="utf-8")
    (job / "ops" / "SOP_COMPLIANCE.md").write_text("# sop\n", encoding="utf-8")
    (job / "audio" / "voice_manifest.json").write_text("{}", encoding="utf-8")
    (job / "audio" / "scene_001.wav").write_bytes(b"w" * 2048)
    (job / "images" / "scene_001.jpg").write_bytes(b"j" * 2048)
    (job / "video" / "final.mp4").write_bytes(b"f" * 4096)
    (job / "video" / "_xfade_tmp" / "xf.mp4").write_bytes(b"x" * 4096)
    (job / "video" / "scenes" / "scene_001.mp4").write_bytes(b"s" * 2048)
    (job / "youtube_meta" / "title.txt").write_text("t", encoding="utf-8")

    ops = root / "output" / "ops"
    ops.mkdir(parents=True)
    monkeypatch.setattr(dg, "ROOT", root)
    monkeypatch.setattr(dg, "JOBS_DIR", jobs)
    monkeypatch.setattr(dg, "OPS_DIR", ops)
    monkeypatch.setattr(dg, "JOBS_JSON", ops / "jobs.json")
    monkeypatch.setattr(dg, "_active_farm_out_dirs", lambda: set())
    monkeypatch.setattr(dg, "_live_force_lock_paths", lambda: set())
    monkeypatch.setattr(dg, "_live_protect_final_paths", lambda policy=None: set())

    policy = dict(DEFAULT_POLICY)
    policy["public_purge_keep_live_pool_finals"] = False
    policy["public_purge_keep_all_public_finals"] = False
    out = cleanup_public_job_media(
        job,
        dry_run=False,
        policy=policy,
        require_public_status=True,
        job_status="public",
    )
    assert out.get("ok") is True
    assert out.get("bytes_freed", 0) > 0
    assert (job / "publish_manifest.json").is_file()
    assert (job / "script" / "script.json").is_file()
    assert (job / "ops" / "SOP_COMPLIANCE.md").is_file()
    assert (job / "audio" / "voice_manifest.json").is_file()
    assert (job / "youtube_meta" / "title.txt").is_file()
    assert not (job / "video" / "final.mp4").exists()
    assert not (job / "video" / "_xfade_tmp").exists()
    assert not (job / "video" / "scenes").exists()
    assert not (job / "audio" / "scene_001.wav").exists()
    assert not (job / "images" / "scene_001.jpg").exists()


def test_cleanup_public_skips_non_public(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    (job / "video").mkdir()
    (job / "video" / "final.mp4").write_bytes(b"f")
    out = cleanup_public_job_media(
        job,
        dry_run=False,
        policy=dict(DEFAULT_POLICY),
        require_public_status=True,
        job_status="private",
    )
    assert out.get("skipped") is True
    assert (job / "video" / "final.mp4").exists()


def test_cleanup_public_preserves_live_force_lock_final(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    jobs = root / "output" / "jobs"
    job = jobs / "live_pub"
    video = job / "video"
    video.mkdir(parents=True)
    final = video / "final.mp4"
    final.write_bytes(b"livehead" * 100)
    other = video / "extra.mp4"
    other.write_bytes(b"extra" * 100)

    ops = root / "output" / "ops"
    ops.mkdir(parents=True)
    monkeypatch.setattr(dg, "ROOT", root)
    monkeypatch.setattr(dg, "JOBS_DIR", jobs)
    monkeypatch.setattr(dg, "OPS_DIR", ops)
    monkeypatch.setattr(dg, "JOBS_JSON", ops / "jobs.json")
    monkeypatch.setattr(dg, "_active_farm_out_dirs", lambda: set())
    monkeypatch.setattr(dg, "_live_force_lock_paths", lambda: {final.resolve()})
    monkeypatch.setattr(
        dg, "_live_protect_final_paths", lambda policy=None: {final.resolve()}
    )

    policy = dict(DEFAULT_POLICY)
    policy["public_purge_keep_all_public_finals"] = False
    out = cleanup_public_job_media(
        job,
        dry_run=False,
        policy=policy,
        require_public_status=True,
        job_status="public",
    )
    assert out.get("ok") is True
    assert final.exists(), "force-locked Live head must survive"
    assert not other.exists()
