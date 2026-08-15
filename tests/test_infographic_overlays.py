"""Tests for code 2D infographic + $0 premium documentary overlays."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from src.services.premium_overlays import (
    channel_motion_style,
    extract_entity_cue,
    plan_premium_overlays,
    render_cold_open_title,
    render_fog_layer,
    render_history_vs_what_if,
    render_map_card,
    render_quote_card,
)
from src.services.visual_modalities import (
    plan_infographic_overlays,
    profile_enabled,
    render_infographic_card,
)


def test_infographic_profile_enabled():
    assert profile_enabled("infographic") is True
    assert profile_enabled("premium_overlays_v1") is True
    assert profile_enabled("toonbee") is False
    assert profile_enabled("blender_3d") is False


def test_channel_motion_tone():
    nap = channel_motion_style("napstorian")
    hist = channel_motion_style("napping_historian")
    assert nap.allow_what_if_split is True
    assert hist.allow_what_if_split is False
    assert hist.zoom_end < nap.zoom_end
    assert hist.fade_soft is True


def test_render_infographic_card(tmp_path: Path):
    out = tmp_path / "card.png"
    path = render_infographic_card(
        "The Divergence Point",
        ["A sealed letter arrives", "The court divides", "History bends"],
        out_path=out,
        style="what_if",
    )
    assert path.is_file()
    img = Image.open(path)
    assert img.size == (1280, 720)


def test_premium_renderers(tmp_path: Path):
    fog = render_fog_layer(tmp_path / "fog.png")
    assert Image.open(fog).mode == "RGBA"
    mp = render_map_card(
        "Campaign",
        ["Vienna", "Belgrade", "Istanbul"],
        out_path=tmp_path / "map.png",
    )
    assert Image.open(mp).size == (1280, 720)
    qt = render_quote_card(
        "The wax seal broke before dawn.",
        out_path=tmp_path / "quote.png",
    )
    assert qt.is_file()
    sp = render_history_vs_what_if(
        "Mary stays in England",
        "Mary wed James V",
        out_path=tmp_path / "split.png",
    )
    assert sp.is_file()
    cold = render_cold_open_title(
        "What If a Tudor Queen Married a Scottish King?",
        out_path=tmp_path / "cold.png",
        subtitle="A sealed letter unanswered",
    )
    assert cold.is_file()
    assert extract_entity_cue("Anne Boleyn walked into the hall") == (
        "Anne Boleyn",
        "Queen Consort",
    )


def test_plan_infographic_overlays_from_chapters(tmp_path: Path):
    script = {
        "title": "What If Test",
        "outline": {
            "chapters": [
                {"id": 1, "title": "The Hook", "goal": "grab", "key_points": ["a"]},
                {
                    "id": 2,
                    "title": "Historical Context",
                    "goal": "set stage",
                    "key_points": ["b", "c"],
                },
                {
                    "id": 3,
                    "title": "The Divergence Point",
                    "goal": "fork",
                    "key_points": ["d"],
                },
                {
                    "id": 4,
                    "title": "Immediate Aftermath",
                    "goal": "shock",
                    "key_points": ["e"],
                },
                {
                    "id": 5,
                    "title": "Mid-Term Shift",
                    "goal": "drift",
                    "key_points": ["f"],
                },
                {
                    "id": 6,
                    "title": "Pattern Interrupt",
                    "goal": "wake",
                    "key_points": ["g"],
                },
                {
                    "id": 7,
                    "title": "Long-term Consequences",
                    "goal": "scale",
                    "key_points": ["h"],
                },
            ]
        },
        "scenes": [
            {"index": 0, "chapter_id": 1, "text": "hook"},
            {"index": 10, "chapter_id": 2, "text": "ctx"},
            {"index": 20, "chapter_id": 3, "text": "div"},
            {"index": 30, "chapter_id": 4, "text": "aft"},
            {"index": 40, "chapter_id": 5, "text": "mid"},
            {"index": 50, "chapter_id": 6, "text": "pi"},
            {"index": 60, "chapter_id": 7, "text": "long"},
        ],
    }
    planned = plan_infographic_overlays(
        script,
        out_dir=tmp_path / "cards",
        min_cards=3,
        max_cards=6,
        estimated_duration_s=400.0,
        target_interval_s=90.0,
    )
    assert 3 <= len(planned) <= 6
    # Hook chapter skipped
    assert all(ov.chapter_id != 1 for ov in planned)
    assert all(Path(ov.card_path).is_file() for ov in planned)


def test_plan_infographic_netflix_density(tmp_path: Path):
    """~23 min doc should plan denser than sparse 6-plate farm default."""
    chapters = [
        {"id": i, "title": f"Chapter {i}", "goal": f"g{i}", "key_points": [f"p{i}"]}
        for i in range(1, 11)
    ]
    scenes = []
    for i in range(125):
        cid = min(10, 1 + i // 12)
        scenes.append(
            {
                "index": i,
                "chapter_id": cid,
                "text": f"Beat {i} in 1524 with border map" if i % 11 == 0 else f"Scene {i}",
            }
        )
    script = {
        "title": "What If a Tudor Queen Married a Scottish King?",
        "outline": {"chapters": chapters},
        "scenes": scenes,
    }
    planned = plan_infographic_overlays(
        script,
        out_dir=tmp_path / "dense",
        min_cards=8,
        max_cards=20,
        target_interval_s=75.0,
        estimated_duration_s=1380.0,
    )
    assert len(planned) >= 12
    assert len(planned) <= 20
    assert all(Path(ov.card_path).is_file() for ov in planned)


def test_plan_premium_overlays_pack(tmp_path: Path):
    script = {
        "title": "What If a Tudor Queen Married a Scottish King?",
        "hook": "A sealed letter from James V sits unanswered.",
        "meta": {"channel": "napstorian"},
        "outline": {
            "chapters": [
                {
                    "id": 1,
                    "title": "The Hook",
                    "goal": "grab",
                    "key_points": ["wax seal 1524"],
                },
                {
                    "id": 2,
                    "title": "Historical Context",
                    "goal": "Map of Europe in 1524",
                    "key_points": [
                        "Map of Europe showing the French noose",
                        "December 1524",
                    ],
                },
                {
                    "id": 3,
                    "title": "The Divergence Point",
                    "goal": "fork",
                    "key_points": ["Mary Tudor wed James V"],
                },
                {
                    "id": 4,
                    "title": "Immediate Aftermath",
                    "goal": "shock",
                    "key_points": ["border march"],
                },
            ]
        },
        "scenes": [
            {
                "index": 0,
                "chapter_id": 1,
                "text": "The wax seal of James the Fifth crumbled.",
            },
            {
                "index": 10,
                "chapter_id": 2,
                "text": "Anne Boleyn watches the map of Europe.",
                "visual_prompt": "campaign map borders",
            },
            {
                "index": 20,
                "chapter_id": 3,
                "text": "Mary Tudor takes the Scottish road.",
            },
            {
                "index": 30,
                "chapter_id": 4,
                "text": "Armies march toward the border.",
                "visual_prompt": "march line campaign",
            },
        ],
    }
    plan = plan_premium_overlays(
        script, out_dir=tmp_path / "premium", channel="napstorian"
    )
    assert plan.pack == "premium_overlays_v1"
    assert plan.channel == "napstorian"
    assert plan.cold_open_path and Path(plan.cold_open_path).is_file()
    assert plan.fog_png and Path(plan.fog_png).is_file()
    assert plan.infographic_overlays
    styles = {o.get("style") for o in plan.infographic_overlays}
    assert "history_vs_what_if" in styles or "quote" in styles or "map" in styles
    chrome = plan.chrome_by_scene()
    assert 0 in chrome
    assert chrome[0].year_stamp == "1524" or chrome[10].year_stamp == "1524"
    assert any(c.fog for c in plan.scene_chrome)
    assert any(c.lower_third_name for c in plan.scene_chrome)
    assert (tmp_path / "premium" / "premium_plan.json").is_file()

    hist = plan_premium_overlays(
        {**script, "meta": {"channel": "napping_historian"}},
        out_dir=tmp_path / "premium_hist",
        channel="napping_historian",
    )
    assert hist.channel == "napping_historian"
    assert not any(
        o.get("style") == "history_vs_what_if" for o in hist.infographic_overlays
    )


def test_playlist_force_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.streaming import vod_picker as vp

    monkeypatch.setattr(vp, "OPS", tmp_path)
    monkeypatch.setattr(vp, "PLAYLIST_FORCE_LOCK", tmp_path / "live_playlist_force_lock.json")
    monkeypatch.setattr(vp, "STREAM_CFG", tmp_path / "streaming")
    (tmp_path / "streaming").mkdir()
    head = tmp_path / "final.mp4"
    head.write_bytes(b"\x00" * 100)

    lock = vp.write_playlist_force_lock(
        heads={"napstorian": str(head), "napping_historian": str(head)},
        reason="unit_test",
    )
    assert lock["locked"] is True
    assert vp.read_playlist_force_lock() is not None

    # refresh_playlists should honor lock (rank_vods may find nothing else)
    monkeypatch.setattr(vp, "rank_vods", lambda *a, **k: [])
    out = vp.refresh_playlists(channels=["napstorian"], dry_run=False)
    assert out.get("force_lock") is True
    assert out["channels"]["napstorian"]["ok"] is True
    pl = (tmp_path / "streaming" / "playlist_napstorian.txt").read_text(encoding="utf-8")
    assert "FORCE-LOCKED" in pl
    assert str(head.resolve()) in pl


def test_is_new_format_final(tmp_path: Path):
    from src.streaming import vod_picker as vp

    old = tmp_path / "old" / "video"
    old.mkdir(parents=True)
    final_old = old / "final.mp4"
    final_old.write_bytes(b"\x00" * 50)
    (old / "edit_manifest.json").write_text(
        json.dumps({"meta": {"compose_infographic_overlays_enabled": False}}),
        encoding="utf-8",
    )
    assert vp.is_new_format_final(final_old) is False

    new = tmp_path / "new" / "video"
    new.mkdir(parents=True)
    final_new = new / "final.mp4"
    final_new.write_bytes(b"\x00" * 50)
    (new / "edit_manifest.json").write_text(
        json.dumps(
            {
                "meta": {
                    "compose_infographic_overlays_enabled": True,
                    "selected_pack": True,
                    "premium_overlays_v1": True,
                    "infographic_overlays": [
                        {"scene_index": 10, "title": "Context"}
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    assert vp.is_new_format_final(final_new) is True


def test_compose_premium_env_default():
    from src.services.settings import get_settings

    get_settings.cache_clear()
    s = get_settings()
    assert s.compose_premium_overlays_enabled is True
    assert s.compose_infographic_overlays_enabled is True


def test_plan_premium_overlays_respects_truncated_scene_indices(tmp_path: Path):
    """Live-test / truncated composes must not plan plates past the cut."""
    script = {
        "title": "What If a Tudor Queen Married a Scottish King?",
        "hook": "A sealed letter from James V sits unanswered.",
        "meta": {"channel": "napstorian"},
        "outline": {
            "chapters": [
                {"id": 1, "title": "The Hook", "goal": "grab", "key_points": ["wax"]},
                {
                    "id": 2,
                    "title": "Historical Context",
                    "goal": "map Europe",
                    "key_points": ["Map of Europe", "1524"],
                },
                {
                    "id": 3,
                    "title": "The Divergence Point",
                    "goal": "fork",
                    "key_points": ["Mary wed James"],
                },
                {
                    "id": 4,
                    "title": "Immediate Aftermath",
                    "goal": "shock",
                    "key_points": ["border march"],
                },
                {
                    "id": 5,
                    "title": "Long Tail Past Cut",
                    "goal": "late",
                    "key_points": ["scene 100 only"],
                },
            ]
        },
        "scenes": [
            {"index": i, "chapter_id": min(5, 1 + i // 20), "text": f"Beat {i} in 1524."}
            for i in range(0, 125)
        ],
    }
    cut = list(range(0, 69))
    plan = plan_premium_overlays(
        script,
        out_dir=tmp_path / "premium_trunc",
        channel="napstorian",
        min_cards=8,
        max_cards=20,
        scene_indices=cut,
        target_interval_s=75.0,
        estimated_duration_s=605.0,
    )
    idxs = [int(o["scene_index"]) for o in plan.infographic_overlays]
    assert idxs
    assert all(0 <= i <= 68 for i in idxs), idxs
    assert len(idxs) >= 8
    assert all(int(c.scene_index) <= 68 for c in plan.scene_chrome)
