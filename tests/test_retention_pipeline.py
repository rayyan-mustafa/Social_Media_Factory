"""Tests for retention pipeline modules."""

from __future__ import annotations

from src.domain.models import ExpandedSentence, Scene
from src.services.retention_profile import (
    active_profile_name,
    merge_profile_into_script_settings,
)
from src.services.script_validate import validate_chapter_sentences
from src.services.voice_prosody import prosody_for_scene


def test_validate_chapter_rejects_visual_marker():
    sents = [
        ExpandedSentence(
            text="The final visual should be a slow zoom on the portrait.",
            visual_prompt="portrait still",
        )
    ]
    result = validate_chapter_sentences(sents, chapter_id=1)
    assert not result.ok
    assert any("visual marker" in e for e in result.errors)


def test_validate_chapter_warns_hook_density():
    sents = [
        ExpandedSentence(
            text=" ".join(["word"] * 18) + ".",
            visual_prompt="still",
        )
        for _ in range(5)
    ]
    result = validate_chapter_sentences(sents, chapter_id=1, hook_chapter=True)
    assert result.ok
    assert any("median words" in w for w in result.warnings)


def test_prosody_hook_chapter_faster():
    scene = Scene(
        index=0,
        text="What if everything changed?",
        visual_prompt="still",
        chapter_id=1,
        pacing_phase="a",
    )
    p = prosody_for_scene(scene, base_speed=0.90, next_chapter_id=1)
    assert p.speed >= 0.90
    assert p.pause_after_ms >= 150


def test_prosody_chapter_boundary_pause():
    scene = Scene(
        index=5,
        text="And then the court fell silent.",
        visual_prompt="still",
        chapter_id=2,
        pacing_phase="a",
    )
    p = prosody_for_scene(scene, base_speed=0.90, next_chapter_id=3)
    assert p.is_chapter_end
    assert p.pause_after_ms <= 200
    assert p.pause_after_ms >= 150


def test_prosody_chapter_end_does_not_stack_interrupt():
    scene = Scene(
        index=5,
        text="Everything changed!",
        visual_prompt="still",
        chapter_id=2,
        pacing_phase="a",
        beat_type="interrupt",
    )
    p = prosody_for_scene(
        scene, base_speed=0.90, next_chapter_id=3, chapter_end_pause_ms=200
    )
    assert p.is_chapter_end
    assert p.pause_after_ms == 200


def test_prosody_reengage_light_pause():
    scene = Scene(
        index=40,
        text="So ask yourself — would the succession have held?",
        visual_prompt="still",
        chapter_id=6,
        pacing_phase="a",
        beat_type="reengage",
    )
    p = prosody_for_scene(scene, base_speed=0.90, next_chapter_id=6)
    assert not p.is_chapter_end
    assert p.pause_after_ms >= 150
    assert p.pause_after_ms <= 200


def test_retention_profile_merge_defaults():
    merged = merge_profile_into_script_settings({})
    assert merged.get("_retention_profile") == active_profile_name()
    assert "target_scenes" in merged
