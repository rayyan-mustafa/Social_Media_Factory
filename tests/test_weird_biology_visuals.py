"""Unit tests for Weird Biology stickman visual pipeline (no live LLM/Kokoro)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import src.services.weird_biology_prop_registry as prop_registry
from src.services.weird_biology_scenes import (
    build_visual_timeline,
    ensure_core_data_xray_slot,
    word_count,
)
from src.services.weird_biology_shot_plan import (
    PLANNER_MODEL,
    _enforce_xray,
    _rule_based_shots,
)
from src.services.weird_biology_stickman import (
    CharacterSpec,
    PROPS,
    ShotSpec,
    build_stickman_svg,
    render_stickman_frame,
)
from src.services.weird_biology_style import (
    CORAL,
    STYLE_REFERENCE_VIBRANT,
    kinetic_font_path,
    load_visual_config,
    palette_rgb,
    planner_model,
    resolve_style,
)


SAMPLE_SECTIONS = """
## SECTION 1: COLD HOOK

Look at your arms. Why do bumps rise. Myth says cold. Wrong. Nerves fire.

## SECTION 2: CONTEXT & SETUP

In nineteen ninety scientists watched. Old belief failed. Survival question remains.

## SECTION 3: CORE DATA & EXPERIMENT

A university ran a study with many subjects. They measured latency. Forty eight percent changed. The nerve is a telegraph wire under skin. Signals race.

## SECTION 4: DILEMMA / PHILOSOPHICAL DEBATE

Modern rooms hide an old trap. Ancestors needed this. Trade-offs remain without consent.

## SECTION 5: IMPACTFUL OUTRO

