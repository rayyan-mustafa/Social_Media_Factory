"""Per-channel YouTube OAuth token path resolution."""

from __future__ import annotations

from pathlib import Path

from src.services.youtube_channel_auth import (
    describe_channel_auth,
    normalize_youtube_channel,
    youtube_token_path,
)


def test_normalize_legacy_to_napstorian():
    assert normalize_youtube_channel(None) == "napstorian"
    assert normalize_youtube_channel("") == "napstorian"
    assert normalize_youtube_channel("Sheet1") == "napstorian"
    assert normalize_youtube_channel("napping_historian") == "napping_historian"


def test_napstorian_uses_legacy_token_path(monkeypatch, tmp_path: Path):
    legacy = tmp_path / "youtube_token.json"
    legacy.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("YOUTUBE_TOKEN_PATH", str(legacy))
    # clear other overrides
    monkeypatch.delenv("YOUTUBE_TOKEN_PATH_NAPSTORIAN", raising=False)
    from src.services import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    p = youtube_token_path("napstorian")
    assert p == legacy.resolve() or p == Path(legacy)
    assert p.exists()


def test_historian_separate_token_file(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("YOUTUBE_TOKEN_PATH_NAPPING_HISTORIAN", raising=False)
    from src.services import settings as settings_mod
    from src.services import youtube_channel_auth as yca

    settings_mod.get_settings.cache_clear()
    # Point ROOT indirectly via env override for historian only
    hist = tmp_path / "youtube_token_napping_historian.json"
    monkeypatch.setenv("YOUTUBE_TOKEN_PATH_NAPPING_HISTORIAN", str(hist))
    p = yca.youtube_token_path("napping_historian")
    assert p == hist.resolve() or p == Path(hist)
    # napstorian must not resolve to historian file
    nap = yca.youtube_token_path("napstorian")
    assert "napping_historian" not in nap.name


def test_describe_channel_auth_keys():
    info = describe_channel_auth("napping_historian")
    assert info["channel"] == "napping_historian"
    assert "youtube_token_napping_historian.json" in str(info["token_path"])
