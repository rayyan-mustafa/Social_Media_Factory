"""Live VOD library path layout, ranking→download handoff, disk_guard exempt."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.runpod import disk_guard as dg
from src.streaming import live_vod_library as lvl
from src.streaming import vod_picker as vp


def test_live_vod_path_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lvl, "LIVE_VODS_ROOT", tmp_path / "live_vods")
    monkeypatch.setattr(lvl, "ROOT", tmp_path)
    ch, vid = "napstorian", "AbCdEfGhIjK"
    final = lvl.live_vod_final_path(ch, vid)
    assert final.as_posix().endswith(f"live_vods/{ch}/{vid}/final.mp4")
    assert lvl.parse_live_vod_path(final) == (ch, vid)
    assert lvl.is_live_vods_path(final) is True
    assert lvl.is_live_vods_path(tmp_path / "jobs" / "x" / "video" / "final.mp4") is False


def test_library_final_ok_idempotent_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "live_vods"
    monkeypatch.setattr(lvl, "LIVE_VODS_ROOT", root)
    monkeypatch.setattr(lvl, "min_library_bytes", lambda: 1000)
    dest = lvl.live_vod_final_path("napstorian", "vidAAA11111")
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"x" * 2000)
    assert lvl.library_final_ok(dest, min_bytes=1000)

    def mock_download(channel, video_id, **kwargs):
        path = lvl.live_vod_final_path(channel, video_id)
        if lvl.library_final_ok(path, min_bytes=1000):
            return {
                "ok": True,
                "action": "skipped_exists",
                "path": str(path),
                "channel": channel,
                "video_id": video_id,
            }
        return {
            "ok": False,
            "action": "failed",
            "channel": channel,
            "video_id": video_id,
        }

    monkeypatch.setattr(
        lvl,
        "rank_top_publics_for_library",
        lambda channel, limit=None, candidate_scan=None: [
            {
                "video_id": "vidAAA11111",
                "channel": "napstorian",
                "views": 900,
                "avd_pct": 40.0,
                "rank_metric": 360.0,
                "title": "Existing Hit",
                "library_path": str(dest),
                "library_ok": True,
            }
        ],
    )
    ensured = lvl.ensure_channel_library(
        "napstorian", top_n=1, download_fn=mock_download
    )
    assert ensured["ok"] is True
    assert ensured["ensured"][0]["action"] == "skipped_exists"
    assert ensured["featured"]["video_id"] == "vidAAA11111"


def test_rank_to_download_handoff_mocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lvl, "LIVE_VODS_ROOT", tmp_path / "live_vods")
    monkeypatch.setattr(
        lvl,
        "rank_top_publics_for_library",
        lambda channel, limit=None, candidate_scan=None: [
            {
                "video_id": "shortVid0001",
                "channel": channel,
                "views": 5000,
                "rank_metric": 5000.0,
                "title": "Short Hot Take #history",
                "library_ok": False,
                "library_path": str(
                    lvl.live_vod_final_path(channel, "shortVid0001")
                ),
            },
            {
                "video_id": "longVid00001",
                "channel": channel,
                "views": 1200,
                "rank_metric": 800.0,
                "title": "What If Anne Survived Henry",
                "library_ok": False,
                "library_path": str(
                    lvl.live_vod_final_path(channel, "longVid00001")
                ),
            },
            {
                "video_id": "backupVid001",
                "channel": channel,
                "views": 900,
                "rank_metric": 700.0,
                "title": "Catherine Haunts Tudor Court",
                "library_ok": False,
                "library_path": str(
                    lvl.live_vod_final_path(channel, "backupVid001")
                ),
            },
        ],
    )

    def mock_download(channel, video_id, **kwargs):
        if video_id == "shortVid0001":
            return {
                "ok": False,
                "action": "skipped_short",
                "channel": channel,
                "video_id": video_id,
                "duration_sec": 45.0,
            }
        path = lvl.live_vod_final_path(channel, video_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"v" * 3000)
        lvl.write_library_meta(channel, video_id, title=kwargs.get("title") or "")
        return {
            "ok": True,
            "action": "downloaded",
            "channel": channel,
            "video_id": video_id,
            "path": str(path),
            "duration_sec": 900.0,
            "size_bytes": 3000,
            "title": kwargs.get("title") or "",
        }

    out = lvl.ensure_channel_library(
        "napstorian", top_n=2, download_fn=mock_download, sleep_between_sec=0
    )
    assert out["ok"] is True
    assert out["filled"] == 2
    assert out["featured"]["video_id"] == "longVid00001"
    ids = [e["video_id"] for e in out["ensured"]]
    assert ids == ["longVid00001", "backupVid001"]
    assert out["skipped_short"][0]["video_id"] == "shortVid0001"
    assert lvl.library_final_ok(lvl.live_vod_final_path("napstorian", "longVid00001"), min_bytes=1000)


def test_refresh_playlists_calls_library_then_ranks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Handoff: refresh ensures library before writing playlist heads."""
    root = tmp_path
    live = root / "output" / "live_vods" / "napstorian" / "pubTopAAAAA"
    final = live / "final.mp4"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"z" * 3_000_000)
    (live / "publish_manifest.json").write_text(
        json.dumps(
            {
                "video_id": "pubTopAAAAA",
                "channel": "napstorian",
                "title": "Top Public Longform",
            }
        ),
        encoding="utf-8",
    )
    stream = root / "config" / "streaming"
    stream.mkdir(parents=True)
    ops = root / "output" / "ops"
    ops.mkdir(parents=True)

    monkeypatch.setattr(vp, "ROOT", root)
    monkeypatch.setattr(vp, "JOBS", root / "output" / "jobs")
    monkeypatch.setattr(vp, "LIVE_VODS", root / "output" / "live_vods")
    monkeypatch.setattr(vp, "OPS", ops)
    monkeypatch.setattr(vp, "STREAM_CFG", stream)
    monkeypatch.setattr(vp, "PICKER_STATUS", ops / "vod_picker_status.json")
    monkeypatch.setattr(vp, "playlist_force_lock_path", lambda: ops / "no_lock.json")
    monkeypatch.setattr(vp, "read_playlist_force_lock", lambda: None)
    monkeypatch.setattr(vp, "_load_views_cache", lambda: {
        "by_video_id": {"pubTopAAAAA": 2200},
        "titles_by_id": {"pubTopAAAAA": "Top Public Longform"},
        "channel_by_video_id": {"pubTopAAAAA": "napstorian"},
    })
    monkeypatch.setattr(vp, "_load_avd_by_video_id", lambda: {})
    monkeypatch.setattr(vp, "_load_ops_job_privacy", lambda: {
        "pubTopAAAAA": {"status": "public", "channel": "napstorian", "title": "Top Public Longform"},
    })
    monkeypatch.setattr(vp, "_load_ops_job_channels", lambda: {})
    monkeypatch.setattr(vp, "_load_benchmark_titles", lambda _ch: [])
    monkeypatch.setattr(vp, "_load_smm_boost_tokens", lambda: set())
    monkeypatch.setattr(vp, "_load_winner_views_by_title", lambda _ch=None: [])
    monkeypatch.setattr(vp, "_ffprobe_duration", lambda _p: 800.0)

    lib_calls: list[dict] = []

    def fake_ensure(**kwargs):
        lib_calls.append(dict(kwargs))
        return {"ok": True, "module": "live_vod_library", "channels": {"napstorian": {"ok": True}}}

    monkeypatch.setattr(
        "src.streaming.live_vod_library.ensure_all_channel_libraries",
        fake_ensure,
    )
    monkeypatch.setattr(
        "src.streaming.live_vod_library.library_enabled",
        lambda: True,
    )
    monkeypatch.setenv("LIVE_VOD_LIBRARY", "1")
    monkeypatch.setenv("LIVE_VOD_LIBRARY_IN_TESTS", "1")

    payload = vp.refresh_playlists(channels=["napstorian"], limit=3, dry_run=False)
    assert lib_calls, "library ensure must run before playlist write"
    assert payload.get("ok") is True
    assert payload.get("live_vod_library", {}).get("ok") is True
    entries = (payload.get("channels") or {}).get("napstorian", {}).get("entries") or []
    assert entries
    assert "live_vods" in entries[0]
    assert "pubTopAAAAA" in entries[0]


