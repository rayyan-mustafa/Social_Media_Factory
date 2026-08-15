"""Unit tests for industrial VOD-loop helpers (backoff, stall, env knobs).

No live FFmpeg / RTMP / subprocess required. Never touches gpu_lock / RunPod.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.streaming.vod_loop import (
    CHANNELS,
    _DEFAULT_BITRATE_K,
    _build_ffmpeg_cmd,
    _parse_progress_file,
    encode_settings,
    next_backoff_sec,
    parse_bitrate_k,
    stall_from_activity,
)


def test_next_backoff_doubles_until_cap() -> None:
    assert next_backoff_sec(5, base=5, cap=300) == 10
    assert next_backoff_sec(10, base=5, cap=300) == 20
    assert next_backoff_sec(160, base=5, cap=300) == 300
    assert next_backoff_sec(300, base=5, cap=300) == 300
    # Cap below base is raised to base.
    assert next_backoff_sec(5, base=5, cap=3) == 5


def test_next_backoff_never_drops_below_base() -> None:
    assert next_backoff_sec(1, base=5, cap=300) == 10  # clamped up then doubled
    assert next_backoff_sec(0.5, base=5, cap=60) == 10


def test_parse_bitrate_k_variants() -> None:
    assert parse_bitrate_k("3800") == 3800
    assert parse_bitrate_k("3800k") == 3800
    assert parse_bitrate_k("2500K") == 2500
    assert parse_bitrate_k("3.8M") == 3800
    assert parse_bitrate_k("4Mbps") == 4000
    assert parse_bitrate_k("") == _DEFAULT_BITRATE_K
    assert parse_bitrate_k(None) == _DEFAULT_BITRATE_K
    assert parse_bitrate_k("bogus") == _DEFAULT_BITRATE_K
    assert parse_bitrate_k("100") == 500  # floor


def test_encode_settings_bitrate_alias_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOD_LOOP_ADAPTIVE", "0")
    monkeypatch.delenv("VOD_LOOP_BITRATE_K", raising=False)
    monkeypatch.setenv("VOD_LOOP_BITRATE", "3500k")
    enc = encode_settings()
    assert enc["bitrate_k"] == 3500
    assert enc["bitrate"] == "3500k"
    assert enc["source"] == "env"
    assert enc["gop"] == enc["fps"] * 2


def test_encode_settings_bitrate_k_wins_over_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOD_LOOP_ADAPTIVE", "0")
    monkeypatch.setenv("VOD_LOOP_BITRATE", "2000")
    monkeypatch.setenv("VOD_LOOP_BITRATE_K", "4000")
    enc = encode_settings()
    assert enc["bitrate_k"] == 4000


def test_encode_settings_height_fps_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOD_LOOP_ADAPTIVE", "0")
    monkeypatch.setenv("VOD_LOOP_BITRATE_K", "3600")
    monkeypatch.setenv("VOD_LOOP_HEIGHT", "720")
    monkeypatch.setenv("VOD_LOOP_FPS", "30")
    monkeypatch.setenv("VOD_LOOP_PRESET", "ultrafast")
    monkeypatch.setenv("VOD_LOOP_STALL_SEC", "120")
    enc = encode_settings("napstorian")
    assert enc["height"] == 720
    assert enc["fps"] == 30
    assert enc["gop"] == 60
    assert enc["preset"] == "ultrafast"
    assert enc["stall_sec"] == 120
    assert enc["maxrate"] == "3600k"
    assert enc["bufsize"] == "7200k"
    assert enc["benchmarks"]["yt_720p30_kbps"] == 4000
    assert enc["benchmarks"]["our_default_kbps"] == 3800


def test_stall_from_activity_fresh_and_stale() -> None:
    now = 1_700_000_000.0
    stalled, detail = stall_from_activity(
        activity_epoch=now - 10, now=now, stall_sec=180
    )
    assert stalled is False
    assert detail.startswith("fresh_")

    stalled, detail = stall_from_activity(
        activity_epoch=now - 200, now=now, stall_sec=180
    )
    assert stalled is True
    assert "stale_" in detail


def test_stall_awaits_first_progress_then_times_out() -> None:
    now = 1_700_000_000.0
    stalled, detail = stall_from_activity(
        activity_epoch=None,
        now=now,
        stall_sec=180,
        session_started_epoch=now - 30,
    )
    assert stalled is False
    assert detail == "awaiting_first_progress"

    stalled, detail = stall_from_activity(
        activity_epoch=None,
        now=now,
        stall_sec=180,
        session_started_epoch=now - 500,
    )
    assert stalled is True
    assert "no_progress_after_" in detail


def test_parse_progress_file(tmp_path: Path) -> None:
    p = tmp_path / "progress.txt"
    p.write_text(
        "frame=1\nbitrate=N/A\nout_time_ms=0\nprogress=continue\n"
        "frame=120\nbitrate=3850.2kbits/s\nout_time_ms=4000000\nprogress=continue\n",
        encoding="utf-8",
    )
    parsed = _parse_progress_file(p)
    assert parsed["frame"] == 120
    assert parsed["bitrate"] == "3850.2kbits/s"
    assert parsed["out_time_ms"] == 4000000


def test_build_ffmpeg_cmd_has_cbr_and_progress(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VOD_LOOP_ADAPTIVE", "0")
    monkeypatch.setenv("VOD_LOOP_BITRATE_K", "3800")
    monkeypatch.setenv("VOD_LOOP_HEIGHT", "720")
    monkeypatch.setenv("VOD_LOOP_FPS", "30")
    monkeypatch.delenv("VOD_LOOP_INPUT_MODE", raising=False)
    # Point playlist at a fake but existing path via env override.
    pl = tmp_path / "playlist.txt"
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"not-real-media")
    pl.write_text(f"file '{media.as_posix()}'\n", encoding="utf-8")
    monkeypatch.setenv("VOD_LOOP_PLAYLIST_NAPSTORIAN", str(pl))

    progress = tmp_path / "prog"
    cmd = _build_ffmpeg_cmd(
        "napstorian",
        "rtmp://example.invalid/live2/REDACTED",
        progress_file=progress,
    )
    assert "-maxrate" in cmd
    assert "3800k" in cmd
    assert "-progress" in cmd
    assert str(progress) in cmd
    assert "-g" in cmd
    assert "60" in cmd  # 30fps * 2s
    assert "-f" in cmd and "flv" in cmd
    # Default: infinite loop of playlist head (not concat demuxer).
    assert "-stream_loop" in cmd
    assert cmd[cmd.index("-stream_loop") + 1] == "-1"
    assert str(media.resolve()) in cmd
    assert "concat" not in cmd
    # Never embed a real key pattern beyond the redacted dest we passed.
    joined = " ".join(cmd)
    assert "REDACTED" in joined


def test_build_ffmpeg_cmd_concat_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VOD_LOOP_ADAPTIVE", "0")
    monkeypatch.setenv("VOD_LOOP_BITRATE_K", "2500")
    monkeypatch.setenv("VOD_LOOP_INPUT_MODE", "concat")
    pl = tmp_path / "playlist.txt"
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    pl.write_text(f"file '{media.as_posix()}'\n", encoding="utf-8")
    monkeypatch.setenv("VOD_LOOP_PLAYLIST_NAPSTORIAN", str(pl))
    cmd = _build_ffmpeg_cmd("napstorian", "rtmp://example.invalid/live2/REDACTED")
    assert "-stream_loop" in cmd
    assert "concat" in cmd
    assert str(pl) in cmd


def test_channels_isolated_from_gpu_names() -> None:
    assert "napstorian" in CHANNELS
    assert "napping_historian" in CHANNELS
    assert "gpu" not in "".join(CHANNELS).lower()


def test_default_bitrate_in_youtube_band() -> None:
    """Second-brief target: raise toward ~3500–4000k @720p30 (not StreamCast 1500)."""
    assert 3500 <= _DEFAULT_BITRATE_K <= 4000


def test_rtmp_dest_rejects_url_as_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mis-copied KEY=URL must not become live2/rtmp://… double path."""
    from src.streaming.vod_loop import _rtmp_destination, _rtmp_url_and_key

    monkeypatch.setenv("YT_LIVE_RTMP_URL_NAPSTORIAN", "rtmp://a.rtmp.youtube.com/live2")
    monkeypatch.setenv("YT_LIVE_RTMP_KEY_NAPSTORIAN", "rtmp://a.rtmp.youtube.com/live2")
    url, key = _rtmp_url_and_key("napstorian")
    assert url.rstrip("/").endswith("/live2")
    assert key == ""
    assert _rtmp_destination("napstorian") is None


