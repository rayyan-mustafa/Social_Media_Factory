"""Tests for Live broadcast packaging reuse from featured VOD meta."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.streaming import live_broadcast_meta as lbm


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_job_dir_from_media_path(tmp_path: Path) -> None:
    job = tmp_path / "jobs" / "20260801T000000Z_Demo_Title"
    media = job / "video" / "final.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"x")
    assert lbm.job_dir_from_media_path(media) == job
    assert lbm.job_dir_from_media_path(None) is None
    assert lbm.job_dir_from_media_path("/tmp/nope.mp4") is None


def test_select_featured_vod_meta_prefers_youtube_meta(tmp_path: Path) -> None:
    job = tmp_path / "jobs" / "20260801T000000Z_Anne_Demo"
    media = job / "video" / "final.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"fake")
    meta = job / "youtube_meta"
    _write(meta / "title.txt", "What If Anne Survived?")
    _write(meta / "description.txt", "Full desc from pack.\n\nChapters…")
    # Tiny file should be rejected; write a >=1KB thumb.
    (meta / "thumbnail.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 2048)
    _write(
        job / "publish_manifest.json",
        json.dumps(
            {
                "title": "Publish title should lose",
                "description": "Publish desc should lose",
                "video_id": "abc123XYZ01",
            }
        ),
    )

    out = lbm.select_featured_vod_meta(media)
    assert out["ok"] is True
    assert out["title"] == "What If Anne Survived?"
    assert out["title_src"] == "youtube_meta"
    assert out["description_src"] == "youtube_meta"
    assert "Full desc" in (out["description"] or "")
    assert out["source_video_id"] == "abc123XYZ01"
    assert out["thumbnail_src"] == "youtube_meta"
    assert Path(out["thumbnail_path"]).is_file()


def test_select_featured_vod_meta_falls_back_pipeline_no_thumb(
    tmp_path: Path,
) -> None:
    job = tmp_path / "jobs" / "20260801T000000Z_Catherine_Haunt"
    media = job / "video_live_head" / "final.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"fake")
    _write(
        job / "pipeline_manifest.json",
        json.dumps(
            {
                "title": "The Unquiet Queen: Why Catherine Haunts",
                "topic": "Why Catherine of Aragon Still Haunts",
                "video_id": None,
            }
        ),
    )
    # Scene stills must NOT be used as Live thumbs.
    imgs = job / "images"
    imgs.mkdir()
    (imgs / "scene_001.jpg").write_bytes(b"\xff\xd8\xff" + b"1" * 2048)

    out = lbm.select_featured_vod_meta(media)
    assert out["ok"] is True
    assert out["title"].startswith("The Unquiet Queen")
    assert out["title_src"] == "pipeline_manifest"
    assert out["description_src"] == "pipeline_topic"
    assert out["thumbnail_path"] is None


def test_select_featured_vod_meta_youtube_fetch_and_publish(
    tmp_path: Path,
) -> None:
    job = tmp_path / "jobs" / "20260801T000000Z_Pub_Only"
    media = job / "video" / "final.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"fake")
    thumbs = job / "thumbnails"
    thumbs.mkdir()
    (thumbs / "selected.jpg").write_bytes(b"\xff\xd8\xff" + b"2" * 2048)
    _write(
        job / "publish_manifest.json",
        json.dumps({"title": "From publish", "description": "", "video_id": "vid99"}),
    )

    out = lbm.select_featured_vod_meta(
        media,
        youtube_fetch={
            "title": "YT title unused",
            "description": "Description from source VOD on YouTube",
        },
    )
    assert out["title"] == "From publish"
    assert out["title_src"] == "publish_manifest"
    assert out["description_src"] == "youtube_source_video"
    assert out["thumbnail_src"] == "local_thumbnails"
    assert "selected.jpg" in (out["thumbnail_path"] or "")


def test_idempotent_skip_same_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lbm, "OPS", tmp_path)
    monkeypatch.setattr(lbm, "STATE_PATH", tmp_path / "live_broadcast_meta.json")
    monkeypatch.setattr(lbm, "_cooldown_sec", lambda: 600.0)

    feat = "/jobs/demo/video/final.mp4"
    meta = {
        "title": "T",
        "description": "D",
        "thumbnail_path": "/tmp/t.jpg",
    }
    state = {
        "channels": {
            "napstorian": {
                "featured_path": feat,
                "title": "T",
                "description_hash": lbm._sha1_text("D"),
                "thumbnail_path": "/tmp/t.jpg",
                "ok": True,
                "applied_at": "2099-01-01T00:00:00+00:00",
            }
        }
    }
    skip, why = lbm._should_skip_idempotent(
        "napstorian",
        featured_path=feat,
        meta=meta,
        force=False,
        state=state,
    )
    assert skip is True
    assert "same_pack" in why or "cooldown" in why

    skip2, _ = lbm._should_skip_idempotent(
        "napstorian",
        featured_path=feat,
        meta=meta,
        force=True,
        state=state,
    )
    assert skip2 is False


def test_sync_channel_live_meta_applies_with_mocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = tmp_path / "jobs" / "20260801T000000Z_Apply_Me"
    media = job / "video" / "final.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"fake")
    meta_dir = job / "youtube_meta"
    _write(meta_dir / "title.txt", "Live Reuse Title")
    _write(meta_dir / "description.txt", "Live reuse description body")
    (meta_dir / "thumbnail.jpg").write_bytes(b"\xff\xd8\xff" + b"9" * 2048)

    monkeypatch.setattr(lbm, "OPS", tmp_path / "ops")
    monkeypatch.setattr(lbm, "STATE_PATH", tmp_path / "ops" / "live_broadcast_meta.json")
    monkeypatch.setattr(lbm, "BROADCAST_IDS_PATH", tmp_path / "ops" / "live_broadcast_ids.json")
    (tmp_path / "ops").mkdir(parents=True)

    youtube = MagicMock()
    # liveBroadcasts.list for resolve
    youtube.liveBroadcasts().list().execute.return_value = {
        "items": [
            {
                "id": "bcastLIVE01",
                "snippet": {
                    "title": "old",
                    "description": "old",
                    "scheduledStartTime": "2026-08-01T00:00:00.000Z",
                },
                "status": {"lifeCycleStatus": "live"},
                "contentDetails": {"boundStreamId": "stream1"},
            }
        ]
    }
    youtube.videos().list().execute.return_value = {
        "items": [
            {
                "id": "bcastLIVE01",
                "snippet": {
                    "title": "old",
                    "description": "old",
                    "categoryId": "27",
                },
            }
        ]
    }
    youtube.videos().update().execute.return_value = {"id": "bcastLIVE01"}
    youtube.liveBroadcasts().update().execute.return_value = {"id": "bcastLIVE01"}
    youtube.thumbnails().set().execute.return_value = {}

    monkeypatch.setattr(lbm, "_build_youtube", lambda ch: youtube)
    monkeypatch.setattr(lbm, "_log_ops", lambda *a, **k: None)

    out = lbm.sync_channel_live_meta(
        "napstorian",
        featured_path=str(media),
        force=True,
        dry_run=False,
        allow_yt_thumb_download=False,
    )
    assert out["ok"] is True
    assert out["action"] == "applied"
    assert out["broadcast_id"] == "bcastLIVE01"
    assert out["meta"]["title"] == "Live Reuse Title"
    assert out["apply"]["snippet_via"] == "videos.update"
    assert out["apply"]["thumbnail_uploaded"] is True

    # Second call without force should skip (idempotent).
    out2 = lbm.sync_channel_live_meta(
        "napstorian",
        featured_path=str(media),
        force=False,
        dry_run=False,
    )
    assert out2["action"] == "skipped_idempotent"


def test_resolve_active_broadcast_prefers_cached_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never combine mine+broadcastStatus; prefer ops-cached broadcast id."""
    youtube = MagicMock()
    cached_item = {
        "id": "cachedBcast01",
        "snippet": {"title": "Cached Live", "description": "d"},
        "status": {"lifeCycleStatus": "live"},
        "contentDetails": {"boundStreamId": "s1"},
    }

    def _list(**kwargs):
        # Fail loudly if incompatible combo is requested.
        assert not (
            kwargs.get("mine") is True and kwargs.get("broadcastStatus") is not None
        ), "mine+broadcastStatus must not be combined"
        m = MagicMock()
        if kwargs.get("id") == "cachedBcast01":
            m.execute.return_value = {"items": [cached_item]}
        elif kwargs.get("broadcastStatus"):
            m.execute.return_value = {"items": []}
        elif kwargs.get("mine") is True:
            m.execute.return_value = {"items": []}
        else:
            m.execute.return_value = {"items": []}
        return m

    youtube.liveBroadcasts().list.side_effect = _list
    monkeypatch.setattr(
        lbm, "load_broadcast_ids", lambda: {"napstorian": "cachedBcast01"}
    )
    monkeypatch.setattr(lbm, "remember_broadcast_id", lambda *a, **k: None)

    out = lbm.resolve_active_broadcast_id(youtube, "napstorian")
    assert out["ok"] is True
    assert out["broadcast_id"] == "cachedBcast01"
    assert out["source"] == "ops_cache_validated"
    assert any(t.startswith("id=") for t in out.get("tried") or [])


def test_sync_all_dry_run_both_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_sync(ch, **kwargs):
        calls.append(ch)
        return {"ok": True, "channel": ch, "action": "dry_run"}

    monkeypatch.setattr(lbm, "sync_channel_live_meta", fake_sync)
    out = lbm.sync_all_live_meta(
        dry_run=True, channels=("napstorian", "napping_historian")
    )
    assert out["ok"] is True
    assert calls == ["napstorian", "napping_historian"]
