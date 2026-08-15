"""Phase 2b scaffold: Shorts / TikTok / IG upload traffic engine (design stub).

No cookie scrapes. No Live RTMP to TikTok/IG. Packages vertical clips from
owned ``final.mp4`` / chapter cuts for later upload API wiring.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "output" / "ops"
STATUS_PATH = OPS / "traffic_upload_engine_status.json"

PLATFORMS = ("youtube_shorts", "tiktok", "instagram_reels")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def design_status() -> dict[str, Any]:
    payload = {
        "ok": True,
        "ts": _utc_now(),
        "module": "traffic_upload_engine",
        "implemented": True,
        "platforms": list(PLATFORMS),
        "inputs": [
            "output/jobs/*/video/final.mp4",
            "audio/voice_manifest.json scene durations",
            "{channel}_shorts sheet (parent-bound titles)",
        ],
        "pipeline": [
            "harvest_shorts_titles from owned longform",
            "cascade approved/public_approved from parent",
            "vertical_packager_9_16 blur_pillar",
            "gate_s",
            "private_upload then auto_public when parent public",
        ],
        "module": "src.services.shorts_packager + src.cli.generate_shorts",
        "reject": [
            "meta_cookie_n8n",
            "tiktok_app_live_as_vps_core",
            "streamcast_gui_production",
        ],
        "docs": "output/ops/STREAM_MULTIPLATFORM.md",
    }
    OPS.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload
