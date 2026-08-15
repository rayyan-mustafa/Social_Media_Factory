"""Tests for RMagine query distill + per-scene Wiki+Met fetch."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from PIL import Image


def test_distill_anne_boleyn_phases():
    from src.services.query_distill import distill_query_phases, extract_historical_figure

    phases = distill_query_phases("Anne Boleyn close up portrait in Tudor court")
    assert "Anne" in phases["phase1"] or "Boleyn" in phases["phase1"]
    assert "portrait" in phases["phase2"].lower()
    fig = extract_historical_figure("Anne Boleyn walks the Tower")
    assert fig == "Anne Boleyn"


def test_distill_manuscript_and_tower():
    from src.services.query_distill import distill_query_phases

    ms = distill_query_phases("a confession letter written with quill on parchment")
    assert "manuscript" in ms["phase1"].lower() or "letter" in ms["phase2"].lower()
    tower = distill_query_phases("execution at the scaffold axe tower")
    assert "Tower" in tower["phase1"] or "tower" in tower["phase3"].lower()


def test_force_symbolic_skips_portrait_priority():
    from src.services.query_distill import distill_query_phases, scene_search_bundle

    normal = distill_query_phases("Henry VIII in armor")
    symbolic = distill_query_phases("Henry VIII in armor", force_symbolic_broll=True)
    assert normal["phase1"]
    assert symbolic["phase1"]
    bundle = scene_search_bundle(
        text="Henry VIII returns",
        visual_prompt="king on throne",
        force_symbolic_broll=True,
    )
    assert bundle["historical_figure"] is None
    assert len(bundle["phase_list"]) == 4


def test_rmagine_places_pass_leaves_reject_for_flux(tmp_path: Path, monkeypatch):
    from src.services import rmagine_scene_fetch as rsf

    job = tmp_path / "job"
    (job / "script").mkdir(parents=True)
    (job / "images").mkdir(parents=True)
    script = {
        "topic": "Tudor test",
        "scenes": [
            {
                "index": 0,
                "text": "Anne Boleyn enters the court.",
                "visual_prompt": "Anne Boleyn portrait",
            },
            {
                "index": 1,
                "text": "A sealed letter arrives.",
                "visual_prompt": "Tudor manuscript letter",
            },
        ],
    }
    (job / "script" / "script.json").write_text(json.dumps(script), encoding="utf-8")

    pass_img = tmp_path / "pass.jpg"
    Image.new("RGB", (640, 480), (10, 20, 30)).save(pass_img, format="JPEG")

    def fake_search(keyword, asset_type, save_dir, **kwargs):
        save_dir = Path(save_dir)
        approved_dir = save_dir / "approved"
        approved_dir.mkdir(parents=True, exist_ok=True)
        dest = approved_dir / "cand.jpg"
        dest.write_bytes(pass_img.read_bytes())
        return {
            "approved": [
                {
                    "local_path": str(dest),
                    "source_url": "https://example.com/x.jpg",
                    "title": keyword,
                    "metadata": {"license_tag": "Public Domain"},
                }
            ],
            "rejected": [],
        }

    def fake_vision(ref, scene_text, historical_figure=None, settings=None):
        # First scene PASS, second REJECT → Flux gap
        if "Anne" in (scene_text or "") or "Boleyn" in (scene_text or ""):
            return {"status": "PASS", "confidence": 9, "reason": "match"}
        return {"status": "FAIL", "confidence": 2, "reason": "no"}

    monkeypatch.setattr(rsf, "search_and_stage", fake_search)
    monkeypatch.setattr(rsf, "validate_image_with_vision_llm", fake_vision)

    report = rsf.fetch_archival_for_all_scenes(job, max_workers=2, skip_if_stamped=False)
    assert report["ok"] is True
    assert report["n_scenes"] == 2
    assert report["policy"] == "per_scene_wiki_met_primary_flux_on_reject"
    assert report["no_hero_product_cap"] is True
    assert report["n_pass"] == 1
    assert report["n_flux_gaps"] == 1
    assert (job / "images" / "scene_000.jpg").is_file()
    assert not (job / "images" / "scene_001.jpg").is_file()
    stamp = job / "assets" / "fetched" / "vision_judge.json"
    assert stamp.is_file()


def test_rmagine_skip_if_stamped(tmp_path: Path):
    from src.services.rmagine_scene_fetch import fetch_archival_for_all_scenes

    job = tmp_path / "job2"
    stage = job / "assets" / "fetched"
    stage.mkdir(parents=True)
    prior = {
        "ok": True,
        "policy": "per_scene_wiki_met_primary_flux_on_reject",
        "n_pass": 5,
    }
    (stage / "vision_judge.json").write_text(json.dumps(prior), encoding="utf-8")
    out = fetch_archival_for_all_scenes(job, skip_if_stamped=True)
    assert out.get("n_pass") == 5


def test_asset_fetcher_caps_are_not_product_truncators():
    from src.services.asset_fetcher import KEYWORD_STAGE_MAX, VISION_PASS_HEROES_MAX

    assert KEYWORD_STAGE_MAX >= 50
    assert VISION_PASS_HEROES_MAX >= 1000