def test_rtmp_dest_parses_full_url_in_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.streaming.vod_loop import _rtmp_destination, _rtmp_url_and_key

    monkeypatch.delenv("YT_LIVE_RTMP_URL_NAPPING_HISTORIAN", raising=False)
    monkeypatch.setenv(
        "YT_LIVE_RTMP_KEY_NAPPING_HISTORIAN",
        "rtmp://a.rtmp.youtube.com/live2/test-stream-key",
    )
    url, key = _rtmp_url_and_key("napping_historian")
    assert url.endswith("/live2")
    assert key == "test-stream-key"
    dest = _rtmp_destination("napping_historian")
    assert dest is not None
    assert dest.endswith("/test-stream-key")
    assert dest.count("rtmp://") == 1


def test_rtmp_dest_normal_join(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.streaming.vod_loop import _rtmp_destination

    monkeypatch.setenv("YT_LIVE_RTMP_URL_NAPSTORIAN", "rtmp://a.rtmp.youtube.com/live2")
    monkeypatch.setenv("YT_LIVE_RTMP_KEY_NAPSTORIAN", "abcd-efgh-ijkl")
    dest = _rtmp_destination("napstorian")
    assert dest == "rtmp://a.rtmp.youtube.com/live2/abcd-efgh-ijkl"


def test_performance_issues_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.streaming import vod_loop

    monkeypatch.setenv("VOD_LOOP_ENABLED", "1")
    monkeypatch.setenv("YT_LIVE_RTMP_URL_NAPSTORIAN", "rtmp://a.rtmp.youtube.com/live2")
    monkeypatch.setenv("YT_LIVE_RTMP_KEY_NAPSTORIAN", "unit-test-key")
    with patch.object(vod_loop, "list_channel_ffmpeg_pids", return_value=[1, 2]):
        with patch.object(vod_loop, "list_channel_supervise_pids", return_value=[9]):
            with patch.object(
                vod_loop,
                "channel_status",
                return_value=vod_loop.ChannelStatus(
                    channel="napstorian",
                    enabled=True,
                    has_rtmp_url=True,
                    has_rtmp_key=True,
                    rtmp_ready=True,
                    playlist="x",
                    playlist_ok=True,
                    playlist_detail="ok",
                    alive=True,
                    supervisor_alive=True,
                ),
            ):
                perf = vod_loop.performance_issues("napstorian")
    assert perf["ffmpeg_count"] == 2
    assert "dual_ingest" in perf["issues"]
    assert perf["needs_repair"] is True


def test_start_channel_never_detaches_when_systemd_owns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabled vod-loop@CHANNEL must not Popen a second supervise."""
    from src.streaming import vod_loop

    monkeypatch.setenv("VOD_LOOP_ENABLED", "1")
    monkeypatch.setenv("YT_LIVE_RTMP_URL_NAPSTORIAN", "rtmp://a.rtmp.youtube.com/live2")
    monkeypatch.setenv("YT_LIVE_RTMP_KEY_NAPSTORIAN", "unit-test-key")
    st = vod_loop.ChannelStatus(
        channel="napstorian",
        enabled=True,
        has_rtmp_url=True,
        has_rtmp_key=True,
        rtmp_ready=True,
        playlist="x",
        playlist_ok=True,
        playlist_detail="ok",
        alive=True,
        supervisor_alive=True,
        pid=111,
        supervisor_pid=100,
    )
    with patch.object(vod_loop, "channel_status", return_value=st):
        with patch.object(vod_loop, "ensure_default_playlists", return_value={}):
            with patch.object(vod_loop, "_systemd_owns_channel", return_value=True):
                with patch.object(vod_loop, "_systemd_unit_active", return_value=True):
                    with patch.object(
                        vod_loop,
                        "ensure_single_ingest",
                        return_value={
                            "ffmpeg_pids": [111],
                            "dual_ingest": False,
                            "killed_ffmpeg": [],
                            "killed_supervise": [],
                        },
                    ) as ens:
                        with patch.object(vod_loop.subprocess, "Popen") as popen:
                            out = vod_loop.start_channel("napstorian", force=False)
    assert out.action == "already_running"
    ens.assert_called_once()
    popen.assert_not_called()


def test_start_channel_systemd_restart_when_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.streaming import vod_loop

    monkeypatch.setenv("VOD_LOOP_ENABLED", "1")
    monkeypatch.setenv("YT_LIVE_RTMP_URL_NAPSTORIAN", "rtmp://a.rtmp.youtube.com/live2")
    monkeypatch.setenv("YT_LIVE_RTMP_KEY_NAPSTORIAN", "unit-test-key")
    st = vod_loop.ChannelStatus(
        channel="napstorian",
        enabled=True,
        has_rtmp_url=True,
        has_rtmp_key=True,
        rtmp_ready=True,
        playlist="x",
        playlist_ok=True,
        playlist_detail="ok",
        alive=False,
        supervisor_alive=False,
    )
    with patch.object(vod_loop, "channel_status", return_value=st):
        with patch.object(vod_loop, "ensure_default_playlists", return_value={}):
            with patch.object(vod_loop, "_systemd_owns_channel", return_value=True):
                with patch.object(vod_loop, "_systemd_unit_active", return_value=False):
                    with patch.object(
                        vod_loop,
                        "_systemctl_run",
                        return_value=type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
                    ) as sc:
                        with patch.object(vod_loop.subprocess, "Popen") as popen:
                            out = vod_loop.start_channel("napstorian", force=False)
    assert out.action == "systemd_started"
    assert sc.call_args[0][0] in {"start", "restart"}
    popen.assert_not_called()


def test_claim_exits_when_living_holder(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.streaming import vod_loop

    with patch.object(vod_loop, "_acquire_supervise_lock", return_value=False):
        with patch.object(vod_loop, "_stale_supervise_lock_pid", return_value=4242):
            with patch.object(vod_loop, "_pid_alive", return_value=True):
                with patch.object(vod_loop, "ensure_single_ingest") as ens:
                    claim = vod_loop.claim_supervisor_singleton("napstorian")
    assert claim["ok"] is False
    assert claim["action"] == "supervise_lock_held"
    assert claim["holder"] == 4242
    ens.assert_not_called()


def test_ensure_single_ingest_keeps_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.streaming import vod_loop

    killed: list[int] = []

    def _fake_kill(pid: int, **_kwargs: object) -> None:
        killed.append(pid)

    with patch.object(vod_loop, "list_channel_ffmpeg_pids", side_effect=[[10, 20], [20], [20]]):
        with patch.object(vod_loop, "list_channel_supervise_pids", side_effect=[[100], [100], [100]]):
            with patch.object(vod_loop, "_read_pid", return_value=20):
                with patch.object(vod_loop, "_read_supervisor_pid", return_value=100):
                    with patch.object(vod_loop, "_systemd_main_pid", return_value=100):
                        with patch.object(vod_loop, "_kill_pid", side_effect=_fake_kill):
                            with patch.object(vod_loop, "_pid_path") as pid_path:
                                pid_path.return_value.write_text = lambda *_a, **_k: None
                                with patch.object(vod_loop, "_supervisor_pid_path") as sp:
                                    sp.return_value.write_text = lambda *_a, **_k: None
                                    out = vod_loop.ensure_single_ingest(
                                        "napstorian", keep_ffmpeg_pid=20, keep_supervise_pid=100
                                    )
    assert out["kept_ffmpeg"] == 20
    assert 10 in killed
    assert 20 not in killed
    assert out["dual_ingest"] is False


def test_list_channel_ffmpeg_pids_ignores_shell_mentions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shell/pgrep cmdlines that quote ffmpeg+playlist must not count as encodes."""
    from src.streaming import vod_loop

    monkeypatch.setattr(vod_loop, "OPS", tmp_path)
    real = "ffmpeg\0-i\0/x/playlist_napstorian.txt\0-progress\0/x/vod_loop_napstorian.progress\0"
    shell = (
        "/bin/bash\0-c\0pgrep -af 'ffmpeg.*playlist_napstorian' "
        "and also playlist_napping_historian\0"
    )

    def fake_iter(*needles: str) -> list[int]:
        # Both mention ffmpeg substr; only pid 1 is real encode.
        return [1, 2]

    def fake_cmd(pid: int) -> str:
        return real if pid == 1 else shell

    with patch.object(vod_loop, "_iter_pids_with_substr", side_effect=fake_iter):
        with patch.object(vod_loop, "_cmdline_of", side_effect=fake_cmd):
            with patch.object(
                vod_loop,
                "_progress_path",
                return_value=tmp_path / "vod_loop_napstorian.progress",
            ):
                pids = vod_loop.list_channel_ffmpeg_pids("napstorian")
    assert pids == [1]
