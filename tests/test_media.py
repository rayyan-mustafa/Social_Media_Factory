"""Media ranking helpers without requiring CLIP GPU."""

from pathlib import Path

from src.services.media_retrieval import MediaRetrievalService


def test_placeholder_image(tmp_path: Path):
    svc = MediaRetrievalService(use_clip=False)
    out = tmp_path / "ph.jpg"
    svc.placeholder_image(out)
    assert out.exists()
    assert out.stat().st_size > 0
