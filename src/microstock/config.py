"""Config loading for the microstock engine.

Mirrors the farm's ``_agents_cfg()`` convention (read JSON fresh, tolerate a
missing file) but keeps microstock config in its own tree so the two systems
never share a config blast radius.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

from src.microstock import paths


class MicrostockConfigError(RuntimeError):
    """Raised when config is missing or structurally unusable."""


def _read_json(path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:  # noqa: BLE001
        raise MicrostockConfigError(f"{path.name} is not valid JSON: {exc}") from exc


def load_settings() -> dict[str, Any]:
    """Full settings.json. Read fresh each call so edits apply without restart."""
    return _read_json(paths.SETTINGS_PATH, {})


def section(name: str) -> dict[str, Any]:
    """One top-level settings section, always a dict."""
    value = load_settings().get(name)
    return value if isinstance(value, dict) else {}


def is_enabled() -> bool:
    """Master switch. Everything in the beat is a no-op while this is false."""
    return bool(load_settings().get("enabled", False))


def distribution_enabled() -> bool:
    """Second, independent gate — nothing leaves the machine unless this is true."""
    return bool(is_enabled() and section("distribution").get("enabled", False))


@lru_cache(maxsize=1)
def _niches_raw() -> dict[str, Any]:
    return _read_json(paths.NICHES_PATH, {"niches": []})


def load_niches(*, enabled_only: bool = True) -> list[dict[str, Any]]:
    """Niche matrix rows, optionally filtered to enabled ones."""
    rows = list(_niches_raw().get("niches") or [])
    if enabled_only:
        rows = [n for n in rows if n.get("enabled", True)]
    return rows


def niche_by_name(name: str) -> dict[str, Any] | None:
    for niche in load_niches(enabled_only=False):
        if niche.get("name") == name:
            return niche
    return None


@lru_cache(maxsize=1)
def _platforms_raw() -> dict[str, Any]:
    return _read_json(paths.PLATFORMS_PATH, {"platforms": []})


def load_platforms(
    *, enabled_only: bool = True, tier: str | None = None
) -> list[dict[str, Any]]:
    """Platform matrix rows, sorted by priority (best royalty first).

    ``permanently_excluded`` rows (Shutterstock) are *never* returned, whatever
    the filters — uploading third-party AI there risks the whole account.
    """
    rows = [p for p in (_platforms_raw().get("platforms") or []) if not p.get("permanently_excluded")]
    if enabled_only:
        rows = [p for p in rows if p.get("enabled", False)]
    if tier is not None:
        rows = [p for p in rows if p.get("tier") == tier]
    return sorted(rows, key=lambda p: p.get("priority", 99))


def platform_by_name(name: str) -> dict[str, Any] | None:
    for platform in _platforms_raw().get("platforms") or []:
        if platform.get("name") == name:
            return platform
    return None


def release_order() -> list[str]:
    """Platform release order for the drip queue — highest royalty first."""
    configured = section("distribution").get("release_order")
    if isinstance(configured, list) and configured:
        return [str(x) for x in configured]
    return [p["name"] for p in load_platforms(enabled_only=False)]


def ftp_credentials(platform_name: str) -> tuple[str, str, str] | None:
    """Resolve (host, user, password) for a platform from the environment.

    Follows the farm's ``YOUTUBE_TOKEN_PATH_<CHANNEL>`` convention: credentials
    live only in ``.env``, never in the committed platform matrix. Returns None
    when the platform is not fully configured.
    """
    platform = platform_by_name(platform_name)
    if not platform:
        return None
    slug = platform_name.upper().replace("-", "_")
    ftp_cfg = platform.get("ftp") or {}
    host = (
        os.environ.get(ftp_cfg.get("host_env", f"MICROSTOCK_FTP_{slug}_HOST"), "")
        or ftp_cfg.get("default_host", "")
    ).strip()
    user = os.environ.get(f"MICROSTOCK_FTP_{slug}_USER", "").strip()
    password = os.environ.get(f"MICROSTOCK_FTP_{slug}_PASS", "").strip()
    if not (host and user and password):
        return None
    return host, user, password


def reload_caches() -> None:
    """Drop memoised niche/platform reads (tests, or after editing config)."""
    _niches_raw.cache_clear()
    _platforms_raw.cache_clear()
