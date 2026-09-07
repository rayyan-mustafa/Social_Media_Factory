"""SVG DOM cleanup — the fixes roadmap.txt's own cleaner does not make."""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from src.microstock.svg_cleaner import SvgCleanError, clean_svg

FIXTURE = Path(__file__).parent / "fixtures" / "microstock_dirty.svg"


def _localnames(path: Path) -> list[str]:
    root = etree.parse(str(path)).getroot()
    return [e.tag.rsplit("}", 1)[-1] for e in root.iter() if isinstance(e.tag, str)]


@pytest.fixture()
def cleaned(tmp_path: Path) -> Path:
    out = tmp_path / "clean.svg"
    clean_svg(FIXTURE, out)
    return out


def test_strips_nested_rasters_not_just_top_level(cleaned: Path):
    """The fixture hides an <image> two levels deep — the roadmap's code misses it."""
    assert "image" not in _localnames(cleaned)


def test_strips_scripts_and_foreign_objects(cleaned: Path):
    names = _localnames(cleaned)
    assert "script" not in names
    assert "foreignObject" not in names


def test_strips_metadata_and_title(cleaned: Path):
    names = _localnames(cleaned)
    assert "metadata" not in names
    assert "title" not in names


def test_removes_embedded_base64_data_uris(cleaned: Path):
    assert "data:image" not in cleaned.read_text(encoding="utf-8")


def test_normalises_viewbox_and_dimensions(cleaned: Path):
    root = etree.parse(str(cleaned)).getroot()
    assert root.get("viewBox") == "0 0 1024 1024"
    assert root.get("width") == "1024px"


def test_removes_full_frame_background(tmp_path: Path):
    out = tmp_path / "c.svg"
    report = clean_svg(FIXTURE, out)
    assert report.backgrounds_removed == 1
    assert "rect" not in _localnames(out)


def test_real_artwork_paths_survive(cleaned: Path):
    assert _localnames(cleaned).count("path") == 2


def test_simplification_reduces_anchors(tmp_path: Path):
    report = clean_svg(FIXTURE, tmp_path / "c.svg")
    assert report.anchors_after < report.anchors_before
    assert report.node_reduction_pct > 0


def test_epsilon_zero_disables_simplification(tmp_path: Path):
    report = clean_svg(FIXTURE, tmp_path / "c.svg", overrides={"simplify_epsilon": 0})
    assert report.anchors_after == report.anchors_before


def test_larger_epsilon_reduces_more(tmp_path: Path):
    gentle = clean_svg(FIXTURE, tmp_path / "a.svg", overrides={"simplify_epsilon": 0.5})
    harsh = clean_svg(FIXTURE, tmp_path / "b.svg", overrides={"simplify_epsilon": 50.0})
    assert harsh.anchors_after <= gentle.anchors_after


def test_missing_input_raises():
    with pytest.raises(SvgCleanError):
        clean_svg("/nonexistent/nope.svg")


def test_corrupt_xml_raises(tmp_path: Path):
    bad = tmp_path / "bad.svg"
    bad.write_text('<svg><path d="M0 0"', encoding="utf-8")
    with pytest.raises(SvgCleanError):
        clean_svg(bad)
