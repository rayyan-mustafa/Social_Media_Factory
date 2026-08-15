"""Tests for farm-safe asset_verifier."""

from __future__ import annotations

from src.services.asset_verifier import normalize_license_tag, verify_asset


def test_approved_domain_and_license():
    r = verify_asset(
        "https://commons.wikimedia.org/wiki/File:X.jpg",
        "image",
        {"license_tag": "Public Domain", "digitization_note": ""},
        log=False,
    )
    assert r["status"] == "approved"
    assert r["checks"]["domain_whitelisted"] is True


def test_reject_non_whitelist_domain():
    r = verify_asset(
        "https://evil.example/a.jpg",
        "image",
        {"license_tag": "CC0"},
        log=False,
    )
    assert r["status"] == "rejected"
    assert "domain" in r["reason"]


def test_reject_missing_license():
    r = verify_asset(
        "https://images.metmuseum.org/a.jpg",
        "image",
        {"license_tag": ""},
        log=False,
    )
    assert r["status"] == "rejected"


def test_digitization_trap_rejects_not_manual_review():
    r = verify_asset(
        "https://commons.wikimedia.org/wiki/File:X.jpg",
        "image",
        {
            "license_tag": "PD-Art",
            "digitization_note": "restored by Studio Color Corp",
        },
        log=False,
    )
    assert r["status"] == "rejected"
    assert "digitization" in r["reason"].lower()


def test_footage_requires_audio_strip():
    r = verify_asset(
        "https://archive.org/details/x",
        "footage",
        {"license_tag": "public domain"},
        log=False,
    )
    assert r["checks"]["requires_audio_strip"] is True


def test_pexels_blocked_even_if_domain_forced():
    r = verify_asset(
        "https://www.pexels.com/photo/1",
        "image",
        {"license_tag": "CC0"},
        domains=["pexels.com"],
        log=False,
    )
    assert r["status"] == "rejected"
    assert "stock" in r["reason"].lower()


def test_normalize_met_oa():
    assert normalize_license_tag("Met Open Access") == "cc0"
