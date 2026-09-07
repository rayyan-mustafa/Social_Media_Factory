"""Stage chain: brief -> raster -> trace -> clean -> Gate V.

One asset at a time, ledger-backed and idempotent. Every stage records its result
so a crashed beat resumes where it stopped instead of regenerating paid work.

Distribution deliberately lives outside this chain — an asset leaving this
function is *staged*, never uploaded. Only ``drip``/``ftp_uploader`` move bytes
off the machine, and only after Gate V passed.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.microstock import config, paths
from src.microstock.generator import GenerateError, generate
from src.microstock.ledger import AssetLedger, sha256_file
from src.microstock.qa_gate import check_svg
from src.microstock.svg_cleaner import SvgCleanError, clean_svg
from src.microstock.vectorizer import VectorizeError, vectorize

logger = logging.getLogger(__name__)


@dataclass
class AssetResult:
    """Outcome of running one asset through the chain."""

    asset_id: str = ""
    prompt: str = ""
    niche: str = ""
    stage: str = "brief"
    passed: bool = False
    error: str | None = None
    png: Path | None = None
    traced_svg: Path | None = None
    clean_svg: Path | None = None
    cost_usd: float = 0.0
    clean_report: dict[str, Any] = field(default_factory=dict)
    gate: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "niche": self.niche,
            "stage": self.stage,
            "passed": self.passed,
            "error": self.error,
            "png": str(self.png) if self.png else None,
            "clean_svg": str(self.clean_svg) if self.clean_svg else None,
            "cost_usd": round(self.cost_usd, 4),
            "clean_report": self.clean_report,
            "gate": self.gate,
        }


def _reject(result: AssetResult, ledger: AssetLedger, reason: str) -> AssetResult:
    """Move a failed asset out of the working set and record why."""
    result.error = reason
    result.passed = False
    if result.clean_svg and result.clean_svg.is_file():
        paths.REJECTED_DIR.mkdir(parents=True, exist_ok=True)
        target = paths.REJECTED_DIR / result.clean_svg.name
        try:
            shutil.move(str(result.clean_svg), str(target))
            result.clean_svg = target
        except OSError:
            logger.warning("could not move rejected asset %s", result.clean_svg)
    ledger.set_stage(
        result.asset_id, "gate_failed", error=reason, gate=result.gate, niche=result.niche
    )
    logger.info("asset %s REJECTED: %s", result.asset_id, reason)
    return result


def process_asset(
    prompt: str,
    *,
    asset_id: str | None = None,
    niche: str = "",
    ledger: AssetLedger | None = None,
    backend: str | None = None,
    source_png: str | Path | None = None,
) -> AssetResult:
    """Run one brief through generate -> trace -> clean -> gate.

    ``source_png`` skips generation and vectorises an existing raster, which is
    how the offline test path and manual re-runs work.
    """
    paths.ensure_dirs()
    ledger = ledger or AssetLedger()
    result = AssetResult(prompt=prompt, niche=niche)

    # --- generate (or adopt an existing raster) -----------------------
    try:
        if source_png:
            png = Path(source_png)
            if not png.is_file():
                raise GenerateError(f"source raster not found: {png}")
        else:
            image = generate(prompt, backend=backend, asset_id=asset_id)
            png = image.path
            result.cost_usd = image.cost_usd
    except GenerateError as exc:
        result.error = str(exc)
        result.stage = "brief"
        return result

    # Identity is the content hash — makes every later stage idempotent.
    result.asset_id = asset_id or sha256_file(png)[:24]
    result.png = png
    result.stage = "generated"
    ledger.set_stage(
        result.asset_id, "generated", prompt=prompt, niche=niche,
        png=str(png), cost_usd=result.cost_usd,
    )

    # --- trace ---------------------------------------------------------
    traced = paths.TRACED_SVG_DIR / f"{result.asset_id}.svg"
    try:
        result.traced_svg = vectorize(png, traced)
    except VectorizeError as exc:
        return _reject(result, ledger, f"trace failed: {exc}")
    result.stage = "traced"
    ledger.set_stage(result.asset_id, "traced", traced_svg=str(traced))

    # --- clean ---------------------------------------------------------
    cleaned = paths.CLEAN_SVG_DIR / f"{result.asset_id}.svg"
    try:
        report = clean_svg(traced, cleaned)
    except SvgCleanError as exc:
        return _reject(result, ledger, f"clean failed: {exc}")
    result.clean_svg = cleaned
    result.clean_report = report.as_dict()
    result.stage = "cleaned"
    ledger.set_stage(result.asset_id, "cleaned", clean_svg=str(cleaned), clean_report=result.clean_report)

    # --- Gate V --------------------------------------------------------
    verdict = check_svg(cleaned, reference_svg=traced, clean_report=result.clean_report)
    result.gate = verdict.as_dict()
    if not verdict.passed:
        return _reject(result, ledger, "; ".join(verdict.failures))

    result.passed = True
    result.stage = "gate_passed"
    ledger.set_stage(result.asset_id, "gate_passed", gate=result.gate, niche=niche)
    logger.info("asset %s PASSED Gate V", result.asset_id)
    return result


def process_batch(
    briefs: list[dict[str, Any]],
    *,
    ledger: AssetLedger | None = None,
    backend: str | None = None,
    stop_on_quota: bool = True,
) -> list[AssetResult]:
    """Run several briefs. Quota exhaustion parks the remainder rather than looping."""
    from src.microstock.generator import QuotaExhausted

    ledger = ledger or AssetLedger()
    results: list[AssetResult] = []
    for brief in briefs:
        try:
            results.append(
                process_asset(
                    brief.get("prompt", ""),
                    niche=brief.get("niche", ""),
                    asset_id=brief.get("asset_id"),
                    ledger=ledger,
                    backend=backend,
                    source_png=brief.get("source_png"),
                )
            )
        except QuotaExhausted as exc:
            logger.warning("quota exhausted — parking %d remaining brief(s): %s",
                           len(briefs) - len(results), exc)
            if stop_on_quota:
                break
    return results


def summarise(results: list[AssetResult]) -> dict[str, Any]:
    passed = [r for r in results if r.passed]
    return {
        "total": len(results),
        "passed": len(passed),
        "rejected": len(results) - len(passed),
        "cost_usd": round(sum(r.cost_usd for r in results), 4),
        "reject_reasons": [r.error for r in results if not r.passed and r.error],
    }
