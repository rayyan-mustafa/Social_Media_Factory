"""CLI: RunPod stills capacity benchmark (no pod create).

Benchmark scheme: ``runpod_stills_benchmark_v1``

Usage::

  .venv/bin/python -m src.cli.runpod_capacity
  .venv/bin/python -m src.cli.runpod_capacity --json
  .venv/bin/python -m src.cli.runpod_capacity --allow-secure   # decision only; does not create

Never creates pods. Logs to output/ops/runpod_capacity_benchmark.jsonl.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.runpod.capacity import (  # noqa: E402
    SCHEME_NAME,
    run_capacity_benchmark,
)
from src.runpod.client import RunPodClient, RunPodClientError  # noqa: E402

app = typer.Typer(
    add_completion=False,
    help="Probe RunPod GPU capacity before stills (no pod create)",
)
console = Console()


@app.command()
def main(
    json_out: bool = typer.Option(
        False, "--json", help="Print full benchmark result as JSON"
    ),
    allow_secure: bool | None = typer.Option(
        None,
        "--allow-secure/--no-allow-secure",
        help="Override RUNPOD_ALLOW_SECURE for decision classification",
    ),
    require_window: bool | None = typer.Option(
        None,
        "--require-window/--no-require-window",
        help="Override RUNPOD_REQUIRE_WINDOW (hard-block outside windows)",
    ),
    no_log: bool = typer.Option(
        False, "--no-log", help="Skip appending to runpod_capacity_benchmark.jsonl"
    ),
) -> None:
    """Run ``runpod_stills_benchmark_v1`` and print GREEN/YELLOW/RED + decision."""
    try:
        client = RunPodClient()
    except RunPodClientError as exc:
        console.print(f"[red]FAIL[/red] {exc}")
        raise typer.Exit(2) from exc

    kwargs: dict = {"client": client, "log_jsonl": not no_log}
    if allow_secure is not None:
        kwargs["allow_secure_flag"] = allow_secure
    if require_window is not None:
        kwargs["require_window_flag"] = require_window

    result = run_capacity_benchmark(**kwargs)

    if json_out:
        console.print_json(data=result.to_dict())
    else:
        color = {
            "GREEN": "green",
            "YELLOW": "yellow",
            "RED": "red",
        }.get(result.classification, "white")
        console.print(
            f"[bold]{SCHEME_NAME}[/bold]  "
            f"[{color}]{result.classification}[/{color}]  "
            f"decision={result.decision}  allow_create={result.allow_create}"
        )
        console.print(result.message)
        if result.schedule_advice:
            console.print(f"[dim]schedule:[/dim] {result.schedule_advice}")

        table = Table(title="GPU levels (GraphQL stock — no pods)")
        table.add_column("GPU")
        table.add_column("Cloud")
        table.add_column("Stock")
        table.add_column("$/hr")
        table.add_column("Avail")
        for lv in result.levels:
            table.add_row(
                lv.gpu_type_id,
                lv.cloud_type,
                str(lv.stock_status or "—"),
                f"{lv.price_usd_hr:.2f}" if lv.price_usd_hr is not None else "—",
                "yes" if lv.available else "no",
            )
        console.print(table)
        console.print(
            f"[dim]logged → output/ops/runpod_capacity_benchmark.jsonl[/dim]"
            if not no_log
            else "[dim]jsonl logging skipped[/dim]"
        )

    # Exit codes: 0 proceed, 3 defer/skip (capacity), 2 API failure already handled
    if not result.allow_create:
        raise typer.Exit(3)


if __name__ == "__main__":
    app()
