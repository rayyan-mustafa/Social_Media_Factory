"""Persistent inventory and request queue for Weird Biology visual assets."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.services.settings import ROOT

ASSETS_ROOT = ROOT / "assets" / "weird_biology"
REQUESTS_PATH = ROOT / "config" / "weird_biology_asset_requests.json"


def _read_requests() -> dict[str, dict[str, Any]]:
    if not REQUESTS_PATH.exists():
        return {}
    try:
        data = json.loads(REQUESTS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_requests(data: dict[str, dict[str, Any]]) -> None:
    REQUESTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = REQUESTS_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(REQUESTS_PATH)


def list_assets() -> list[str]:
    """Return existing PNG assets, relative to the Weird Biology asset root."""
    if not ASSETS_ROOT.exists():
        return []
    return sorted(
        str(path.relative_to(ASSETS_ROOT))
        for path in ASSETS_ROOT.rglob("*.png")
        if path.is_file()
    )


def resolve_asset(asset: str) -> Path | None:
    """Resolve a relative asset safely; reject traversal and missing files."""
    candidate = (ASSETS_ROOT / asset).resolve()
    try:
        candidate.relative_to(ASSETS_ROOT.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def request_asset(
    name: str,
    description: str,
    *,
    kind: str = "prop",
    beat_text: str = "",
) -> dict[str, Any]:
    """Queue a missing asset for generation/review without pretending it exists."""
    key = name.strip().lower().replace(" ", "_")
    requests = _read_requests()
    current = requests.get(key) or {
        "name": key,
        "kind": kind,
        "description": description,
        "status": "requested",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if beat_text and beat_text not in current.setdefault("beat_examples", []):
        current["beat_examples"].append(beat_text)
    requests[key] = current
    _write_requests(requests)
    return current


def asset_status(name: str) -> str:
    """Return available, requested, or missing for an asset name/path."""
    if resolve_asset(name):
        return "available"
    if name.strip().lower().replace(" ", "_") in _read_requests():
        return "requested"
    return "missing"