def test_disk_guard_exempts_live_vods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    live = root / "output" / "live_vods" / "napstorian" / "keepMeVid001"
    live.mkdir(parents=True)
    final = live / "final.mp4"
    final.write_bytes(b"KEEP" * 1000)
    meta = live / "meta.json"
    meta.write_text("{}", encoding="utf-8")

    jobs = root / "output" / "jobs"
    job = jobs / "pub_job"
    video = job / "video"
    video.mkdir(parents=True)
    (video / "final.mp4").write_bytes(b"purge_me" * 200)
    (job / "publish_manifest.json").write_text("{}", encoding="utf-8")

    ops = root / "output" / "ops"
    ops.mkdir(parents=True)

    monkeypatch.setattr(dg, "ROOT", root)
    monkeypatch.setattr(dg, "JOBS_DIR", jobs)
    monkeypatch.setattr(dg, "LIVE_VODS_DIR", root / "output" / "live_vods")
    monkeypatch.setattr(dg, "OPS_DIR", ops)
    monkeypatch.setattr(dg, "JOBS_JSON", ops / "jobs.json")
    monkeypatch.setattr(dg, "_active_farm_out_dirs", lambda: set())
    monkeypatch.setattr(dg, "_live_force_lock_paths", lambda: set())
    monkeypatch.setattr(dg, "_playlist_final_paths", lambda: set())
    monkeypatch.setattr(dg, "_top_public_final_paths", lambda top_n=8: set())
    monkeypatch.setattr(dg, "_public_job_final_paths", lambda: set())

    assert dg._is_factory_path(final) is True  # refuse deletes
    assert dg._is_live_vods_path(final) is True
    freed = dg._safe_unlink(final, dry_run=False)
    assert freed == 0
    assert final.is_file()

    protect = dg._live_protect_final_paths(dict(dg.DEFAULT_POLICY))
    assert final.resolve() in protect

    policy = dict(dg.DEFAULT_POLICY)
    policy["public_purge_keep_all_public_finals"] = False
    policy["public_purge_keep_live_pool_finals"] = False
    out = dg.cleanup_public_job_media(
        job,
        dry_run=False,
        policy=policy,
        require_public_status=True,
        job_status="public",
    )
    assert out.get("ok") is True
    assert not (video / "final.mp4").exists()  # farm purge still works
    assert final.is_file()  # live library untouched


