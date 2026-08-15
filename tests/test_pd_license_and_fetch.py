"""License gate + Wikimedia/MET Open Access fetch paths for PD heroes."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from src.services.pd_clippings import (
    DEFAULT_PD_HERO_CURATED,
    MET_OPEN_ACCESS_CURATED,
    TARGET_HERO_MAX,
    TARGET_HERO_MIN,
    fetch_curated_heroes,
    fetch_met_open_access_still,
    license_gate_or_raise,
    license_is_commercial_ok,
    met_open_access_image_url,
)


def _jpeg_bytes(color: tuple[int, int, int] = (40, 30, 20)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (640, 480), color).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def test_license_gate_allows_met_oa_and_blocks_pexels():
    assert license_is_commercial_ok("CC0 (Met Open Access)")
    assert license_is_commercial_ok("Met Open Access")
    assert license_is_commercial_ok("PD-Art (Wikimedia Commons)")
    assert license_is_commercial_ok("US government work")
    assert not license_is_commercial_ok("Pexels License")
    assert not license_is_commercial_ok("Mixkit License")
    assert not license_is_commercial_ok("CC BY-NC-SA 4.0")
    assert not license_is_commercial_ok("check Commons file page")
    license_gate_or_raise("CC0 (Met Open Access)", label="met")
    try:
        license_gate_or_raise("fair use", label="bad")
        assert False, "expected raise"
    except ValueError as exc:
        assert "license gate refused" in str(exc)


def test_met_open_access_image_url_requires_public_domain():
    assert met_open_access_image_url({"isPublicDomain": False, "primaryImage": "http://x"}) is None
    assert met_open_access_image_url({"isPublicDomain": True, "primaryImage": ""}) is None
    assert (
        met_open_access_image_url(
            {
                "isPublicDomain": True,
                "primaryImage": "https://images.metmuseum.org/x.jpg",
            }
        )
        == "https://images.metmuseum.org/x.jpg"
    )


def test_fetch_met_open_access_still_refuses_non_pd(tmp_path: Path):
    dest = tmp_path / "raw.jpg"

    def _fake_obj(_oid: int, *, timeout: float = 45.0):
        return {"isPublicDomain": False, "primaryImage": "https://example.com/a.jpg"}

    with patch("src.services.pd_clippings.fetch_met_object", side_effect=_fake_obj):
        try:
            fetch_met_open_access_still(23936, dest)
            assert False, "expected refuse"
        except RuntimeError as exc:
            assert "not Open Access" in str(exc)


def test_fetch_met_open_access_still_ok(tmp_path: Path):
    dest = tmp_path / "raw.jpg"
    obj = {
        "isPublicDomain": True,
        "primaryImage": "https://images.metmuseum.org/armor.jpg",
        "title": "Field Armor of King Henry VIII",
        "objectURL": "https://www.metmuseum.org/art/collection/search/23936",
    }

    def _fake_fetch(url: str, path: Path, *, timeout: float = 60.0):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_jpeg_bytes((90, 80, 70)))
        return path

    with (
        patch("src.services.pd_clippings.fetch_met_object", return_value=obj),
        patch("src.services.pd_clippings.fetch_url", side_effect=_fake_fetch),
    ):
        out, meta, img = fetch_met_open_access_still(23936, dest)
    assert out.is_file() and out.stat().st_size > 800
    assert meta["isPublicDomain"] is True
    assert "metmuseum.org" in img


def test_fetch_curated_heroes_wikimedia_and_met_stamps(tmp_path: Path):
    curated = [
        {
            "id": "wiki_hero",
            "title": "Wiki Hero",
            "commons_file": "Example.jpg",
            "license": "PD-Art (Wikimedia Commons)",
            "source": "wikimedia_commons",
            "commercial_ok": True,
        },
        {
            "id": "met_hero",
            "title": "Met Hero",
            "met_object_id": 23936,
            "license": "CC0 (Met Open Access)",
            "source": "met_open_access",
            "commercial_ok": True,
        },
        {
            "id": "blocked_nc",
            "title": "Bad",
            "commons_file": "Bad.jpg",
            "license": "CC BY-NC 4.0",
            "source": "wikimedia_commons",
            "commercial_ok": True,
        },
        {
            "id": "blocked_pexels",
            "title": "Pexels",
            "commons_file": "P.jpg",
            "license": "Pexels License",
            "source": "wikimedia_commons",
        },
    ]

    def _fake_fetch(url: str, path: Path, *, timeout: float = 60.0):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_jpeg_bytes())
        return path

    def _fake_met(oid: int, dest: Path, *, timeout: float = 60.0):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_jpeg_bytes((20, 40, 60)))
        return (
            dest,
            {
                "isPublicDomain": True,
                "primaryImage": "https://images.metmuseum.org/x.jpg",
                "title": "Met Hero",
            },
            "https://images.metmuseum.org/x.jpg",
        )

    with (
        patch("src.services.pd_clippings.fetch_url", side_effect=_fake_fetch),
        patch(
            "src.services.pd_clippings.fetch_met_open_access_still",
            side_effect=_fake_met,
        ),
        patch("time.sleep", return_value=None),
    ):
        heroes = fetch_curated_heroes(
            tmp_path / "pd", curated=curated, pause_s=0, reuse_fitted=False
        )

    assert len(heroes) == 2
    ids = {h.id for h in heroes}
    assert ids == {"wiki_hero", "met_hero"}
    for h in heroes:
        assert h.commercial_ok is True
        assert h.source_url
        assert license_is_commercial_ok(h.license)
        side = Path(h.path).with_suffix(".jpg.license.json")
        assert side.is_file()
        meta = json.loads(side.read_text(encoding="utf-8"))
        assert meta["commercial_ok"] is True
        assert meta["source_url"]
        assert meta["license"]

    manifest = json.loads((tmp_path / "pd" / "pd_heroes.json").read_text(encoding="utf-8"))
    assert manifest["target_min"] == TARGET_HERO_MIN
    assert manifest["target_max"] == TARGET_HERO_MAX
    assert any(b.get("id") == "blocked_nc" for b in manifest.get("blocked") or [])
    assert "wikimedia_commons" in manifest["allowed_sources"]
    assert "met_open_access" in manifest["allowed_sources"]


def test_default_curated_pack_targets_dense_quantity():
    assert TARGET_HERO_MIN == 20
    assert TARGET_HERO_MAX == 30
    assert len(DEFAULT_PD_HERO_CURATED) >= TARGET_HERO_MIN
    assert len(MET_OPEN_ACCESS_CURATED) >= 4
    sources = {str(i.get("source")) for i in DEFAULT_PD_HERO_CURATED}
    assert sources <= {"wikimedia_commons", "met_open_access"}
    for item in DEFAULT_PD_HERO_CURATED:
        assert license_is_commercial_ok(str(item.get("license") or ""))
