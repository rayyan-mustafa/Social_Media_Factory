"""Load region / vertical / chain-skip packs."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from src.services.settings import CONFIG_DIR

LEADGEN_DIR = CONFIG_DIR / "leadgen"


def _read(name: str) -> dict[str, Any]:
    path = LEADGEN_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=8)
def load_regions() -> dict[str, Any]:
    return _read("regions.json")


@lru_cache(maxsize=8)
def load_verticals() -> dict[str, Any]:
    return _read("verticals.json")


@lru_cache(maxsize=8)
def load_chains_skip() -> list[str]:
    data = _read("chains_skip.json")
    return [str(n).strip() for n in (data.get("names") or []) if str(n).strip()]


def list_regions() -> list[str]:
    return sorted((load_regions().get("regions") or {}).keys())


def list_verticals() -> list[str]:
    return sorted((load_verticals().get("verticals") or {}).keys())


def load_region(region_id: str) -> dict[str, Any]:
    key = (region_id or "").strip().lower()
    pack = (load_regions().get("regions") or {}).get(key)
    if not pack:
        raise KeyError(f"unknown region {region_id!r}; have {list_regions()}")
    out = dict(pack)
    out["id"] = key
    return out


def load_vertical(vertical_id: str) -> dict[str, Any]:
    key = (vertical_id or "").strip().lower()
    pack = (load_verticals().get("verticals") or {}).get(key)
    if not pack:
        raise KeyError(f"unknown vertical {vertical_id!r}; have {list_verticals()}")
    out = dict(pack)
    out["id"] = key
    return out


def is_chain(name: str, *, skip: list[str] | None = None) -> bool:
    blob = (name or "").lower()
    if not blob:
        return False
    for n in skip if skip is not None else load_chains_skip():
        if n.lower() in blob:
            return True
    return False
