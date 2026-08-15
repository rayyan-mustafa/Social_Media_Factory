"""Scoreboard CLI — AGENCY_MASTER §13 weekly pipeline summary.

Usage:
    python -m src.agents.leadgen.scoreboard
    python -m src.agents.leadgen.scoreboard --csv path/to/leads_LIVE.csv

Reads output/leadgen_live/leads_LIVE.csv and prints a plain-text scoreboard:
  - Sends this week
  - Replies this week
  - Leads by pipeline stage
  - Top untouched leads (highest score, no date_sent yet)
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.services.settings import ROOT

LIVE_CSV = ROOT / "output" / "leadgen_live" / "leads_LIVE.csv"


def _this_week_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now - timedelta(days=now.weekday())


def _parse_date(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def run(csv_path: Path | None = None) -> str:
    path = csv_path or LIVE_CSV
    if not path.is_file():
        return (
            f"[scoreboard] No live CSV found at {path}\n"
            "Run the pipeline in --mode live first, or confirm the path with --csv."
        )

    rows: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        return "[scoreboard] Live CSV is empty."

    week_start = _this_week_start()
    sends_week = 0
    replies_week = 0

    # Stage counters
    stage_sent          = 0
    stage_replied       = 0
    stage_priced        = 0
    stage_closed_won    = 0
    stage_closed_lost   = 0
    stage_untouched     = 0

    top_untouched: list[dict[str, str]] = []

    for row in rows:
        # Skip TEST rows from accidentally mixing in
        if (row.get("TEST_MODE") or "").upper() == "TRUE":
            continue

        date_sent_str = (row.get("date_sent") or "").strip()
        replied_str   = (row.get("replied") or "").strip().upper()
        priced_str    = (row.get("priced") or "").strip().upper()
        closed_str    = (row.get("closed") or "").strip().upper()
        score_str     = (row.get("score") or "0").strip()

        sent_on = _parse_date(date_sent_str)
        if sent_on and sent_on >= week_start:
            sends_week += 1

        # Determine pipeline stage
        if closed_str in ("YES", "WON", "TRUE", "1"):
            stage_closed_won += 1
        elif closed_str in ("LOST", "NO", "0"):
            stage_closed_lost += 1
        elif priced_str in ("YES", "TRUE", "1"):
            stage_priced += 1
        elif replied_str in ("YES", "TRUE", "1"):
            stage_replied += 1
            sent_date = _parse_date(date_sent_str)
            if sent_date and sent_date >= week_start:
                replies_week += 1
        elif date_sent_str:
            stage_sent += 1
        else:
            stage_untouched += 1
            try:
                score = int(float(score_str))
            except (ValueError, TypeError):
                score = 0
            top_untouched.append({**row, "_score_int": str(score)})

    total_live = len([r for r in rows if (r.get("TEST_MODE") or "").upper() != "TRUE"])
    top_untouched.sort(key=lambda r: int(r.get("_score_int") or "0"), reverse=True)

    lines: list[str] = [
        "=" * 58,
        "  AGENCY LEAD PIPELINE SCOREBOARD  (AGENCY_MASTER §13)",
        f"  Week from: {week_start.strftime('%Y-%m-%d')}",
        "=" * 58,
        "",
        f"  📤  Sends this week ............. {sends_week}",
        f"  📬  Replies this week ........... {replies_week}",
        "",
        "  PIPELINE STAGES",
        f"  ○  Untouched (not sent yet) .... {stage_untouched}",
        f"  ➤  Sent (awaiting reply) ........ {stage_sent}",
        f"  💬  Replied ..................... {stage_replied}",
        f"  💰  Priced ...................... {stage_priced}",
        f"  ✅  Closed Won .................. {stage_closed_won}",
        f"  ❌  Closed Lost ................. {stage_closed_lost}",
        "",
        f"  TOTAL LIVE LEADS IN TRACKER ..... {total_live}",
        "",
    ]

    if top_untouched:
        lines.append("  TOP UNTOUCHED LEADS (copy → send manually, max 20-30/day)")
        lines.append("  " + "-" * 54)
        for i, r in enumerate(top_untouched[:10], start=1):
            name    = (r.get("business_name") or "?")[:40]
            score   = r.get("score") or "?"
            track   = r.get("track") or "?"
            phone   = r.get("phone") or "(no phone)"
            lines.append(f"  {i:2}. [{score:>3}] {name}")
            lines.append(f"       track={track}  phone={phone}")
        lines.append("")

    lines.append("=" * 58)
    lines.append("  No sends happen automatically. All outreach is manual.")
    lines.append("=" * 58)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Print AGENCY_MASTER §13 weekly scoreboard from leads_LIVE.csv"
    )
    parser.add_argument("--csv", type=Path, default=None, help="Path to leads_LIVE.csv")
    args = parser.parse_args()
    print(run(csv_path=args.csv))


if __name__ == "__main__":
    main()
