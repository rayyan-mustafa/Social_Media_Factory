"""Unit tests for Weird Biology asset compositor (alpha paste, no white halos)."""

from __future__ import annotations

import unittest
from pathlib import Path

from PIL import Image

from src.services.weird_biology_assets import bootstrap_assets
from src.services.weird_biology_compositor import (
    ASSETS_DIR,
    composite_frame,
    load_asset,
    paste_layer,
    render_composited_still,
    clear_asset_cache,
)
from src.services.weird_biology_stickman import ShotSpec


class TestCompositorAlpha(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        clear_asset_cache()
        bootstrap_assets(force=True)

    def test_assets_are_rgba(self):
        head = load_asset("parts/adult_head_smile.png")
        self.assertEqual(head.mode, "RGBA")
        self.assertGreater(head.getchannel("A").getextrema()[1], 0)
        self.assertLess(head.getchannel("A").getextrema()[0], 255)

    def test_paste_layer_no_white_halo(self):
        """Transparent pixels must not become opaque white when pasted."""
        bg = Image.new("RGBA", (200, 200), (0xF2, 0xDA, 0xAE, 255))
        layer = Image.new("RGBA", (80, 80), (0, 0, 0, 0))
        # Opaque stroke circle in center only
        from PIL import ImageDraw

        draw = ImageDraw.Draw(layer)
        draw.ellipse((20, 20, 60, 60), fill=(0x11, 0x11, 0x11, 255))
        paste_layer(bg, layer, (60, 60))
        # Corner of paste bbox should stay cream (not white)
        px = bg.getpixel((60, 60))
        self.assertAlmostEqual(px[0], 0xF2, delta=5)
        self.assertAlmostEqual(px[1], 0xDA, delta=5)
        self.assertEqual(px[3], 255)

    def test_crib_door_composited_still(self):
        shot = ShotSpec(layout="crib_door_scene", bubble_bars=3, seed=42)
        img = render_composited_still(shot)
        self.assertEqual(img.size, (1280, 720))
        # Cream wall sample
        px = img.getpixel((10, 10))
        self.assertGreater(px[0], 200)
        self.assertGreater(px[1], 180)
        # Orange bubble bars present
        orange = (0xD0, 0x94, 0x2C)
        found = any(
            all(abs(img.getpixel((x, y))[i] - orange[i]) <= 24 for i in range(3))
            for y in range(80, 320, 4)
            for x in range(80, 500, 6)
        )
        self.assertTrue(found, "expected orange bubble-bar pixels")

    def test_composite_frame_layer_order(self):
        shot = ShotSpec(layout="crib_door_scene", bubble_bars=3, seed=1)
        from src.services.weird_biology_compositor import RigSpec

        rig = RigSpec.from_shot(shot)
        self.assertEqual(rig.layout, "crib_door_scene")
        z_vals = [layer.z for layer in rig.layers]
        self.assertEqual(min(z_vals), 10)
        self.assertEqual(max(z_vals), 80)
        self.assertTrue(any("crib_front" in layer.asset for layer in rig.layers))
        # composite_frame sorts by z at render time
        frame = composite_frame(rig)
        self.assertEqual(frame.size, (1280, 720))

    def test_asset_library_on_disk(self):
        self.assertTrue((ASSETS_DIR / "backgrounds/nursery_room.png").exists())
        self.assertTrue((ASSETS_DIR / "characters/adult_torso_walk.png").exists())


if __name__ == "__main__":
    unittest.main()
