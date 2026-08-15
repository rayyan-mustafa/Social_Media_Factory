"""Tests for QUALITY FREEZE derivatives + image reuse packs."""

from __future__ import annotations

import json
from pathlib import Path

from src.content.derivatives import (
    ALLOWED_UNDER_FREEZE,
    describe_derivative_plan,
    is_module_allowed,
)
from src.content.image_reuse import build_image_reuse_pack, list_scene_images


def test_freeze_allows_pinterest_blocks_podcast():
    assert is_module_allowed("pinterest_pins") is True
    assert is_module_allowed("image_reuse_stills") is True
    assert is_module_allowed("podcast_audio") is False
    assert is_module_allowed("tiktok_shorts_text") is False
    assert is_module_allowed("podcast_audio", freeze_video_heavy=False) is True
    plan = describe_derivative_plan()
    assert plan["quality_freeze"] is True
    assert "pinterest_pins" in plan["allowed_under_freeze"]
    assert "podcast_audio" in plan["frozen_modules"]


def test_build_image_reuse_pack(tmp_path: Path):
    img_dir = tmp_path / "images"
    img_dir.mkdir()
    for i in range(5):
        (img_dir / f"scene_{i:03d}.jpg").write_bytes(b"fakejpg")
    script_dir = tmp_path / "script"
    script_dir.mkdir()
    (script_dir / "script.json").write_text(
        json.dumps(
            {
                "title": "What If Anne Survived?",
                "hook": "A sealed letter changes Tudor succession.",
            }
        ),
        encoding="utf-8",
    )
    images = list_scene_images(tmp_path, limit=3)
    assert len(images) == 3
    manifest = build_image_reuse_pack(
        tmp_path,
        video_id="abc123",
        channel="napstorian",
        max_images=4,
    )
    assert manifest["n_images"] == 4
    assert manifest["youtube_url"].endswith("abc123")
    assert (tmp_path / "derivatives" / "image_reuse" / "pinterest.json").exists()
    assert (tmp_path / "derivatives" / "image_reuse" / "pinterest.csv").exists()
    assert (tmp_path / "derivatives" / "image_reuse" / "instagram_carousel.json").exists()
    assert (tmp_path / "derivatives" / "image_reuse" / "facebook_post.json").exists()
    assert "pinterest_pins" in ALLOWED_UNDER_FREEZE
