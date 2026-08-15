"""CEO-SMM beat — full-auto dual-channel growth manager.

Usage:
  .venv/bin/python -m src.cli.ceo_smm_beat
  .venv/bin/python -m src.cli.ceo_smm_beat --force-scorecard --force-email
  .venv/bin/python -m src.cli.ceo_smm_beat --dry-run

Crontab (hourly)::

  5 * * * * cd ROOT && .venv/bin/python -m src.cli.ceo_smm_beat >> output/ops/ceo_smm.log 2>&1
"""

from __future__ import annotations

import json
import sys

import typer

from src.agents.ceo_smm import run_ceo_beat

app = typer.Typer(add_completion=False, no_args_is_help=False)


@app.callback(invoke_without_command=True)
def main(
    force_scorecard: bool = typer.Option(
        False, "--force-scorecard", help="Force scorecard even if no new reach CSVs"
    ),
    force_email: bool = typer.Option(
        False, "--force-email", help="Send CEO digest email ignoring 6h coalesce"
    ),
    force_competitors: bool = typer.Option(
        False, "--force-competitors", help="Force competitor refresh"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Build playbook/digest only; no spawns/emails/patches"
    ),
) -> None:
    out = run_ceo_beat(
        force_scorecard=force_scorecard,
        force_email=force_email,
        force_competitors=force_competitors,
        dry_run=dry_run,
    )
    print(json.dumps(out, indent=2, default=str))
    if out.get("steps", {}).get("email", {}).get("ok") is False and not dry_run:
        # Non-zero only on hard failure of beat itself
        pass
    sys.exit(0 if not out.get("error") else 1)


if __name__ == "__main__":
    app()
