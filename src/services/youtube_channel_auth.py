"""Per-channel YouTube OAuth paths (napstorian + napping_historian).

Hard rule: never overwrite ``config/youtube_token.json`` when authenticating
another Brand Account. That file stays the **napstorian** (legacy) token.

Resolution order for tokens:
1. ``YOUTUBE_TOKEN_PATH_<CHANNEL>`` env (e.g. YOUTUBE_TOKEN_PATH_NAPPING_HISTORIAN)
2. ``config/youtube_token_<channel>.json`` for non-legacy channels
3. Settings ``youtube_token_path`` for napstorian / empty / default

Client secrets may be shared (same GCP Desktop OAuth client) or overridden via
``YOUTUBE_CLIENT_SECRETS_PATH_<CHANNEL>``.
"""

from __future__ import annotations

import os
from pathlib import Path

from src.services.settings import ROOT, get_settings

# Production Brand Accounts + staged empire skins (token files appear on activate).
KNOWN_CHANNELS: tuple[str, ...] = (
    "napstorian",
    "napping_historian",
    "art_mysteries",
    "forgotten_empires",
    "science_history",
    "royal_courts",
    "business_empires",
    "money_history",
    "war_tech_history",
    "philosophy_sleep",
)
LEGACY_DEFAULT_CHANNEL = "napstorian"

# Path helpers use os.getenv for per-channel overrides; ensure .env is visible.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass


def normalize_youtube_channel(channel: str | None) -> str:
    """Map sheet/job channel → auth key. Empty/legacy → napstorian."""
    c = (channel or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not c or c in {"sheet1", "sheet_1", "default", "legacy", "primary"}:
        return LEGACY_DEFAULT_CHANNEL
    return c


def _env_suffix(channel: str) -> str:
    return normalize_youtube_channel(channel).upper()


def _as_abs(path: Path | str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    return p


def youtube_token_path(channel: str | None = None) -> Path:
    """Absolute path to the OAuth token JSON for this channel."""
    ch = normalize_youtube_channel(channel)
    env_override = (os.getenv(f"YOUTUBE_TOKEN_PATH_{_env_suffix(ch)}") or "").strip()
    if env_override:
        return _as_abs(env_override)

    if ch == LEGACY_DEFAULT_CHANNEL:
        # Keep existing napstorian token path untouched.
        get_settings.cache_clear()
        s = get_settings()
        return _as_abs(s.youtube_token_path)

    return ROOT / "config" / f"youtube_token_{ch}.json"


def youtube_client_secrets_path(channel: str | None = None) -> Path:
    """Absolute path to Desktop OAuth client secrets for this channel.

    Defaults to the shared ``youtube_client_secrets_path`` so one GCP client can
    authorize multiple Brand Accounts into separate token files.
    """
    ch = normalize_youtube_channel(channel)
    env_override = (
        os.getenv(f"YOUTUBE_CLIENT_SECRETS_PATH_{_env_suffix(ch)}") or ""
    ).strip()
    if env_override:
        return _as_abs(env_override)

    get_settings.cache_clear()
    s = get_settings()
    return _as_abs(s.youtube_client_secrets_path)


def describe_channel_auth(channel: str | None = None) -> dict[str, str | bool]:
    ch = normalize_youtube_channel(channel)
    token = youtube_token_path(ch)
    secrets = youtube_client_secrets_path(ch)
    return {
        "channel": ch,
        "token_path": str(token),
        "token_exists": token.exists(),
        "client_secrets_path": str(secrets),
        "client_secrets_exists": secrets.exists(),
        "legacy_shared_token": ch == LEGACY_DEFAULT_CHANNEL,
    }
