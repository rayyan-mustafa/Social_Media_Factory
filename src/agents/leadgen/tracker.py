"""Production lead tracker with live CSV schema (AGENCY_MASTER §12-13).

This module handles the LIVE output path only.
Test-mode output stays in run.py / write_run() untouched.

LIVE CSV columns (founder fills outcome fields manually):
  business_name, phone, address, website_url, category, tags, score,
  draft_text, date_scraped, date_sent, sent_by, replied,
  followup_sent, priced, closed, tier, value_pkr_or_usd, notes

NEVER pre-fill date_sent, replied, closed, or any outcome field —
those are entered manually by the founder as real outreach happens.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.services.settings import ROOT

logger = logging.getLogger(__name__)

# ── Path constants ─────────────────────────────────────────────────────────
LIVE_DIR  = ROOT / "output" / "leadgen_live"
LIVE_CSV  = LIVE_DIR / "leads_LIVE.csv"
TEST_DIR  = ROOT / "output" / "leadgen_test"

# ── Live CSV column spec (AGENCY_MASTER §12-13) ────────────────────────────
LIVE_COLUMNS: list[str] = [
    "business_name",
    "phone",
    "address",
    "website_url",
    "category",
    "tags",
    "score",
    "draft_text",
    "date_scraped",
    # ── Outcome fields — filled manually by founder ──
    "date_sent",
    "sent_by",
    "replied",
    "followup_sent",
    "priced",
    "closed",
    "tier",
    "value_pkr_or_usd",
    "notes",
    # ── Internal refs ──
    "TEST_MODE",
    "lead_id",
    "run_id",
    "source",
    "track",
    "grade",
]

EXAMPLE_LIVE_ROW = {
    "business_name": "Austin Best HVAC LLC",
    "phone": "+1 512 555 0199",
    "address": "1402 S Congress Ave, Austin TX 78704",
    "website_url": "",
    "category": "hvac",
    "tags": "no_site; no_booking",
    "score": "38",
    "draft_text": "Hi Austin Best HVAC team — I found you on Google, no website came up, just the listing. ...",
    "date_scraped": "2026-08-15",
    # Founder fills these in manually:
    "date_sent": "",
    "sent_by": "",
    "replied": "",
    "followup_sent": "",
    "priced": "",
    "closed": "",
    "tier": "",
    "value_pkr_or_usd": "",
    "notes": "",
    "TEST_MODE": "FALSE",
    "lead_id": "a1b2c3d4e5f6",
    "run_id": "20260815T123000Z_default_region_hvac_Austin",
    "source": "nominatim",
    "track": "no_site",
    "grade": "Needs work",
}


def _live_row(lead: dict[str, Any], *, run_id: str, test_mode: bool) -> dict[str, Any]:
    """Build a single row dict for the live CSV schema."""
    flags   = lead.get("flags") or []
    reasons = lead.get("reasons") or []
    tags    = "; ".join(str(f) for f in flags) if flags else "; ".join(str(r) for r in reasons)
    draft   = str(lead.get("draft_primary") or lead.get("draft_email") or "").replace("\n", " / ")
    scraped = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "business_name":    str(lead.get("name") or ""),
        "phone":            str(lead.get("phone") or ""),
        "address":          str(lead.get("address") or ""),
        "website_url":      str(lead.get("website") or ""),
        "category":         str(lead.get("vertical") or ""),
        "tags":             tags,
        "score":            str(lead.get("score") or "0"),
        "draft_text":       draft[:500],
        "date_scraped":     scraped,
        # Outcome cols — always empty on first write
        "date_sent":        "",
        "sent_by":          "",
        "replied":          "",
        "followup_sent":    "",
        "priced":           "",
        "closed":           "",
        "tier":             "",
        "value_pkr_or_usd": "",
        "notes":            "",
        # Internal
        "TEST_MODE":        "TRUE" if test_mode else "FALSE",
        "lead_id":          str(lead.get("lead_id") or ""),
        "run_id":           run_id,
        "source":           str(lead.get("source") or ""),
        "track":            str(lead.get("track") or ""),
        "grade":            str(lead.get("grade") or ""),
    }


def write_live_csv(leads: list[dict[str, Any]], *, run_id: str) -> Path:
    """Append new leads to the shared LIVE CSV. TEST_MODE=FALSE on every row.

    Does NOT pre-fill any outcome fields. Returns the path written.
    """
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    exists = LIVE_CSV.is_file()
    with LIVE_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LIVE_COLUMNS, extrasaction="ignore")
        if not exists:
            w.writeheader()
        for lead in leads:
            w.writerow(_live_row(lead, run_id=run_id, test_mode=False))
    logger.info("live tracker: wrote %d rows → %s", len(leads), LIVE_CSV)
    return LIVE_CSV


def write_test_csv(leads: list[dict[str, Any]], *, run_id: str, out_dir: Path) -> Path:
    """Write test-run output with TEST_MODE=TRUE on every row.
    Separate from the live CSV so the two can never mix.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "leads_TEST_ONLY.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LIVE_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for lead in leads:
            w.writerow(_live_row(lead, run_id=run_id, test_mode=True))
    logger.info("test tracker: wrote %d rows → %s", len(leads), path)
    return path
