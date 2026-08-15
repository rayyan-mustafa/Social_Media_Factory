"""Leadgen CLI entry point.

Usage (TEST mode — default, completely safe):
    python -m src.agents.leadgen.cli --city Austin --vertical hvac

Usage (LIVE mode — requires explicit QA confirmation flag):
    python -m src.agents.leadgen.cli --city Austin --vertical hvac \\
        --mode live --i-have-completed-qa

Hard gate: --mode live requires --i-have-completed-qa.
If that flag is missing, the script refuses to run and prints the QA checklist path.
This is a deliberate friction point — do not remove it.

NO auto-sending happens in either mode. This script generates leads and drafts only.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("leadgen.cli")

QA_CHECKLIST = "config/sop/PRE_CUSTOMER_QA.md"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Leadgen pipeline: scrape → score → draft → save CSV"
    )
    p.add_argument("--city",       default="Austin",          help="City to search (default: Austin)")
    p.add_argument("--vertical",   default="hvac",            help="Vertical id from config/leadgen/verticals.json")
    p.add_argument("--region",     default="default_region",  help="Region id from config/leadgen/regions.json")
    p.add_argument("--limit",      type=int, default=25,      help="Max leads to keep (default: 25)")
    p.add_argument("--no-sites",   action="store_true",       help="Skip fetching business homepages (faster)")
    p.add_argument(
        "--mode",
        choices=["test", "live"],
        default="test",
        help=(
            "test (default) = writes to output/leadgen_test/, TEST_MODE=TRUE. "
            "live = writes to output/leadgen_live/leads_LIVE.csv, TEST_MODE=FALSE. "
            "Live mode requires --i-have-completed-qa."
        ),
    )
    p.add_argument(
        "--i-have-completed-qa",
        action="store_true",
        default=False,
        dest="qa_confirmed",
        help="Required when --mode live. Confirms founder has completed PRE_CUSTOMER_QA.md.",
    )
    p.add_argument("--sync-sheets", action="store_true", help="Push results to Google Sheets")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # ── HARD GATE: live mode requires explicit QA confirmation ──────────────
    if args.mode == "live" and not args.qa_confirmed:
        print(
            "\n"
            "❌  LIVE MODE BLOCKED\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "You must complete the pre-customer QA checklist before running\n"
            "the pipeline in live mode.\n\n"
            f"Checklist: {QA_CHECKLIST}\n\n"
            "Once you have completed every item in that checklist, re-run\n"
            "with the explicit confirmation flag:\n\n"
            "    python -m src.agents.leadgen.cli --mode live --i-have-completed-qa\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n",
            file=sys.stderr,
        )
        return 1

    # ── Import pipeline modules ─────────────────────────────────────────────
    from src.agents.leadgen.run import hunt_leads, write_run
    from src.agents.leadgen.tracker import LIVE_DIR, write_live_csv, write_test_csv

    mode_label = "LIVE" if args.mode == "live" else "TEST"
    logger.info(
        "Starting leadgen pipeline — mode=%s  city=%s  vertical=%s  limit=%d",
        mode_label, args.city, args.vertical, args.limit,
    )

    if args.mode == "live":
        logger.warning(
            "LIVE MODE: output will be written to %s — TEST_MODE=FALSE on all rows.",
            LIVE_DIR / "leads_LIVE.csv",
        )

    # ── Scrape + score + draft ──────────────────────────────────────────────
    hunt = hunt_leads(
        region_id=args.region,
        vertical_id=args.vertical,
        city=args.city,
        limit=args.limit,
        fetch_sites=not args.no_sites,
        allow_nominatim_fallback=True,
    )

    leads = hunt.get("leads") or []
    logger.info(
        "Hunt complete: source=%s  n=%d  skipped_chain=%d  skipped_stack=%d  elapsed=%.1fs",
        hunt.get("source"), len(leads),
        hunt.get("skipped_chain", 0), hunt.get("skipped_chat_stack", 0),
        hunt.get("elapsed_s", 0.0),
    )

    # ── Write output ────────────────────────────────────────────────────────
    summary = write_run(hunt, write_sheet=args.sync_sheets)
    run_id  = summary["run_id"]

    if args.mode == "live":
        live_csv = write_live_csv(leads, run_id=run_id)
        print(f"\n✅  LIVE run complete.")
        print(f"   Run ID  : {run_id}")
        print(f"   Leads   : {len(leads)}")
        print(f"   Live CSV: {live_csv}")
        print(f"   Drafts  : {summary['drafts']}")
        print(f"\n⚠️  DO NOT auto-send. Copy drafts manually. Max 20-30/day (AGENCY_MASTER §8/§15).")
    else:
        from datetime import datetime, timezone
        test_ts  = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        test_dir = Path(summary["out_dir"])
        test_csv = write_test_csv(leads, run_id=run_id, out_dir=test_dir)
        print(f"\n✅  TEST run complete (TEST_MODE=TRUE on all rows).")
        print(f"   Run ID      : {run_id}")
        print(f"   Leads found : {len(leads)}")
        print(f"   Test CSV    : {test_csv}")
        print(f"   Drafts      : {summary['drafts']}")
        print(f"   Report dir  : {summary['out_dir']}")
        print(f"\n📋  Review the CSV and drafts. When ready for live outreach,")
        print(f"   complete {QA_CHECKLIST} then re-run with --mode live --i-have-completed-qa")

    return 0


if __name__ == "__main__":
    sys.exit(main())
