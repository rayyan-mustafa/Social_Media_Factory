from __future__ import annotations

import unittest

from src.services.weird_biology_compositor import render_scene
from src.services.weird_biology_scene_graph import SceneGraphError, SceneSpec


class TestSceneGraph(unittest.TestCase):
    def test_reference_style_scene_roundtrip_and_layer_order(self):
        scene = SceneSpec.from_dict(
            {
                "beat_index": 3,
                "beat_text": "The brain stays alert while the body rests.",
                "background": {"asset": "living_room"},
                "characters": [
                    {
                        "id": "reader",
                        "pose": "sitting_reading",
                        "x": 0.18,
                        "y": 0.58,
                        "z": 50,
                    },
                    {
                        "id": "sleeping_person",
                        "pose": "lying_down",
                        "x": 0.48,
                        "y": 0.55,
                        "z": 50,
                    },
                    {
                        "id": "observer",
                        "pose": "sitting",
                        "x": 0.82,
                        "y": 0.62,
                        "z": 50,
                    },
                ],
                "objects": [
                    {"id": "chair", "asset": "chair", "x": 0.18, "y": 0.68, "z": 20},
                    {"id": "book", "asset": "book", "x": 0.24, "y": 0.54, "z": 60},
                    {"id": "lamp", "asset": "lamp", "x": 0.82, "y": 0.25, "z": 20},
                ],
                "camera": {"shot": "wide", "focus_id": "sleeping_person"},
            }
        )

        self.assertEqual(len(scene.characters), 3)
        self.assertEqual(len(scene.objects), 3)
        self.assertEqual(scene.camera.focus_id, "sleeping_person")
        self.assertEqual([item.id for item in scene.layers()], [
            "chair",
            "lamp",
            "observer",
            "reader",
            "sleeping_person",
            "book",
        ])
        self.assertEqual(SceneSpec.from_dict(scene.to_dict()).beat_index, 3)

    def test_rejects_out_of_bounds_position(self):
        with self.assertRaises(SceneGraphError):
            SceneSpec.from_dict(
                {
                    "beat_text": "A biology beat.",
                    "characters": [{"id": "person", "x": 1.2}],
                }
            )

    def test_scene_graph_renders_a_single_beat(self):
        scene = SceneSpec.from_dict(
            {
                "beat_index": 0,
                "beat_text": "A warm nursery glows while a baby stares at the door.",
                "background": {"asset": "backgrounds/nursery_room.png"},
                "characters": [
                    {
                        "id": "baby",
                        "role": "baby",
                        "pose": "seated",
                        "x": 0.34,
                        "y": 0.62,
                        "z": 50,
                    }
                ],
                "objects": [
                    {"id": "door", "asset": "props/door_frame.png", "x": 0.72, "y": 0.18, "z": 20},
                    {"id": "crib", "asset": "props/crib_back.png", "x": 0.18, "y": 0.52, "z": 15},
                ],
                "camera": {"shot": "wide"},
            }
        )

        frame = render_scene(scene)
        self.assertEqual(frame.size, (1280, 720))
        self.assertIsNotNone(frame.getbbox())

    def test_rejects_unknown_camera_focus(self):
        with self.assertRaises(SceneGraphError):
            SceneSpec.from_dict(
                {
                    "beat_text": "A biology beat.",
                    "characters": [{"id": "person"}],
                    "camera": {"focus_id": "missing"},
                }
            )


if __name__ == "__main__":
    unittest.main()
