"""Minimal SVG path parsing, curve flattening and Douglas-Peucker simplification.

roadmap.txt claims 60-80% node reduction but its sample code contains no
simplification at all. This module is the real implementation.

Scope is deliberately narrow: everything vtracer actually emits (M/L/H/V/C/Q/S/T
and Z, absolute or relative). Anything unrecognised — arcs especially — makes the
caller leave that path untouched rather than risk corrupting artwork.
"""

from __future__ import annotations

import math
import re

Point = tuple[float, float]

_TOKEN_RE = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])|(-?\d*\.?\d+(?:[eE][-+]?\d+)?)")

# Commands this module can faithfully convert to a polyline.
_SUPPORTED = set("MmLlHhVvCcSsQqTtZz")

_ARGC = {
    "M": 2, "L": 2, "H": 1, "V": 1,
    "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0,
}


class UnsupportedPath(ValueError):
    """The path uses a command this module will not risk rewriting (e.g. arcs)."""


def tokenize(d: str) -> list[tuple[str, list[float]]]:
    """Split a path ``d`` attribute into (command, numbers) pairs."""
    tokens: list[tuple[str, list[float]]] = []
    current: str | None = None
    numbers: list[float] = []
    for match in _TOKEN_RE.finditer(d):
        command, number = match.group(1), match.group(2)
        if command is not None:
            if current is not None:
                tokens.append((current, numbers))
            current, numbers = command, []
        elif current is not None:
            numbers.append(float(number))
    if current is not None:
        tokens.append((current, numbers))
    return tokens


def _cubic_at(p0: Point, p1: Point, p2: Point, p3: Point, t: float) -> Point:
    mt = 1.0 - t
    a, b, c, d = mt * mt * mt, 3 * mt * mt * t, 3 * mt * t * t, t * t * t
    return (
        a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
        a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1],
    )


def _flatten_cubic(p0: Point, p1: Point, p2: Point, p3: Point, tolerance: float) -> list[Point]:
    """Sample a cubic Bezier densely enough that tolerance governs the final shape."""
    control_len = (
        math.dist(p0, p1) + math.dist(p1, p2) + math.dist(p2, p3)
    )
    steps = max(2, min(64, int(math.sqrt(control_len / max(tolerance, 0.05)) * 2)))
    return [_cubic_at(p0, p1, p2, p3, i / steps) for i in range(1, steps + 1)]


