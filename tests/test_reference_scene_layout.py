from __future__ import annotations

import unittest
from pathlib import Path

from src.services.reference_scene_layout import SceneLayout, infer_layout, render_scene_layout


class TestReferenceSceneLayout(unittest.TestCase):
    def test_infer_layout_has_reasonable_parameters(self):
        layout = infer_layout(None, width=1280, height=720)
        self.assertEqual(layout.width, 1280)
        self.assertEqual(layout.height, 720)
        self.assertGreater(layout.table["width"], 0.5)
        self.assertEqual(len(layout.chairs), 2)
        self.assertGreater(layout.adult.head_r, 0.05)
        self.assertLess(layout.child.cx, layout.adult.cx)

    def test_render_scene_layout_creates_real_png(self):
        layout = SceneLayout(width=1280, height=720)
        out = Path("output/test_reference_scene_layout.png")
        img = render_scene_layout(layout, out_path=out)
        self.assertEqual(img.size, (1280, 720))
        self.assertTrue(out.exists())
        self.assertGreater(out.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