def test_live_broadcast_meta_resolves_live_vods(tmp_path: Path) -> None:
    from src.streaming import live_broadcast_meta as lbm

    live = tmp_path / "live_vods" / "napping_historian" / "histVid0001"
    media = live / "final.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"x")
    (live / "publish_manifest.json").write_text(
        json.dumps(
            {
                "video_id": "histVid0001",
                "channel": "napping_historian",
                "title": "Why Catherine Haunts",
                "description": "Full haunted court story.",
            }
        ),
        encoding="utf-8",
    )
    assert lbm.job_dir_from_media_path(media) == live
    meta = lbm.select_featured_vod_meta(media)
    assert meta["ok"] is True
    assert meta["source_video_id"] == "histVid0001"
    assert meta["title"] == "Why Catherine Haunts"


def test_public_missing_respects_live_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_final = tmp_path / "live_vods" / "napstorian" / "hasLibVid" / "final.mp4"
    live_final.parent.mkdir(parents=True)
    live_final.write_bytes(b"x" * 100)

    monkeypatch.setattr(
        vp,
        "_load_views_cache",
        lambda: {
            "by_video_id": {"orphanPub": 1200, "hasLibVid": 50},
            "titles_by_id": {"orphanPub": "Orphan", "hasLibVid": "In Library"},
            "channel_by_video_id": {
                "orphanPub": "napstorian",
                "hasLibVid": "napstorian",
            },
        },
    )
    monkeypatch.setattr(
        vp,
        "_load_publish_index",
        lambda: {
            str(live_final.resolve()): {
                "video_id": "hasLibVid",
                "channel": "napstorian",
                "title": "In Library",
            }
        },
    )
    missing = vp.public_videos_missing_local_final("napstorian")
    ids = {r["video_id"] for r in missing}
    assert "orphanPub" in ids
    assert "hasLibVid" not in ids
