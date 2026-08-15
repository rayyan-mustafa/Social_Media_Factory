"""Napstorian 10–15 / historian soft 4–6 PD motion clip path + commercial license gate."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from src.services.pd_clippings import (
    apply_pd_motion_clips_to_job,
    license_is_commercial_ok,
    synthesize_motion_from_still,
)


def _still(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1280, 720), (40, 30, 20)).save(path, format="JPEG", quality=90)


def test_license_gate_allows_pd_rejects_nc():
    assert license_is_commercial_ok("PD-Art (Wikimedia Commons)")
    assert license_is_commercial_ok("PD (Wikimedia Commons)")
    assert license_is_commercial_ok("CC0")
    assert license_is_commercial_ok("CC0 (Met Open Access)")
    assert license_is_commercial_ok("Public Domain")
    assert license_is_commercial_ok("US government work")
    assert not license_is_commercial_ok("CC BY-NC 4.0")
    assert not license_is_commercial_ok("CC BY-ND")
    assert not license_is_commercial_ok("fair use")
    assert not license_is_commercial_ok("check Commons file page")
    assert not license_is_commercial_ok("Pexels License")
    assert not license_is_commercial_ok("")



def test_synthesize_motion_from_still(tmp_path: Path):
    still = tmp_path / "hero.jpg"
    _still(still)
    out = tmp_path / "motion.mp4"
    synthesize_motion_from_still(still, out, duration_s=3.0)
    assert out.is_file() and out.stat().st_size > 1000


def test_dropin_without_license_sidecar_blocked(tmp_path: Path):
    job = tmp_path / "job"
    images = job / "images"
    raw = images / "pd_motion" / "raw"
    raw.mkdir(parents=True)
    # Fake tiny "video" won't prepare — use still motion path instead via curated.
    # Place a drop-in mp4 without sidecar; should be blocked, curated still fills.
    bad = raw / "risky.mp4"
    bad.write_bytes(b"not-a-real-video-but-big-enough-" + b"x" * 3000)
    for name in (
        "mary_i_portrait",
        "james_v_portrait",
        "henry_viii_portrait",
        "tudor_map",
        "catherine_of_aragon",
        "met_henry_viii_field_armor",
    ):
        _still(images / "pd_heroes" / "fitted" / f"{name}.jpg")

    for i in range(0, 40, 5):
        _still(images / f"scene_{i:03d}.jpg")
    (job / "script").mkdir()
    (job / "script" / "script.json").write_text(
        json.dumps(
            {
                "scenes": [
                    {"index": i, "chapter_id": (i // 5) + 1, "text": "x"}
                    for i in range(0, 40, 5)
                ]
            }
        ),
        encoding="utf-8",
    )
    (images / "visual_manifest.json").write_text(
        json.dumps(
            {
                "scenes": [
                    {
                        "index": i,
                        "path": str(images / f"scene_{i:03d}.jpg"),
                        "width": 1280,
                        "height": 720,
                        "visual_prompt": "x",
                    }
                    for i in range(0, 40, 5)
                ],
                "meta": {},
            }
        ),
        encoding="utf-8",
    )
    out = apply_pd_motion_clips_to_job(
        job, channel="napstorian", min_clips=2, max_clips=4, duration_s=3.0
    )
    assert out.get("ok") is True
    side = json.loads(
        (images / "pd_motion" / "pd_motion_clips.json").read_text(encoding="utf-8")
    )
    assert any(b.get("reason") == "missing_license_sidecar" for b in side.get("blocked") or [])
    for c in out.get("clips") or []:
        assert c.get("commercial_ok") is True
        assert license_is_commercial_ok(c.get("license"))


def test_apply_pd_motion_both_channels(tmp_path: Path):
    job = tmp_path / "job"
    images = job / "images"
    images.mkdir(parents=True)
    for i in range(0, 40, 5):
        _still(images / f"scene_{i:03d}.jpg")
    fitted = images / "pd_heroes" / "fitted"
    for name in (
        "mary_i_portrait",
        "james_v_portrait",
        "henry_viii_portrait",
        "tudor_map",
        "catherine_of_aragon",
        "met_henry_viii_field_armor",
        "scotland_map",
        "letter_seal",
        "edinburgh_castle",
        "holyrood",
        "ortelius_britain",
        "anne_boleyn_portrait",
        "elizabeth_i_portrait",
        "thomas_more_holbein",
        "margaret_tudor",
    ):
        _still(fitted / f"{name}.jpg")

    script = {
        "scenes": [
            {"index": i, "chapter_id": (i // 5) + 1, "text": "x"}
            for i in range(0, 40, 5)
        ]
    }
    (job / "script").mkdir()
    (job / "script" / "script.json").write_text(json.dumps(script), encoding="utf-8")
    vm = {
        "scenes": [
            {
                "index": i,
                "path": str(images / f"scene_{i:03d}.jpg"),
                "width": 1280,
                "height": 720,
                "visual_prompt": "x",
            }
            for i in range(0, 40, 5)
        ],
        "meta": {},
    }
    (images / "visual_manifest.json").write_text(json.dumps(vm), encoding="utf-8")

    hist = apply_pd_motion_clips_to_job(
        job, channel="napping_historian", min_clips=2, max_clips=4, duration_s=3.0
    )
    assert hist.get("ok") is True
    assert hist.get("soft") is True
    assert int(hist.get("pd_motion_count") or 0) >= 2

    out = apply_pd_motion_clips_to_job(
        job, channel="napstorian", min_clips=4, max_clips=6, duration_s=3.0, force=True
    )
    assert out.get("ok") is True
    assert int(out.get("pd_motion_count") or 0) >= 4
    stamp = json.loads((job / "pd_clippings.json").read_text(encoding="utf-8"))
    assert stamp.get("pd_motion_clips") is True
    assert stamp.get("commercial_use_only") is True
    vm2 = json.loads((images / "visual_manifest.json").read_text(encoding="utf-8"))
    motion_scenes = [s for s in vm2["scenes"] if s.get("pd_motion")]
    assert len(motion_scenes) >= 4
    for s in motion_scenes:
        assert Path(s["video_path"]).is_file()
    for c in out.get("clips") or []:
        assert c.get("commercial_ok") is True
        assert c.get("youtube_monetization_safe") is True
        assert c.get("source_url")


def test_apply_pd_motion_respects_max_scene_index(tmp_path: Path):
    """SOP ~10m heads must land motion inserts inside the cut window."""
    job = tmp_path / "job"
    images = job / "images"
    images.mkdir(parents=True)
    fitted = images / "pd_heroes" / "fitted"
    for name in (
        "mary_i_portrait",
        "james_v_portrait",
        "henry_viii_portrait",
        "tudor_map",
        "catherine_of_aragon",
        "met_henry_viii_field_armor",
    ):
        _still(fitted / f"{name}.jpg")
    for i in range(0, 120):
        _still(images / f"scene_{i:03d}.jpg")
    (job / "script").mkdir()
    (job / "script" / "script.json").write_text(
        json.dumps(
            {
                "scenes": [
                    {"index": i, "chapter_id": (i // 30) + 1, "text": "x"}
                    for i in range(0, 120)
                ]
            }
        ),
        encoding="utf-8",
    )
    scenes = [
        {
            "index": i,
            "path": str(images / f"scene_{i:03d}.jpg"),
            "width": 1280,
            "height": 720,
            "visual_prompt": "x",
        }
        for i in range(0, 69)
    ]
    (images / "visual_manifest.json").write_text(
        json.dumps({"scenes": scenes, "meta": {}}), encoding="utf-8"
    )
    (images / "visual_manifest_sop_10m.json").write_text(
        json.dumps({"scenes": scenes, "meta": {}}), encoding="utf-8"
    )

    out = apply_pd_motion_clips_to_job(
        job,
        channel="napstorian",
        min_clips=2,
        max_clips=4,
        duration_s=2.0,
        max_scene_index=68,
        force=True,
    )
    assert out.get("ok") is True
    assert int(out.get("pd_motion_count") or 0) >= 2
    for c in out.get("clips") or []:
        assert 3 <= int(c["scene_index"]) <= 68
        assert c.get("commercial_ok") is True
    sop = json.loads((images / "visual_manifest_sop_10m.json").read_text(encoding="utf-8"))
    motion = [s for s in sop["scenes"] if s.get("pd_motion")]
    assert len(motion) >= 2
    assert all(3 <= int(s["index"]) <= 68 for s in motion)