def to_polylines(d: str, *, tolerance: float = 0.5) -> list[list[Point]]:
    """Convert a path into one polyline per subpath, flattening all curves.

    Raises ``UnsupportedPath`` if the path contains commands we will not rewrite.
    """
    tokens = tokenize(d)
    if any(cmd not in _SUPPORTED for cmd, _ in tokens):
        raise UnsupportedPath(f"unsupported command in path: {d[:40]!r}")

    subpaths: list[list[Point]] = []
    current: list[Point] = []
    cursor: Point = (0.0, 0.0)
    start: Point = (0.0, 0.0)
    prev_cubic_ctrl: Point | None = None
    prev_quad_ctrl: Point | None = None

    for command, numbers in tokens:
        upper = command.upper()
        relative = command.islower()
        argc = _ARGC[upper]

        if upper == "Z":
            if current:
                current.append(start)
                subpaths.append(current)
                current = []
            cursor = start
            prev_cubic_ctrl = prev_quad_ctrl = None
            continue

        # A command's numbers may repeat (implicit repetition, e.g. "L 1 2 3 4").
        chunks = [numbers[i:i + argc] for i in range(0, len(numbers), argc)] or []
        for idx, args in enumerate(chunks):
            if len(args) < argc:
                break
            if upper == "M":
                x, y = args
                if relative:
                    x, y = cursor[0] + x, cursor[1] + y
                # Implicit repeats after an M are treated as L, per SVG spec.
                if idx == 0:
                    if current:
                        subpaths.append(current)
                    start = (x, y)
                    current = [start]
                else:
                    current.append((x, y))
                cursor = (x, y)
                prev_cubic_ctrl = prev_quad_ctrl = None
            elif upper in ("L", "T"):
                x, y = args
                if relative:
                    x, y = cursor[0] + x, cursor[1] + y
                if upper == "T":
                    ctrl = prev_quad_ctrl or cursor
                    reflected = (2 * cursor[0] - ctrl[0], 2 * cursor[1] - ctrl[1])
                    c1 = (cursor[0] + 2 / 3 * (reflected[0] - cursor[0]),
                          cursor[1] + 2 / 3 * (reflected[1] - cursor[1]))
                    c2 = (x + 2 / 3 * (reflected[0] - x), y + 2 / 3 * (reflected[1] - y))
                    current.extend(_flatten_cubic(cursor, c1, c2, (x, y), tolerance))
                    prev_quad_ctrl = reflected
                else:
                    current.append((x, y))
                    prev_quad_ctrl = None
                cursor = (x, y)
                prev_cubic_ctrl = None
            elif upper in ("H", "V"):
                value = args[0]
                if upper == "H":
                    x = cursor[0] + value if relative else value
                    cursor = (x, cursor[1])
                else:
                    y = cursor[1] + value if relative else value
                    cursor = (cursor[0], y)
                current.append(cursor)
                prev_cubic_ctrl = prev_quad_ctrl = None
            elif upper in ("C", "S"):
                if upper == "C":
                    x1, y1, x2, y2, x, y = args
                    if relative:
                        x1, y1 = cursor[0] + x1, cursor[1] + y1
                        x2, y2 = cursor[0] + x2, cursor[1] + y2
                        x, y = cursor[0] + x, cursor[1] + y
                else:
                    x2, y2, x, y = args
                    if relative:
                        x2, y2 = cursor[0] + x2, cursor[1] + y2
                        x, y = cursor[0] + x, cursor[1] + y
                    ctrl = prev_cubic_ctrl or cursor
                    x1, y1 = 2 * cursor[0] - ctrl[0], 2 * cursor[1] - ctrl[1]
                current.extend(_flatten_cubic(cursor, (x1, y1), (x2, y2), (x, y), tolerance))
                cursor = (x, y)
                prev_cubic_ctrl = (x2, y2)
                prev_quad_ctrl = None
            elif upper == "Q":
                x1, y1, x, y = args
                if relative:
                    x1, y1 = cursor[0] + x1, cursor[1] + y1
                    x, y = cursor[0] + x, cursor[1] + y
                c1 = (cursor[0] + 2 / 3 * (x1 - cursor[0]), cursor[1] + 2 / 3 * (y1 - cursor[1]))
                c2 = (x + 2 / 3 * (x1 - x), y + 2 / 3 * (y1 - y))
                current.extend(_flatten_cubic(cursor, c1, c2, (x, y), tolerance))
                cursor = (x, y)
                prev_quad_ctrl = (x1, y1)
                prev_cubic_ctrl = None

    if current:
        subpaths.append(current)
    return [sp for sp in subpaths if len(sp) >= 2]


def douglas_peucker(points: list[Point], epsilon: float) -> list[Point]:
    """Classic Ramer-Douglas-Peucker polyline simplification (iterative)."""
    if len(points) < 3 or epsilon <= 0:
        return list(points)

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]

    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        ax, ay = points[first]
        bx, by = points[last]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)

        best_dist, best_idx = -1.0, -1
        for i in range(first + 1, last):
            px, py = points[i]
            if norm == 0:
                dist = math.hypot(px - ax, py - ay)
            else:
                dist = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if dist > best_dist:
                best_dist, best_idx = dist, i

        if best_dist > epsilon and best_idx > first:
            keep[best_idx] = True
            stack.append((first, best_idx))
            stack.append((best_idx, last))

    return [p for p, k in zip(points, keep, strict=True) if k]


def _fmt(value: float, precision: int) -> str:
    text = f"{value:.{precision}f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def polylines_to_path(subpaths: list[list[Point]], *, precision: int = 2) -> str:
    """Emit simplified polylines as a compact absolute path ``d`` string."""
    parts: list[str] = []
    for points in subpaths:
        if len(points) < 2:
            continue
        closed = math.dist(points[0], points[-1]) < 10 ** -precision
        drawn = points[:-1] if closed and len(points) > 2 else points
        parts.append(f"M{_fmt(drawn[0][0], precision)} {_fmt(drawn[0][1], precision)}")
        parts.extend(
            f"L{_fmt(x, precision)} {_fmt(y, precision)}" for x, y in drawn[1:]
        )
        if closed:
            parts.append("Z")
    return "".join(parts)


def count_anchors(d: str) -> int:
    """Number of anchor points a path defines — the platform 'complexity' metric."""
    total = 0
    for command, numbers in tokenize(d):
        upper = command.upper()
        if upper == "Z":
            continue
        argc = _ARGC.get(upper, 0)
        total += max(1, len(numbers) // argc) if argc else 0
    return total
