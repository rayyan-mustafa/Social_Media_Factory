"""Gate V — the quality gate every asset must clear before it leaves this machine.

The hybrid-autonomy contract: generation, tracing and cleanup run headless, but
nothing reaches a stock platform without passing here first. A rejected batch
costs nothing; a rejected *upload* damages the contributor account score that
every future acceptance depends on.

Mirrors the farm's Gate B/Gate R pattern: a pure, side-effect-free check that
returns a structured verdict, leaving the caller to decide what to do with it.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree

from src.microstock import config, svgpath

logger = logging.getLogger(__name__)

FORBIDDEN_TAGS = ("image", "foreignObject", "script")


@dataclass
class GateResult:
    """Structured verdict. ``passed`` is the only thing callers should branch on."""

    asset: str = ""
    passed: bool = False
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def fail(self, reason: str) -> None:
        self.failures.append(reason)

    def warn(self, reason: str) -> None:
        self.warnings.append(reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "passed": self.passed,
            "failures": self.failures,
            "warnings": self.warnings,
            "metrics": self.metrics,
        }


def _localname(element: etree._Element) -> str:
    tag = element.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _render_gray(svg_path: Path, size: int):
    """Render an SVG to a square grayscale numpy array, or None if it will not render."""
    import io

    import cairosvg
    import numpy as np
    from PIL import Image

    try:
        png = cairosvg.svg2png(
            url=str(svg_path), output_width=size, output_height=size,
            background_color="white",
        )
    except Exception as exc:  # noqa: BLE001 - cairosvg raises a wide family
        logger.warning("fidelity render failed for %s: %s", svg_path.name, exc)
        return None
    with Image.open(io.BytesIO(png)) as img:
        return np.asarray(img.convert("L"), dtype="float32")


def _fidelity_diff(reference: Path, candidate: Path, size: int) -> float | None:
    """Mean absolute pixel difference (0-255) between two rendered SVGs."""
    import numpy as np

    ref = _render_gray(reference, size)
    cand = _render_gray(candidate, size)
    if ref is None or cand is None or ref.shape != cand.shape:
        return None
    return float(np.mean(np.abs(ref - cand)))


def check_svg(
    svg_path: str | Path,
    *,
    reference_svg: str | Path | None = None,
    clean_report: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> GateResult:
    """Run Gate V against a cleaned SVG.

    ``reference_svg`` is the pre-simplification trace; when supplied the fidelity
    check verifies simplification did not destroy the artwork.
    """
    path = Path(svg_path)
    result = GateResult(asset=path.name)

    cfg = dict(config.section("gate"))
    cfg.update(overrides or {})

    if not path.is_file():
        result.fail(f"missing file: {path}")
        return result

    # --- 1. Parseable -------------------------------------------------
    try:
        root = etree.parse(str(path)).getroot()
    except etree.XMLSyntaxError as exc:
        result.fail(f"unparseable SVG: {exc}")
        return result

    # --- 2. No rasters, scripts or embedded data URIs, at any depth ----
    if cfg.get("forbid_raster", True):
        found: dict[str, int] = {}
        for element in root.iter():
            name = _localname(element)
            href = element.get("href") or element.get(
                "{http://www.w3.org/1999/xlink}href"
            ) or ""
            if name in FORBIDDEN_TAGS:
                found[name] = found.get(name, 0) + 1
            elif href.strip().lower().startswith("data:image"):
                found["data-uri"] = found.get("data-uri", 0) + 1
        if found:
            result.fail(f"forbidden nodes present: {found}")
        result.metrics["forbidden_nodes"] = found

    # --- 3. Explicit viewBox ------------------------------------------
    viewbox = root.get("viewBox")
    result.metrics["viewbox"] = viewbox
    if cfg.get("require_viewbox", True) and not viewbox:
        result.fail("no explicit viewBox — platform previews will clip")

    # --- 4. Anchor budget ---------------------------------------------
    anchors = 0
    paths = 0
    for element in root.iter():
        if _localname(element) == "path" and element.get("d"):
            paths += 1
            anchors += svgpath.count_anchors(element.get("d", ""))
    result.metrics["anchor_points"] = anchors
    result.metrics["path_count"] = paths
    max_anchors = int(cfg.get("max_anchor_points") or 0)
    if max_anchors and anchors > max_anchors:
        result.fail(f"anchor budget exceeded: {anchors} > {max_anchors}")
    if paths == 0:
        result.fail("no vector paths — asset is empty")

    # --- 5. File size --------------------------------------------------
    size_bytes = path.stat().st_size
    result.metrics["bytes"] = size_bytes
    max_bytes = int(cfg.get("max_file_bytes") or 0)
    if max_bytes and size_bytes > max_bytes:
        result.fail(f"file too large: {size_bytes} > {max_bytes} bytes")

    # --- 6. Node reduction (recorded, never a pass condition) ----------
    if clean_report:
        result.metrics["node_reduction_pct"] = clean_report.get("node_reduction_pct")
        result.metrics["backgrounds_removed"] = clean_report.get("backgrounds_removed")
        if clean_report.get("paths_unsupported"):
            result.warn(
                f"{clean_report['paths_unsupported']} path(s) left unsimplified "
                "(unsupported commands)"
            )

    # --- 7. Fidelity: did simplification destroy the artwork? ----------
    if cfg.get("fidelity_check", True) and reference_svg:
        size = int(cfg.get("fidelity_render_px") or 256)
        diff = _fidelity_diff(Path(reference_svg), path, size)
        result.metrics["fidelity_mean_diff"] = None if diff is None else round(diff, 2)
        if diff is None:
            result.warn("fidelity check skipped — one side failed to render")
        else:
            limit = float(cfg.get("max_mean_pixel_diff") or 18.0)
            if diff > limit:
                result.fail(f"simplification changed the artwork: mean diff {diff:.1f} > {limit}")
    elif cfg.get("fidelity_check", True):
        # No reference, but the asset must still render at all.
        if _render_gray(path, int(cfg.get("fidelity_render_px") or 256)) is None:
            result.fail("SVG does not render")

    result.passed = not result.failures
    logger.info(
        "gate %s: %s%s", path.name, "PASS" if result.passed else "FAIL",
        "" if result.passed else f" ({'; '.join(result.failures)})",
    )
    return result
