"""Generate, register, and reuse Pillow assets for Weird Biology scenes.

The planner supplies a semantic asset name plus a safe declarative recipe. Missing
assets are generated once, validated, added to the manifest, and reused thereafter.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from src.services.settings import ROOT
from src.services.weird_biology_asset_registry import ASSETS_ROOT, resolve_asset

MANIFEST_PATH = ROOT / "config" / "weird_biology_asset_manifest.json"
_CANVAS = (320, 240)


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "", name.strip().lower().replace("-", "_"))


def _load_manifest() -> dict[str, dict[str, Any]]:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_manifest(manifest: dict[str, dict[str, Any]]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = MANIFEST_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(MANIFEST_PATH)


def find_asset(name: str, tags: list[str] | None = None) -> dict[str, Any] | None:
    """Find an existing asset by semantic name or overlapping tags."""
    key = _key(name)
    manifest = _load_manifest()
    if key in manifest and resolve_asset(str(manifest[key].get("path") or "")):
        return manifest[key]

    wanted = {str(tag).lower() for tag in (tags or [])}
    best: tuple[int, dict[str, Any]] | None = None
    for item in manifest.values():
        item_tags = {str(tag).lower() for tag in item.get("tags") or []}
        score = len(wanted & item_tags)
        if score and (best is None or score > best[0]):
            best = (score, item)
    return best[1] if best else None


def _draw_recipe(recipe: dict[str, Any]) -> Image.Image:
    """Render a bounded declarative recipe; no generated Python is executed."""
    image = Image.new("RGBA", _CANVAS, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for shape in recipe.get("shapes") or []:
        if not isinstance(shape, dict):
            continue
        kind = str(shape.get("type") or "").lower()
        fill = tuple(shape.get("fill") or (90, 122, 122, 255))
        outline = tuple(shape.get("outline") or (17, 17, 17, 255))
        width = max(1, min(20, int(shape.get("width") or 4)))
        coords = shape.get("coords") or []
        if kind in {"ellipse", "circle"} and len(coords) == 4:
            draw.ellipse(tuple(coords), fill=fill, outline=outline, width=width)
        elif kind == "rectangle" and len(coords) == 4:
            draw.rectangle(tuple(coords), fill=fill, outline=outline, width=width)
        elif kind == "line" and len(coords) == 4:
            draw.line(tuple(coords), fill=outline, width=width)
        elif kind == "polygon" and isinstance(coords, list) and len(coords) >= 3:
            draw.polygon([tuple(point) for point in coords], fill=fill, outline=outline)
    return image


def _validate_png(path: Path) -> None:
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        if image.mode != "RGBA":
            raise ValueError("generated asset must be RGBA")
        if image.width <= 0 or image.height <= 0:
            raise ValueError("generated asset has invalid dimensions")


def propose_missing_asset(
    name: str,
    description: str,
    *,
    tags: list[str] | None = None,
    recipe: dict[str, Any] | None = None,
    beat_text: str = "",
    kind: str = "prop",
) -> dict[str, Any]:
    """Return a proposed asset spec without persisting it to the library.

    The planner may suggest missing assets, but only a later validated generation
    step should write to the persistent manifest. This keeps the asset library
    strictly evidence-based and prevents speculative or duplicate assets.
    """
    clean_name = _key(name)
    if not clean_name:
        raise ValueError("asset name is required")
    return {
        "name": clean_name,
        "kind": kind,
        "description": description,
        "tags": tags or [],
        "recipe": recipe,
        "beat_examples": [beat_text] if beat_text else [],
        "status": "proposed",
        "persisted": False,
    }


def ensure_asset_for_beat(
    name: str,
    description: str,
    *,
    tags: list[str] | None = None,
    recipe: dict[str, Any] | None = None,
    beat_text: str = "",
) -> dict[str, Any]:
    """Reuse an asset or generate/register it once from a safe recipe."""
    clean_name = _key(name)
    if not clean_name:
        raise ValueError("asset name is required")

    existing = find_asset(clean_name, tags)
    if existing:
        return {**existing, "reused": True}
    if not recipe:
        raise ValueError(f"missing generation recipe for asset '{clean_name}'")

    relative_path = f"generated/{clean_name}.png"
    output = ASSETS_ROOT / relative_path
    output.parent.mkdir(parents=True, exist_ok=True)
    _draw_recipe(recipe).save(output, format="PNG")
    _validate_png(output)

    entry = {
        "name": clean_name,
        "path": relative_path,
        "description": description,
        "tags": tags or [],
        "recipe": recipe,
        "beat_examples": [beat_text] if beat_text else [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest = _load_manifest()
    manifest[clean_name] = entry
    _save_manifest(manifest)
    return {**entry, "reused": False}
