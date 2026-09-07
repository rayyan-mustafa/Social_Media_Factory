"""Stock SMM — learns what actually sells and feeds it back into harvest.

The microstock counterpart to ``smm_agent.learn_channel_winners`` ->
``smm_harvest_bridge`` -> harvest. Same shape, different signal: instead of
YouTube analytics deciding which video topics to invent next, per-platform
download and earnings reports decide which commercial niches to draw next.

Contributor platforms do not expose earnings APIs to individual contributors, so
this ingests the CSV reports you download from each contributor dashboard into
``output/microstock/ops/reports/<platform>/``. Column names differ per platform,
so matching is done on whatever filename-like and count-like columns are present
rather than assuming one schema.

Like its YouTube sibling, this module only ever *writes signals*. It never
approves, uploads or deletes anything.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.microstock import paths
from src.microstock.ledger import AssetLedger

logger = logging.getLogger(__name__)

REPORTS_DIR = paths.OPS_DIR / "reports"

# Platform CSVs disagree on column names; match case-insensitively on substrings.
_FILENAME_HINTS = ("filename", "file name", "file", "title", "asset", "id", "media")
_DOWNLOAD_HINTS = ("download", "sales", "sold", "units", "count", "quantity")
_REVENUE_HINTS = ("revenue", "earning", "royalty", "amount", "commission", "payout")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _match_column(fieldnames: list[str], hints: tuple[str, ...]) -> str | None:
    """Best-effort column match — exact hint first, then substring."""
    lowered = {name: name.lower().strip() for name in fieldnames if name}
    for hint in hints:
        for original, low in lowered.items():
            if low == hint:
                return original
    for hint in hints:
        for original, low in lowered.items():
            if hint in low:
                return original
    return None


def _to_number(value: Any) -> float:
    """Parse a CSV cell that may carry currency symbols, commas or blanks."""
    if value is None:
        return 0.0
    text = re.sub(r"[^\d.\-]", "", str(value))
    if not text or text in ("-", ".", "-."):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _asset_id_from_cell(cell: str) -> str:
    """Assets are uploaded named by asset_id, so the stem is the id."""
    return Path(str(cell).strip()).stem


def parse_report(csv_path: str | Path) -> list[dict[str, Any]]:
    """Parse one platform report into (asset_id, downloads, revenue) rows."""
    path = Path(csv_path)
    rows: list[dict[str, Any]] = []
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return []
            name_col = _match_column(list(reader.fieldnames), _FILENAME_HINTS)
            dl_col = _match_column(list(reader.fieldnames), _DOWNLOAD_HINTS)
            rev_col = _match_column(list(reader.fieldnames), _REVENUE_HINTS)
            if not name_col:
                logger.warning("report %s has no identifiable filename column", path.name)
                return []
            for raw in reader:
                asset_id = _asset_id_from_cell(raw.get(name_col, ""))
                if not asset_id:
                    continue
                rows.append({
                    "asset_id": asset_id,
                    # A report row with no count column still represents one sale.
                    "downloads": _to_number(raw.get(dl_col)) if dl_col else 1.0,
                    "revenue_usd": _to_number(raw.get(rev_col)) if rev_col else 0.0,
                })
    except (OSError, csv.Error) as exc:
        logger.warning("could not read report %s: %s", path, exc)
        return []
    return rows


def aggregate_by_niche(
    rows: list[dict[str, Any]], ledger: AssetLedger | None = None
) -> dict[str, dict[str, float]]:
    """Roll per-asset sales up to per-niche totals using the asset ledger."""
    ledger = ledger or AssetLedger()
    index = {r.get("asset_id"): r for r in ledger.all()}
    totals: dict[str, dict[str, float]] = {}
    matched = 0
    for row in rows:
        record = index.get(row["asset_id"])
        if not record:
            continue
        niche = record.get("niche") or "unknown"
        matched += 1
        bucket = totals.setdefault(niche, {"downloads": 0.0, "revenue_usd": 0.0, "assets": 0.0})
        bucket["downloads"] += row["downloads"]
        bucket["revenue_usd"] += row["revenue_usd"]
    # Assets published per niche — the denominator that makes niches comparable.
    for record in ledger.all():
        niche = record.get("niche") or "unknown"
        if record.get("stage") in ("queued", "distributed") and niche in totals:
            totals[niche]["assets"] += 1
    logger.info("aggregated %d/%d report rows into %d niche(s)", matched, len(rows), len(totals))
    return totals


def score_niches(totals: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """Score = revenue per published asset, falling back to downloads per asset.

    Per-asset rather than absolute, so a niche with 300 uploads does not
    automatically outrank one with 20 that sells better.
    """
    scored: dict[str, dict[str, float]] = {}
    for niche, row in totals.items():
        assets = max(1.0, row.get("assets", 0.0))
        revenue = row.get("revenue_usd", 0.0)
        downloads = row.get("downloads", 0.0)
        score = revenue / assets if revenue > 0 else (downloads / assets) * 0.01
        scored[niche] = {
            "downloads": round(downloads, 2),
            "revenue_usd": round(revenue, 4),
            "assets": int(assets),
            "score": round(score, 6),
        }
    return scored


def write_benchmarks(scored: dict[str, dict[str, float]], *, source: str = "") -> dict[str, Any]:
    payload = {"updated_at": _now(), "source": source, "niches": scored}
    paths.SALES_BENCHMARKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = paths.SALES_BENCHMARKS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(paths.SALES_BENCHMARKS_PATH)
    return payload


def maybe_refresh_sales(*, ledger: AssetLedger | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Ingest every report under ops/reports/ and refresh the niche benchmarks.

    A no-op with a clear reason when no reports have been downloaded yet, which
    is the normal state until the first payout cycle.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_files = sorted(REPORTS_DIR.rglob("*.csv"))
    if not report_files:
        return {"refreshed": False, "reason": "no_reports", "hint": str(REPORTS_DIR)}

    rows: list[dict[str, Any]] = []
    for path in report_files:
        rows.extend(parse_report(path))
    if not rows:
        return {"refreshed": False, "reason": "reports_unparseable", "files": len(report_files)}

    scored = score_niches(aggregate_by_niche(rows, ledger))
    if not scored:
        return {
            "refreshed": False, "reason": "no_rows_matched_ledger",
            "files": len(report_files), "rows": len(rows),
        }
    if dry_run:
        return {"refreshed": False, "reason": "dry_run", "would_write": scored}

    write_benchmarks(scored, source=f"{len(report_files)} report(s)")
    top = max(scored.items(), key=lambda kv: kv[1]["score"])[0]
    logger.info("sales benchmarks refreshed; best niche = %s", top)
    return {
        "refreshed": True, "files": len(report_files), "rows": len(rows),
        "niches": scored, "best_niche": top,
    }
