"""Config loading, and the safety rules encoded in it."""

from __future__ import annotations

from src.microstock import config


def test_shutterstock_is_never_returned():
    """Uploading third-party AI to Shutterstock risks the whole account."""
    for kwargs in ({"enabled_only": False}, {"enabled_only": True}):
        assert "shutterstock" not in [p["name"] for p in config.load_platforms(**kwargs)]


def test_shutterstock_row_still_exists_and_is_flagged():
    row = config.platform_by_name("shutterstock")
    assert row is not None and row["permanently_excluded"] is True


def test_platforms_sorted_best_royalty_first():
    names = [p["name"] for p in config.load_platforms(enabled_only=False)]
    assert names[0] == "adobe_stock"


def test_tier_filter_splits_auto_from_manual():
    auto = {p["name"] for p in config.load_platforms(enabled_only=False, tier="auto")}
    manual = {p["name"] for p in config.load_platforms(enabled_only=False, tier="manual_review")}
    assert "vecteezy" in auto and "adobe_stock" not in auto
    assert {"adobe_stock", "freepik"} <= manual


def test_distribution_requires_both_gates():
    """Distribution must be inert unless master AND distribution are enabled."""
    assert config.distribution_enabled() is False


def test_ftp_credentials_absent_without_env(monkeypatch):
    for suffix in ("HOST", "USER", "PASS"):
        monkeypatch.delenv(f"MICROSTOCK_FTP_VECTEEZY_{suffix}", raising=False)
    assert config.ftp_credentials("vecteezy") is None


def test_ftp_credentials_resolve_from_env(monkeypatch):
    monkeypatch.setenv("MICROSTOCK_FTP_VECTEEZY_USER", "u")
    monkeypatch.setenv("MICROSTOCK_FTP_VECTEEZY_PASS", "p")
    creds = config.ftp_credentials("vecteezy")
    assert creds == ("ftp.vecteezy.com", "u", "p")


def test_unknown_platform_has_no_credentials():
    assert config.ftp_credentials("nope") is None


def test_niches_have_required_shape():
    niches = config.load_niches()
    assert niches
    for niche in niches:
        assert {"name", "weight", "style_hint", "deliverables"} <= set(niche)
        assert niche["weight"] > 0
