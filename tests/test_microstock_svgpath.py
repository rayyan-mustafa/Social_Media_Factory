"""Path parsing, curve flattening and Douglas-Peucker simplification."""

from __future__ import annotations

import math

import pytest

from src.microstock import svgpath


def test_douglas_peucker_drops_collinear_noise():
    points = [(0, 0), (1, 0.01), (2, 0), (3, 0.01), (4, 0), (5, 10), (6, 0)]
    simplified = svgpath.douglas_peucker(points, 0.5)
    assert simplified[0] == (0, 0)
    assert simplified[-1] == (6, 0)
    assert (5, 10) in simplified, "the real corner must survive"
    assert len(simplified) < len(points)


def test_douglas_peucker_keeps_endpoints_and_is_stable():
    points = [(0, 0), (10, 10)]
    assert svgpath.douglas_peucker(points, 5.0) == points
    assert svgpath.douglas_peucker([], 1.0) == []


def test_arcs_are_refused_rather_than_corrupted():
    with pytest.raises(svgpath.UnsupportedPath):
        svgpath.to_polylines("M0 0 A 10 10 0 0 1 20 20")


def test_relative_commands_resolve_to_absolute():
    absolute = svgpath.to_polylines("M10 10 L20 10 L20 20 Z")
    relative = svgpath.to_polylines("m10 10 l10 0 l0 10 z")
    assert absolute[0] == relative[0]


def test_horizontal_vertical_shorthand():
    line = svgpath.to_polylines("M0 0 H10 V10 Z")[0]
    assert (10.0, 0.0) in line
    assert (10.0, 10.0) in line


def test_cubic_is_flattened_to_points_on_the_curve():
    points = svgpath.to_polylines("M0 0 C0 10 10 10 10 0", tolerance=0.5)[0]
    assert len(points) > 2
    assert points[0] == (0.0, 0.0)
    assert math.isclose(points[-1][0], 10.0, abs_tol=1e-6)
    # A symmetric cubic must bulge downward between the endpoints.
    assert max(p[1] for p in points) > 1.0


def test_implicit_repeated_lineto_arguments():
    points = svgpath.to_polylines("M0 0 L1 1 2 2 3 3")[0]
    assert points == [(0.0, 0.0), (1.0, 1.0), (2.0, 2.0), (3.0, 3.0)]


def test_multiple_subpaths_are_separated():
    subpaths = svgpath.to_polylines("M0 0 L1 0 Z M5 5 L6 5 Z")
    assert len(subpaths) == 2


def test_round_trip_emits_parseable_path():
    original = "M0 0 L10 0 L10 10 Z"
    emitted = svgpath.polylines_to_path(svgpath.to_polylines(original))
    assert emitted.startswith("M")
    assert svgpath.to_polylines(emitted)


def test_count_anchors_ignores_close_command():
    assert svgpath.count_anchors("M0 0 L1 1 Z") == 2
