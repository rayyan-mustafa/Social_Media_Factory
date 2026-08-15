#!/usr/bin/env python3
"""Fetch curated PD heroes for Scottish King (or any job) and swap into stills.

Usage:
  .venv/bin/python scripts/fetch_pd_heroes_scottish_king.py \\
    --job output/jobs/20260808T061032Z_What_If_a_Tudor_Queen_Married_a_Scottish_King
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.pd_clippings import apply_pd_heroes_to_job  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Curated PD hero fetch + job swap")
    ap.add_argument(
        "--job",
        type=Path,
        default=ROOT
        / "output/jobs/20260808T061032Z_What_If_a_Tudor_Queen_Married_a_Scottish_King",
    )
    args = ap.parse_args()
    report = apply_pd_heroes_to_job(args.job)
    print(json.dumps(report, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
