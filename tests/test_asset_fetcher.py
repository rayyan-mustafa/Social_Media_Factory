"""Tests for asset_fetcher normalization + staging (mocked APIs)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.services.asset_fetcher import fetch_met, search_and_stage


def test_fetch_met_skips_non_pd():
    search = MagicMock()
    search.json.return_value = {"objectIDs": [1, 2]}
    search.raise_for_status = MagicMock()

    obj_bad = MagicMock()
    obj_bad.json.return_value = {
        "isPublicDomain": False,
        "primaryImage": "https://images.metmuseum.org/a.jpg",
    }
    obj_bad.raise_for_status = MagicMock()

    obj_ok = MagicMock()
    obj_ok.json.return_value = {
        "isPublicDomain": True,
        "primaryImage": "https://images.metmuseum.org/b.jpg",
        "title": "Armor",
        "objectURL": "https://www.metmuseum.org/art/collection/search/2",
        "creditLine": "",
        "repository": "",
    }
    obj_ok.raise_for_status = MagicMock()

    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.get.side_effect = [search, obj_bad, obj_ok]

    with patch("src.services.asset_fetcher._client", return_value=client):
        with patch("src.services.asset_fetcher._delay"):
            rows = fetch_met("armor", max_results=5)
    assert len(rows) == 1
    assert rows[0]["metadata"]["license_tag"] == "Met Open Access"
    assert rows[0]["metadata"]["raw_source"] == "met_open_access"


def test_search_and_stage_routes_approved(tmp_path: Path):
    asset = {
        "source_url": "https://images.metmuseum.org/ok.jpg",
        "asset_type": "image",
        "title": "ok",
        "metadata": {
            "license_tag": "Met Open Access",
            "source_page_url": "https://www.metmuseum.org/x",
            "digitization_note": "",
            "raw_source": "met_open_access",
        },
    }

    def fake_download(a, save_dir):
        p = Path(save_dir) / "ok.jpg"
        p.write_bytes(b"fakejpg")
        p.with_suffix(".jpg.json").write_text(json.dumps(a), encoding="utf-8")
        return str(p)

    with patch("src.services.asset_fetcher.fetch_assets", return_value=[asset]):
        with patch("src.services.asset_fetcher.download_asset", side_effect=fake_download):
            summary = search_and_stage("armor", "image", tmp_path, max_results=2)
    assert summary["n_approved"] == 1
    assert (tmp_path / "approved").exists()


def test_keyword_stage_caps_are_soft_not_hero_product_cap():
    from src.services.asset_fetcher import KEYWORD_STAGE_MAX, VISION_PASS_HEROES_MAX

    # Soft defaults only — per-scene path is uncapped (rmagine_scene_fetch).
    assert KEYWORD_STAGE_MAX >= 50
    assert VISION_PASS_HEROES_MAX >= 1000


def test_collect_vision_pass_rejects_flux_fill(monkeypatch, tmp_path: Path):
    """FAIL assets stay out of passed; flux_on_vision_reject / ai_fill_gaps stay on."""
    from src.services import asset_fetcher as af

    img_fail = tmp_path / "fail.jpg"
    img_pass = tmp_path / "pass.jpg"
    img_fail.write_bytes(b"x")
    img_pass.write_bytes(b"y")

    def fake_validate(ref, *_a, **_k):
        if str(ref).endswith("fail.jpg"):
            return {"status": "FAIL", "confidence": 3, "reason": "off topic"}
        return {"status": "PASS", "confidence": 9, "reason": "on topic"}

    monkeypatch.setattr(
        "src.services.vision_judge.validate_image_with_vision_llm", fake_validate
    )

    judged = af.collect_vision_pass_assets(
        "tudor armor",
        [
            {"local_path": str(img_fail), "title": "bad"},
            {"local_path": str(img_pass), "title": "good"},
        ],
        max_pass=30,
    )
    assert judged["n_pass"] == 1
    assert judged["n_rejected"] == 1
    assert judged["ai_fill_gaps"] is True
    assert judged["flux_on_vision_reject"] is True
    assert judged["reuse_weak_pd"] is False
    assert judged["passed"][0]["local_path"] == str(img_pass)
    assert judged["rejected"][0]["local_path"] == str(img_fail)


def test_apply_vision_pass_does_not_place_rejects(tmp_path: Path):
    from PIL import Image

    from src.services.asset_fetcher import apply_vision_pass_heroes_to_job

    job = tmp_path / "job"
    (job / "images").mkdir(parents=True)
    src = tmp_path / "only_if_passed.jpg"
    Image.new("RGB", (640, 480), (1, 2, 3)).save(src, format="JPEG")
    script = {"scenes": [{"index": i, "chapter_id": 1} for i in range(0, 10)]}
    (job / "script").mkdir()
    (job / "script" / "script.json").write_text(json.dumps(script), encoding="utf-8")

    # Empty pass list → no scene files, Flux must fill
    report = apply_vision_pass_heroes_to_job(job, [], script=script, max_heroes=5)
    assert report["ok"] is False
    assert report.get("reason") == "no_pass_locals"
    assert not list((job / "images").glob("scene_*.jpg"))
