"""Raster -> multi-colour SVG tracing via vtracer.

roadmap.txt specifies Potrace, but Potrace is monochrome: it traces a single
silhouette and cannot produce the multi-colour flat art the target niches (SaaS
UI, infographics, packaging) actually sell. vtracer produces stacked colour
layers, needs no system packages, and runs pure-CPU.

Tracing runs in a subprocess (see ``_vtrace_worker``): the current wheel can
segfault, and an unattended beat must survive one bad image.

Every tuning knob lives in ``config/microstock/settings.json -> vectorizer``.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from PIL import Image

from src.microstock import config, paths
from src.microstock._vtrace_worker import PARAM_ORDER

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 180


class VectorizeError(RuntimeError):
    """Raised when tracing fails, times out, or produces unusable output."""


def _trace_params(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Config values filtered to real vtracer params, with `_note` keys dropped."""
    cfg = dict(config.section("vectorizer"))
    cfg.update(overrides or {})
    return {k: v for k, v in cfg.items() if k in PARAM_ORDER and v is not None}


def _prepare_raster(src: Path, work_dir: Path, canvas: int) -> Path:
    """Normalise the source to a square RGBA PNG at the target canvas size.

    A consistently sized raster is what makes the cleaner's viewBox normalisation
    and the gate's node budget comparable across assets.
    """
    with Image.open(src) as img:
        img = img.convert("RGBA")
        if img.size != (canvas, canvas):
            img = img.resize((canvas, canvas), Image.LANCZOS)
        out = work_dir / f"{src.stem}__prepped.png"
        img.save(out, "PNG")
    return out


def vectorize(
    input_png: str | Path,
    output_svg: str | Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    keep_prepped: bool = False,
) -> Path:
    """Trace a raster into a multi-colour SVG. Returns the written SVG path."""
    src = Path(input_png)
    if not src.is_file():
        raise VectorizeError(f"input raster not found: {src}")

    canvas = int(config.section("cleaner").get("canvas") or 1024)
    dest = Path(output_svg) if output_svg else paths.TRACED_SVG_DIR / f"{src.stem}.svg"
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        prepped = _prepare_raster(src, dest.parent, canvas)
    except Exception as exc:  # noqa: BLE001 - Pillow raises a wide family here
        raise VectorizeError(f"unreadable raster {src.name}: {exc}") from exc
    params = _trace_params(overrides)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "src.microstock._vtrace_worker",
             str(prepped), str(dest), json.dumps(params)],
            cwd=str(paths.ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VectorizeError(f"vtracer timed out after {timeout_s}s on {src.name}") from exc
    finally:
        if not keep_prepped:
            prepped.unlink(missing_ok=True)

    if proc.returncode != 0:
        # Negative return code == killed by signal (segfault is -11).
        detail = f"signal {-proc.returncode}" if proc.returncode < 0 else f"exit {proc.returncode}"
        raise VectorizeError(
            f"vtracer failed on {src.name} ({detail}): {(proc.stderr or '').strip()[:300]}"
        )
    if not dest.is_file() or dest.stat().st_size == 0:
        raise VectorizeError(f"vtracer produced no output for {src.name}")

    logger.info("traced %s -> %s (%d bytes)", src.name, dest.name, dest.stat().st_size)
    return dest
