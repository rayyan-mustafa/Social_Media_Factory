"""Dynamic Prop Registry for Weird Biology.

Manages built-in and custom synthesized props stored in `config/weird_biology_props_custom.json`.
Supports infinite scaling, keyword search, persistent registration, and safe dynamic SVG rendering.
"""

from __future__ import annotations

import ast
import json
import logging
import math
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[2]
CUSTOM_PROPS_FILE = ROOT_DIR / "config" / "weird_biology_props_custom.json"

BUILTIN_PROPS: tuple[str, ...] = (
    "none",
    "door",
    "crib",
    "arrow",
    "thermometer",
    "brain_icon",
    "floor_only",
    "bubble",
    "room_corner",
    "sun_ray",
    "rain_drops",
    "smell_cloud",
    "equals_sign",
    "cheese_wedge",
    "bacteria_blob",
    "sneeze_burst",
    "heat_waves",
)

_CUSTOM_PROPS_CACHE: dict[str, dict[str, Any]] | None = None

_FORBIDDEN_AST_NODES = (
    ast.Import,
    ast.ImportFrom,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.Raise,
    ast.Delete,
    ast.Global,
    ast.Nonlocal,
    ast.Lambda,
)


def compile_prop_code(python_code: str) -> Any:
    """Compile generated drawing code after rejecting unsafe syntax and calls."""
    tree = ast.parse(python_code, mode="exec")
    for node in ast.walk(tree):
        if isinstance(node, _FORBIDDEN_AST_NODES):
            raise ValueError(f"unsupported syntax: {type(node).__name__}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError("dunder attribute access is forbidden")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ValueError("dunder names are forbidden")
        if isinstance(node, ast.Call):
            call = node.func
            allowed = (
                isinstance(call, ast.Name)
                and call.id in {"range", "min", "max", "abs", "round", "len"}
            ) or (
                isinstance(call, ast.Attribute)
                and isinstance(call.value, ast.Name)
                and (
                    (call.value.id == "parts" and call.attr == "append")
                    or (call.value.id == "rng" and call.attr in {"uniform", "randint", "random", "choice"})
                    or (call.value.id == "math" and not call.attr.startswith("_"))
                )
            )
            if not allowed:
                raise ValueError("generated code contains a forbidden function call")
    return compile(tree, "<dynamic-prop>", "exec")


def load_custom_props(force_reload: bool = False) -> dict[str, dict[str, Any]]:
    """Load persistent custom props from JSON storage."""
    global _CUSTOM_PROPS_CACHE
    if _CUSTOM_PROPS_CACHE is not None and not force_reload:
        return _CUSTOM_PROPS_CACHE

    if not CUSTOM_PROPS_FILE.exists():
        CUSTOM_PROPS_FILE.parent.mkdir(parents=True, exist_ok=True)
        CUSTOM_PROPS_FILE.write_text("{}", encoding="utf-8")
        _CUSTOM_PROPS_CACHE = {}
        return _CUSTOM_PROPS_CACHE

    try:
        data = json.loads(CUSTOM_PROPS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _CUSTOM_PROPS_CACHE = data
        else:
            _CUSTOM_PROPS_CACHE = {}
    except Exception as err:
        logger.warning("Failed to parse custom props JSON (%s); using empty registry", err)
        _CUSTOM_PROPS_CACHE = {}

    return _CUSTOM_PROPS_CACHE


def save_custom_props() -> None:
    """Save custom props cache back to JSON disk storage."""
    global _CUSTOM_PROPS_CACHE
    if _CUSTOM_PROPS_CACHE is None:
        return
    try:
        CUSTOM_PROPS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_path = CUSTOM_PROPS_FILE.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(_CUSTOM_PROPS_CACHE, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp_path.replace(CUSTOM_PROPS_FILE)
    except Exception as err:
        logger.error("Failed to save custom props to %s: %s", CUSTOM_PROPS_FILE, err)


def get_all_props() -> list[str]:
    """Get list of all available props (built-in + custom dynamically registered)."""
    custom = load_custom_props()
    all_props = list(BUILTIN_PROPS)
    for k in sorted(custom.keys()):
        if k not in all_props:
            all_props.append(k)
    return all_props


def get_planner_props(context: str, max_custom: int = 40) -> list[str]:
    """Return built-ins plus only context-relevant custom props for bounded prompts."""
    keywords = re.findall(r"[a-z0-9]+", (context or "").lower())
    relevant = [
        name
        for name, _score in search_props(list(dict.fromkeys(keywords)))
        if name not in BUILTIN_PROPS
    ][:max_custom]
    return [*BUILTIN_PROPS, *relevant]


def has_prop(prop_name: str) -> bool:
    """Check if prop exists in built-in list or custom registry."""
    if not prop_name or not isinstance(prop_name, str):
        return False
    p = prop_name.strip().lower()
    if p in BUILTIN_PROPS:
        return True
    custom = load_custom_props()
    return p in custom


def search_props(keywords: list[str]) -> list[tuple[str, float]]:
    """Search existing props using keyword matching to avoid duplicate synthesis.

    Returns list of (prop_name, relevance_score) sorted by highest relevance.
    """
    if not keywords:
        return []

    norm_kw = [k.strip().lower() for k in keywords if k.strip()]
    if not norm_kw:
        return []

    results: dict[str, float] = {}

    # Check built-in props
    for bp in BUILTIN_PROPS:
        score = 0.0
        for kw in norm_kw:
            if kw == bp:
                score += 1.0
            elif kw in bp or bp in kw:
                score += 0.5
        if score > 0:
            results[bp] = score

    # Check custom props metadata
    custom = load_custom_props()
    for cp, meta in custom.items():
        score = 0.0
        tags = [str(t).lower() for t in meta.get("tags") or []]
        desc = str(meta.get("description") or "").lower()
        for kw in norm_kw:
            if kw == cp:
                score += 1.5
            elif kw in tags:
                score += 1.0
            elif kw in desc or kw in cp:
                score += 0.5
        if score > 0:
            results[cp] = score

    sorted_res = sorted(results.items(), key=lambda x: x[1], reverse=True)
    return sorted_res


def register_custom_prop(
    name: str,
    description: str,
    python_code: str,
    category: str = "biology",
    tags: list[str] | None = None,
) -> bool:
    """Register a new synthesized prop in the persistent registry."""
    clean_name = re.sub(r"[^a-z0-9_]", "", name.strip().lower().replace("-", "_"))
    if not clean_name:
        return False
    if len(python_code.encode("utf-8")) > 32_000:
        logger.warning("Rejected oversized dynamic prop '%s'", clean_name)
        return False
    try:
        compile_prop_code(python_code)
    except (SyntaxError, ValueError) as err:
        logger.warning("Rejected unsafe dynamic prop '%s': %s", clean_name, err)
        return False

    custom = load_custom_props()
    entry = {
        "description": description,
        "category": category,
        "tags": tags or [],
        "python_code": python_code,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    custom[clean_name] = entry
    save_custom_props()
    logger.info("Registered dynamic custom prop '%s'", clean_name)
    return True


def render_custom_prop(
    prop_name: str,
    pal: dict[str, tuple[int, int, int]],
    W: int,
    H: int,
    rng: random.Random,
    *,
    parts: list[str] | None = None,
) -> list[str]:
    """Execute dynamic python SVG rendering code for a custom prop safely.

    Returns list of SVG XML string parts.
    """
    if parts is None:
        parts = []

    custom = load_custom_props()
    p_key = prop_name.strip().lower()
    if p_key not in custom:
        return parts

    meta = custom[p_key]
    py_code = meta.get("python_code") or ""

    if not py_code:
        return parts

    # Convert RGB tuples in pal to hex helpers
    def _hex(rgb: tuple[int, int, int]) -> str:
        return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"

    stroke = _hex(pal.get("prop_stroke", (0x11, 0x11, 0x11)))
    fill = _hex(pal.get("prop_fill", (0x5A, 0x7A, 0x7A)))
    coral = _hex(pal.get("coral_accent", (0xE8, 0x72, 0x4C)))
    white = _hex(pal.get("head_fill", (0xFF, 0xFF, 0xFF)))
    stick = _hex(pal.get("stick_stroke", (0x11, 0x11, 0x11)))

    # Safe evaluation environment with standard drawing helpers
    exec_scope = {
        "W": W,
        "H": H,
        "rng": rng,
        "pal": pal,
        "parts": parts,
        "stroke": stroke,
        "fill": fill,
        "coral": coral,
        "white": white,
        "stick": stick,
        "math": math,
        "_hex": _hex,
    }

    exec_scope["__builtins__"] = {
        "range": range,
        "min": min,
        "max": max,
        "abs": abs,
        "round": round,
        "len": len,
    }

    try:
        compiled = compile_prop_code(py_code)
        exec(compiled, exec_scope)  # noqa: S102
    except Exception as err:
        logger.error("Error rendering dynamic custom prop '%s': %s", p_key, err)

    return parts


class DynamicPropsTuple(tuple):
    """Dynamic tuple-like wrapper around all props to keep `PROPS` backward compatible."""

    def __contains__(self, item: object) -> bool:
        if isinstance(item, str):
            return has_prop(item)
        return False

    def __iter__(self):
        return iter(get_all_props())

    def __len__(self):
        return len(get_all_props())

    def __getitem__(self, index):
        return get_all_props()[index]

    def __repr__(self):
        return f"DynamicPropsTuple({get_all_props()!r})"
