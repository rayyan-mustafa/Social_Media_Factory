"""SVG DOM cleanup — what makes a raw trace survive platform review filters.

Implements roadmap.txt section 2A properly. The roadmap's own sample code has
three defects this module fixes:

1. It iterates ``list(root)`` only, so it strips ``<image>`` tags at the top level
   and misses every nested one — which is where tracers actually put them.
2. It promises 60-80% node reduction but performs no simplification whatsoever.
3. Its namespace registration URL is a broken markdown link, so output keeps the
   ``ns0:`` prefix artefacts it claims to remove.

Everything here is driven by ``config/microstock/settings.json -> cleaner``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree

from src.microstock import config, svgpath

logger = logging.getLogger(__name__)

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

# Tags that must never survive into a submitted asset.
DEFAULT_STRIP_TAGS = ("image", "foreignObject", "script", "metadata", "title", "desc")


class SvgCleanError(RuntimeError):
    """Raised when an SVG cannot be parsed or cleaned."""


@dataclass
class CleanReport:
    """What the cleaner did — feeds the QA gate and the ops ledger."""

    source: str = ""
    anchors_before: int = 0
    anchors_after: int = 0
    paths_total: int = 0
    paths_simplified: int = 0
    paths_unsupported: int = 0
    stripped_tags: dict[str, int] = field(default_factory=dict)
    backgrounds_removed: int = 0
    viewbox: str = ""
    bytes_out: int = 0

    @property
    def node_reduction_pct(self) -> float:
        if self.anchors_before <= 0:
            return 0.0
        return (self.anchors_before - self.anchors_after) / self.anchors_before * 100.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "anchors_before": self.anchors_before,
            "anchors_after": self.anchors_after,
            "node_reduction_pct": round(self.node_reduction_pct, 1),
            "paths_total": self.paths_total,
            "paths_simplified": self.paths_simplified,
            "paths_unsupported": self.paths_unsupported,
            "stripped_tags": self.stripped_tags,
            "backgrounds_removed": self.backgrounds_removed,
            "viewbox": self.viewbox,
            "bytes_out": self.bytes_out,
        }


def _localname(element: etree._Element) -> str:
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _strip_forbidden(root: etree._Element, strip_tags: tuple[str, ...]) -> dict[str, int]:
    """Remove forbidden elements anywhere in the tree, not just at the top level.

    This is the fix for the roadmap's top-level-only scrub. Also drops any element
    carrying an ``xlink:href``/``href`` that embeds base64 raster data.
    """
    removed: dict[str, int] = {}
    wanted = set(strip_tags)
    for element in list(root.iter()):
        if element is root:
            continue
        name = _localname(element)
        href = element.get("href") or element.get(f"{{{XLINK_NS}}}href") or ""
        is_embedded_raster = href.strip().lower().startswith("data:image")
        if name in wanted or is_embedded_raster:
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
                key = name if name in wanted else f"{name}[data-uri]"
                removed[key] = removed.get(key, 0) + 1
    return removed


def _path_bbox(d: str, tolerance: float) -> tuple[float, float, float, float] | None:
    try:
        polylines = svgpath.to_polylines(d, tolerance=tolerance)
    except svgpath.UnsupportedPath:
        return None
    points = [p for line in polylines for p in line]
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _strip_backgrounds(
    root: etree._Element, canvas: float, threshold: float, tolerance: float
) -> int:
    """Delete full-frame background shapes so the asset exports transparent.

    A path counts as background when its bounding box covers >= ``threshold`` of
    the canvas in both axes. Only the first such shape in document order is
    removed — a later full-bleed shape is usually intentional artwork, not a
    backdrop.
    """
    removed = 0
    area = canvas * canvas
    if area <= 0:
        return 0
    for element in list(root.iter()):
        name = _localname(element)
        if name == "rect":
            try:
                width = float(element.get("width", "0") or 0)
                height = float(element.get("height", "0") or 0)
            except ValueError:
                continue
            covers = (width / canvas) >= threshold and (height / canvas) >= threshold
        elif name == "path":
            box = _path_bbox(element.get("d", ""), tolerance)
            if box is None:
                continue
            covers = (
                (box[2] - box[0]) / canvas >= threshold
                and (box[3] - box[1]) / canvas >= threshold
            )
        else:
            continue
        if covers:
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
                removed += 1
                break
    return removed


def _simplify_paths(
    root: etree._Element, report: CleanReport, *, epsilon: float,
    tolerance: float, preserve_curves: bool, precision: int,
) -> None:
    """Douglas-Peucker every path, leaving anything unparseable untouched."""
    for element in root.iter():
        if _localname(element) != "path":
            continue
        d = element.get("d")
        if not d:
            continue
        report.paths_total += 1
        before = svgpath.count_anchors(d)
        report.anchors_before += before

        has_curve = any(c in d for c in "CcSsQqTt")
        if preserve_curves and has_curve:
            report.anchors_after += before
            continue
        try:
            polylines = svgpath.to_polylines(d, tolerance=tolerance)
            simplified = [svgpath.douglas_peucker(line, epsilon) for line in polylines]
            new_d = svgpath.polylines_to_path(simplified, precision=precision)
        except svgpath.UnsupportedPath:
            report.paths_unsupported += 1
            report.anchors_after += before
            continue
        if not new_d:
            report.anchors_after += before
            continue
        element.set("d", new_d)
        report.paths_simplified += 1
        report.anchors_after += svgpath.count_anchors(new_d)


def clean_svg(
    input_svg: str | Path,
    output_svg: str | Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
) -> CleanReport:
    """Sanitise, normalise and simplify an SVG. Returns a CleanReport."""
    src = Path(input_svg)
    if not src.is_file():
        raise SvgCleanError(f"input SVG not found: {src}")
    dest = Path(output_svg) if output_svg else src

    cfg = dict(config.section("cleaner"))
    cfg.update(overrides or {})
    canvas = float(cfg.get("canvas") or 1024)
    epsilon = float(cfg.get("simplify_epsilon") or 0)
    tolerance = float(cfg.get("curve_flatten_tolerance") or 0.5)
    preserve_curves = str(cfg.get("simplify_mode") or "flatten") == "preserve_curves"
    strip_tags = tuple(cfg.get("strip_tags") or DEFAULT_STRIP_TAGS)
    threshold = float(cfg.get("background_coverage_threshold") or 0.98)
    precision = int(config.section("vectorizer").get("path_precision") or 2)

    parser = etree.XMLParser(remove_blank_text=True, resolve_entities=False, huge_tree=True)
    try:
        tree = etree.parse(str(src), parser)
    except etree.XMLSyntaxError as exc:
        raise SvgCleanError(f"{src.name} is not parseable XML: {exc}") from exc
    root = tree.getroot()

    report = CleanReport(source=str(src))

    # 1. Scrub forbidden nodes at every depth.
    report.stripped_tags = _strip_forbidden(root, strip_tags)

    # 2. Drop the full-frame backdrop so the asset exports transparent.
    if cfg.get("strip_background", True):
        report.backgrounds_removed = _strip_backgrounds(root, canvas, threshold, tolerance)

    # 3. Simplify geometry.
    if epsilon > 0:
        _simplify_paths(
            root, report, epsilon=epsilon, tolerance=tolerance,
            preserve_curves=preserve_curves, precision=precision,
        )
    else:
        for element in root.iter():
            if _localname(element) == "path" and element.get("d"):
                report.paths_total += 1
                anchors = svgpath.count_anchors(element.get("d", ""))
                report.anchors_before += anchors
                report.anchors_after += anchors

    # 4. Normalise the canvas so previews never clip.
    viewbox = f"0 0 {int(canvas)} {int(canvas)}"
    root.set("width", f"{int(canvas)}px")
    root.set("height", f"{int(canvas)}px")
    root.set("viewBox", viewbox)
    root.set("xmlns", SVG_NS) if root.nsmap.get(None) is None else None
    report.viewbox = viewbox

    dest.parent.mkdir(parents=True, exist_ok=True)
    tree.write(str(dest), encoding="utf-8", xml_declaration=True, pretty_print=False)
    report.bytes_out = dest.stat().st_size

    logger.info(
        "cleaned %s -> %s | anchors %d->%d (%.1f%%) | stripped=%s | bg=%d",
        src.name, dest.name, report.anchors_before, report.anchors_after,
        report.node_reduction_pct, report.stripped_tags, report.backgrounds_removed,
    )
    return report
