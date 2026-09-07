"""Gate V — nothing reaches a stock platform without clearing this."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.microstock.qa_gate import check_svg
from src.microstock.svg_cleaner import clean_svg

FIXTURE = Path(__file__).parent / "fixtures" / "microstock_dirty.svg"

CLEAN_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="1024px" height="1024px" '
    'viewBox="0 0 1024 1024"><path d="M100 100L300 100L300 300L100 300Z" fill="#36c"/></svg>'
)


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_clean_asset_passes(tmp_path: Path):
    assert check_svg(_write(tmp_path, "ok.svg", CLEAN_SVG)).passed


def test_raw_dirty_fixture_is_rejected():
    result = check_svg(FIXTURE)
    assert not result.passed
    assert any("forbidden nodes" in f for f in result.failures)


def test_same_fixture_passes_after_cleaning(tmp_path: Path):
    out = tmp_path / "clean.svg"
    clean_svg(FIXTURE, out)
    assert check_svg(out).passed


def test_missing_viewbox_is_rejected(tmp_path: Path):
    body = CLEAN_SVG.replace(' viewBox="0 0 1024 1024"', "")
    result = check_svg(_write(tmp_path, "novb.svg", body))
    assert not result.passed
    assert any("viewBox" in f for f in result.failures)


def test_empty_svg_is_rejected(tmp_path: Path):
    body = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024"></svg>'
    result = check_svg(_write(tmp_path, "empty.svg", body))
    assert not result.passed
    assert any("no vector paths" in f for f in result.failures)


def test_anchor_budget_is_enforced(tmp_path: Path):
    result = check_svg(_write(tmp_path, "ok.svg", CLEAN_SVG), overrides={"max_anchor_points": 1})
    assert not result.passed
    assert any("anchor budget" in f for f in result.failures)


def test_file_size_budget_is_enforced(tmp_path: Path):
    result = check_svg(_write(tmp_path, "ok.svg", CLEAN_SVG), overrides={"max_file_bytes": 10})
    assert not result.passed
    assert any("too large" in f for f in result.failures)


def test_corrupt_svg_is_rejected(tmp_path: Path):
    result = check_svg(_write(tmp_path, "bad.svg", '<svg><path d="M0 0"'))
    assert not result.passed
    assert any("unparseable" in f for f in result.failures)


def test_missing_file_is_rejected(tmp_path: Path):
    assert not check_svg(tmp_path / "nope.svg").passed


def test_embedded_data_uri_is_rejected(tmp_path: Path):
    body = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">'
        '<g><image href="data:image/png;base64,iVBORw0KGgo="/></g>'
        '<path d="M0 0L10 10Z"/></svg>'
    )
    result = check_svg(_write(tmp_path, "raster.svg", body))
    assert not result.passed


def test_over_simplification_is_caught_by_fidelity(tmp_path: Path):
    """Destroying the artwork must fail even though every structural check passes."""
    reference = tmp_path / "ref.svg"
    clean_svg(FIXTURE, reference, overrides={"simplify_epsilon": 0})
    destroyed = tmp_path / "destroyed.svg"
    clean_svg(FIXTURE, destroyed, overrides={"simplify_epsilon": 400.0})

    result = check_svg(destroyed, reference_svg=reference, overrides={"max_mean_pixel_diff": 0.5})
    assert not result.passed
    assert any("changed the artwork" in f for f in result.failures)


def test_faithful_simplification_passes_fidelity(tmp_path: Path):
    reference = tmp_path / "ref.svg"
    clean_svg(FIXTURE, reference, overrides={"simplify_epsilon": 0})
    gentle = tmp_path / "gentle.svg"
    clean_svg(FIXTURE, gentle, overrides={"simplify_epsilon": 1.0})
    result = check_svg(gentle, reference_svg=reference)
    assert result.passed
    assert result.metrics["fidelity_mean_diff"] is not None


def test_gate_reports_metrics_without_mutating_the_file(tmp_path: Path):
    path = _write(tmp_path, "ok.svg", CLEAN_SVG)
    before = path.read_bytes()
    result = check_svg(path)
    assert path.read_bytes() == before
    assert result.metrics["path_count"] == 1
