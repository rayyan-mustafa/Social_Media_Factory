"""RAM guard threshold helpers + protect markers."""

from __future__ import annotations

from src.runpod.ram_guard import (
    _ffmpeg_protected,
    is_ram_healthy_target,
    is_ram_unhealthy,
)


def test_is_ram_unhealthy_by_gb() -> None:
    pol = {"trigger_available_gb": 1.25, "trigger_available_percent": 5.0}
    bad, reason = is_ram_unhealthy(
        {"ok": True, "available_gb": 0.8, "available_percent": 20.0}, pol
    )
    assert bad is True
    assert "available_gb" in reason


def test_is_ram_unhealthy_by_percent() -> None:
    pol = {"trigger_available_gb": 0.1, "trigger_available_percent": 12.0}
    bad, reason = is_ram_unhealthy(
        {"ok": True, "available_gb": 2.0, "available_percent": 10.0}, pol
    )
    assert bad is True
    assert "available_pct" in reason


def test_is_ram_healthy_when_above_both() -> None:
    pol = {"trigger_available_gb": 1.25, "trigger_available_percent": 12.0}
    bad, _ = is_ram_unhealthy(
        {"ok": True, "available_gb": 3.0, "available_percent": 40.0}, pol
    )
    assert bad is False


def test_is_ram_healthy_target() -> None:
    pol = {"target_available_gb": 2.0, "target_available_percent": 20.0}
    assert (
        is_ram_healthy_target(
            {"ok": True, "available_gb": 2.5, "available_percent": 10.0}, pol
        )
        is True
    )
    assert (
        is_ram_healthy_target(
            {"ok": True, "available_gb": 0.5, "available_percent": 25.0}, pol
        )
        is True
    )
    assert (
        is_ram_healthy_target(
            {"ok": True, "available_gb": 0.5, "available_percent": 10.0}, pol
        )
        is False
    )


def test_live_rtmp_ffmpeg_protected() -> None:
    ok, why = _ffmpeg_protected(
        "ffmpeg -re -i final.mp4 -f flv rtmp://a.rtmp.youtube.com/live2/KEY"
    )
    assert ok is True
    assert why.startswith("live:")


def test_farm_xfade_ffmpeg_protected() -> None:
    ok, why = _ffmpeg_protected(
        "ffmpeg -i xf_0230.mp4 -i scene_231.mp4 -filter_complex [0:v][1:v]xfade=transition=fade"
    )
    assert ok is True
    assert why.startswith("farm:")


def test_unrelated_ffmpeg_not_protected() -> None:
    ok, why = _ffmpeg_protected("ffmpeg -i /tmp/scratch.wav -f null -")
    assert ok is False
    assert why == ""
