from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from src.services.reference_to_image import (
    Img2ImgConfig,
    generate_reference_image,
)


class TestReferenceToImage(unittest.TestCase):
    def test_uses_pillow_when_provider_is_not_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "render.png"
            path, backend = generate_reference_image(
                None,
                out,
                prompt="a child and adult at a table",
                width=640,
                height=360,
                config=None,
            )
            self.assertEqual(path, out)
            self.assertEqual(backend, "pillow_fallback")
            self.assertEqual(Image.open(out).size, (640, 360))

    def test_config_reads_remote_provider(self):
        config = Img2ImgConfig(endpoint="https://example.invalid/img2img", strength=0.25)
        self.assertEqual(config.endpoint, "https://example.invalid/img2img")
        self.assertEqual(config.strength, 0.25)


if __name__ == "__main__":
    unittest.main()
