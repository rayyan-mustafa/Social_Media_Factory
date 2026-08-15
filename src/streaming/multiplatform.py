"""Phase 2a scaffold: optional extra RTMP destinations (Twitch/Kick/FB).

Does not start streams yet. Reads env, reports readiness, enforces encode cap
documented in ``output/ops/VPS_UPGRADE_GATE.md``.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any

from src.streaming.vod_loop import CHANNELS

EXTRA_PLATFORMS = ("twitch", "kick", "fb_live")

_ENV_PREFIX = {
    "twitch": "TWITCH_RTMP",
    "kick": "KICK_RTMP",
    "fb_live": "FB_LIVE_RTMP",
}


@dataclass
class DestStatus:
    channel: str
    platform: str
    has_url: bool
    has_key: bool
    ready: bool


def _channel_suffix(channel: str) -> str:
    return channel.upper()


def max_concurrent_encodes() -> int:
    try:
        return max(1, int(os.getenv("VOD_LOOP_MAX_CONCURRENT_ENCODES") or "2"))
    except ValueError:
        return 2


def allow_1080() -> bool:
    return (os.getenv("VOD_LOOP_ALLOW_1080") or "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def dest_status(channel: str, platform: str) -> DestStatus:
    prefix = _ENV_PREFIX[platform]
    suf = _channel_suffix(channel)
    url = (os.getenv(f"{prefix}_URL_{suf}") or "").strip()
    key = (os.getenv(f"{prefix}_KEY_{suf}") or "").strip()
    return DestStatus(
        channel=channel,
        platform=platform,
        has_url=bool(url),
        has_key=bool(key),
        ready=bool(url and key),
    )


def inventory() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    ready_count = 0
    for ch in CHANNELS:
        for plat in EXTRA_PLATFORMS:
            st = dest_status(ch, plat)
            rows.append(asdict(st))
            if st.ready:
                ready_count += 1
    # YouTube Live encodes (vod_loop) count toward budget when keys present —
    # caller should add YT ready count; we expose policy only here.
    return {
        "ok": True,
        "module": "multiplatform",
        "max_concurrent_encodes": max_concurrent_encodes(),
        "allow_1080": allow_1080(),
        "upgrade_gate_doc": "output/ops/VPS_UPGRADE_GATE.md",
        "extra_ready_count": ready_count,
        "destinations": rows,
        "policy": {
            "prefer_second_supervise_over_tee": True,
            "youtube_primary": True,
            "enable_after_yt_live_48h_green": True,
        },
    }


def within_encode_budget(planned_encodes: int) -> bool:
    return planned_encodes <= max_concurrent_encodes()