Feel that skin again. Soft beds cannot erase wild wiring. Survival beauty. What wakes you?
"""

SAMPLE_VO = (
    "Look at your arms. Why do bumps rise. Myth says cold. Wrong. Nerves fire. "
    "In nineteen ninety scientists watched. Old belief failed. Survival question remains. "
    "A university ran a study with many subjects. They measured latency. Forty eight percent changed. "
    "The nerve is a telegraph wire under skin. Signals race. "
    "Modern rooms hide an old trap. Ancestors needed this. Trade-offs remain without consent. "
    "Feel that skin again. Soft beds cannot erase wild wiring. Survival beauty. What wakes you?"
)


class TestStyleConfig(unittest.TestCase):
    def test_palette_teal_coral(self):
        pal = palette_rgb()
        self.assertEqual(pal["bg_teal"], (0x3E, 0x6B, 0x6B))
        self.assertEqual(pal["coral_accent"], (0xE8, 0x72, 0x4C))
        # Default brand palette stays teal (no earth-tone keys in default block)
        default_pal = load_visual_config().get("palette") or {}
        self.assertNotIn("earth", json.dumps(default_pal).lower())

    def test_palette_vibrant_warm_cream(self):
        pal = palette_rgb(STYLE_REFERENCE_VIBRANT)
        # Warm creamy yellow/peach wall (sampled from reference)
        self.assertAlmostEqual(pal["bg_teal"][0], 0xF2, delta=12)
        self.assertAlmostEqual(pal["bg_teal"][1], 0xDA, delta=20)
        self.assertGreater(pal["bg_teal"][0], pal["bg_teal"][2])  # warm, not teal
        self.assertIn("palette_vibrant", load_visual_config())
        self.assertEqual(
            resolve_style(None, layout="crib_door_scene"), STYLE_REFERENCE_VIBRANT
        )

    def test_planner_model_flash_lite(self):
        self.assertEqual(planner_model(), "google/gemini-2.5-flash-lite")
        self.assertEqual(PLANNER_MODEL, "google/gemini-2.5-flash-lite")

    def test_font_exists(self):
        self.assertTrue(kinetic_font_path().exists())


class TestDynamicPropRegistry(unittest.TestCase):
    def setUp(self):
        self._old_file = prop_registry.CUSTOM_PROPS_FILE
        self._old_cache = prop_registry._CUSTOM_PROPS_CACHE
        self._temp_dir = tempfile.TemporaryDirectory()
        prop_registry.CUSTOM_PROPS_FILE = (
            Path(self._temp_dir.name) / "custom_props.json"
        )
        prop_registry._CUSTOM_PROPS_CACHE = None

    def tearDown(self):
        prop_registry.CUSTOM_PROPS_FILE = self._old_file
        prop_registry._CUSTOM_PROPS_CACHE = self._old_cache
        self._temp_dir.cleanup()

    def test_register_search_and_render_custom_prop(self):
        code = (
            "bx, by = W * 0.75, H * 0.25\n"
            "parts.append(f'<circle cx=\"{bx:.1f}\" cy=\"{by:.1f}\" r=\"24\" "
            "fill=\"{fill}\" stroke=\"{stick}\" stroke-width=\"4\"/>')"
        )
        self.assertTrue(
            prop_registry.register_custom_prop(
                "cell_nucleus", "A simple cell nucleus", code, tags=["cell", "nucleus"]
            )
        )
        self.assertTrue(prop_registry.has_prop("cell_nucleus"))
        self.assertEqual(prop_registry.search_props(["nucleus"])[0][0], "cell_nucleus")
        parts = prop_registry.render_custom_prop(
            "cell_nucleus", palette_rgb(), 1280, 720, __import__("random").Random(42)
        )
        self.assertTrue(any("<circle" in part for part in parts))

    def test_rejects_unsafe_generated_code(self):
        self.assertFalse(
            prop_registry.register_custom_prop(
                "unsafe_prop", "must be rejected", "import os\nos.system('echo bad')"
            )
        )
        self.assertFalse(prop_registry.has_prop("unsafe_prop"))

    def test_planner_prop_list_stays_bounded(self):
        prop_registry._CUSTOM_PROPS_CACHE = {
            f"organ_{index}": {
                "description": f"organ {index}",
                "tags": ["organ"],
                "python_code": "parts.append('<circle cx=\"10\" cy=\"10\" r=\"2\"/>')",
            }
            for index in range(500)
        }
        props = prop_registry.get_planner_props("human organ", max_custom=12)
        self.assertLessEqual(
            len(props), len(prop_registry.BUILTIN_PROPS) + 12
        )


class TestStickmanRender(unittest.TestCase):
    def test_frame_palette_and_white_head(self):
        shot = ShotSpec(
            pose="stand",
            emotion="surprise",
            props=["door"],
            kinetic_text="48 PERCENT",
            xray=False,
            seed=1,
        )
        img = render_stickman_frame(shot)
        self.assertEqual(img.size, (1280, 720))
        # Sample corners should be teal-ish
        px = img.getpixel((10, 10))
        self.assertAlmostEqual(px[0], 0x3E, delta=8)
        self.assertAlmostEqual(px[1], 0x6B, delta=8)
        # Coral must appear when kinetic text set
        coral = (0xE8, 0x72, 0x4C)
        found_coral = any(
            img.getpixel((x, y)) == coral
            for y in range(20, 120, 4)
            for x in range(100, 1180, 8)
        )
        self.assertTrue(found_coral, "expected coral kinetic pixels")
        # White head region roughly center-left
        whites = sum(
            1
            for y in range(150, 280, 5)
            for x in range(480, 620, 5)
            if img.getpixel((x, y))[0] > 240
        )
        self.assertGreater(whites, 20)

    def test_svg_joint_rig_markup(self):
        from src.services.weird_biology_stickman import build_stickman_svg, build_skeleton

        shot = ShotSpec(pose="point", emotion="calm", seed=4)
        svg = build_stickman_svg(shot)
        self.assertIn("<svg", svg)
        self.assertIn("<path", svg)
        sk = build_skeleton("point", camera="wide", W=1280, H=720)
        for j in ("head", "neck", "hip", "wrist_r", "ankle_l"):
            self.assertIn(j, sk.joints)

    def test_xray_frame(self):
        shot = ShotSpec(pose="point", xray=True, internal="nerve", seed=2)
        img = render_stickman_frame(shot, phase=0.4)
        self.assertEqual(img.size, (1280, 720))

    def test_crib_door_dual_cast_svg(self):
        shot = ShotSpec(
            layout="crib_door_scene",
            bubble_bars=3,
            seed=42,
        )
        self.assertEqual(shot.resolved_style(), STYLE_REFERENCE_VIBRANT)
        svg = build_stickman_svg(shot)
        self.assertIn("<svg", svg)
        # Dual characters → multiple head fills
        self.assertGreaterEqual(svg.count('fill="#ffffff"'), 2)
        # Vibrant orange bars (reference amber) appear
        self.assertIn("#d0942c", svg.lower())
        # Cream wall fill present
        self.assertIn("#f2daae", svg.lower())
        self.assertIn("<ellipse", svg)  # bubble seam
        self.assertIn("<rect", svg)
        cast = shot.resolved_cast()
        self.assertEqual(len(cast), 2)
        roles = {c.role for c in cast}
        self.assertEqual(roles, {"baby", "adult"})
        self.assertIn("stroke-linecap=\"round\"", svg)
        img = render_stickman_frame(shot)
        self.assertEqual(img.size, (1280, 720))
        # Vibrant cream/peach wall (not teal)
        px = img.getpixel((10, 10))
        self.assertGreater(px[0], 200)
        self.assertGreater(px[1], 180)
        self.assertLess(px[2], px[0])  # warm, not cyan/teal
        # Orange accent appears in bubble bars
        orange = (0xD0, 0x94, 0x2C)
        found_orange = any(
            all(abs(img.getpixel((x, y))[i] - orange[i]) <= 18 for i in range(3))
            for y in range(80, 320, 4)
            for x in range(80, 500, 6)
        )
        self.assertTrue(found_orange, "expected orange bubble-bar pixels")

    def test_default_layout_stays_teal(self):
        shot = ShotSpec(pose="stand", emotion="neutral", seed=3)
        self.assertEqual(shot.resolved_style(), "default")
        img = render_stickman_frame(shot)
        px = img.getpixel((10, 10))
        self.assertAlmostEqual(px[0], 0x3E, delta=8)
        self.assertAlmostEqual(px[1], 0x6B, delta=8)

    def test_props_include_bubble(self):
        self.assertIn("bubble", PROPS)
        self.assertIn("room_corner", PROPS)

    def test_character_spec_from_dict(self):
        c = CharacterSpec.from_dict(
            {"role": "baby", "pose": "arms_up", "emotion": "surprise", "look_at": "adult"}
        )
        self.assertEqual(c.role, "baby")
        self.assertEqual(c.pose, "arms_up")


class TestTimeline(unittest.TestCase):
    def test_covers_duration_no_gaps(self):
        dur = 60.0
        tl = build_visual_timeline(
            SAMPLE_VO,
            script_sections_md=SAMPLE_SECTIONS,
            full_vo_duration_s=dur,
            topic="goosebumps",
        )
        self.assertGreater(len(tl.beats), 3)
        self.assertAlmostEqual(tl.beats[0].start_s, 0.0, places=2)
        self.assertAlmostEqual(tl.beats[-1].end_s, dur, places=2)
        for i in range(len(tl.beats) - 1):
            self.assertAlmostEqual(
                tl.beats[i].end_s, tl.beats[i + 1].start_s, places=2
            )
        self.assertTrue(any(b.section == 3 for b in tl.beats))
        self.assertIsNotNone(ensure_core_data_xray_slot(tl))

    def test_word_count(self):
        self.assertEqual(word_count("forty eight percent"), 3)


class TestXrayGate(unittest.TestCase):
    def test_enforce_xray(self):
        tl = build_visual_timeline(
            SAMPLE_VO,
            script_sections_md=SAMPLE_SECTIONS,
            full_vo_duration_s=45.0,
        )
        shots = _rule_based_shots(tl)
        # Strip xray then enforce
        for s in shots:
            s["xray"] = False
        fixed = _enforce_xray(shots, tl)
        self.assertTrue(any(s.get("xray") for s in fixed))


class TestComposeContractUnit(unittest.TestCase):
    def test_shot_spec_roundtrip(self):
        d = {
            "pose": "look_at_arms",
            "emotion": "surprise",
            "props": ["crib"],
            "kinetic_text": "NOW",
            "xray": True,
            "internal": "vessel",
            "camera": "wide",
            "layout": "crib_door_scene",
            "bubble_bars": 3,
            "cast": [
                {"role": "baby", "pose": "arms_up", "emotion": "surprise"},
                {"role": "adult", "pose": "enter_door", "emotion": "smile"},
            ],
        }
        spec = ShotSpec.from_dict(d, seed=9)
        self.assertEqual(spec.pose, "look_at_arms")
        self.assertTrue(spec.xray)
        self.assertEqual(spec.internal, "vessel")
        self.assertEqual(spec.layout, "crib_door_scene")
        self.assertEqual(spec.bubble_bars, 3)
        self.assertEqual(len(spec.cast or []), 2)
        self.assertEqual(spec.resolved_style(), STYLE_REFERENCE_VIBRANT)


if __name__ == "__main__":
    unittest.main()
