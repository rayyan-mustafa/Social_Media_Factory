"""CLI: unattended sleep-mode factory beat + crontab helper."""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.sleep_factory import (  # noqa: E402
    install_crontab,
    install_crontab_hint,
    run_beat,
)

app = typer.Typer(add_completion=False, help="Sleep-mode YouTube factory")
console = Console()


@app.command("beat")
def beat(
    harvest: bool | None = typer.Option(
        None,
        "--harvest/--no-harvest",
        help="Override agents_settings sleep_beat.harvest_if_queue_low",
    ),
    run_pipeline: bool | None = typer.Option(
        None,
        "--run-pipeline/--no-run-pipeline",
        help=(
            "Spawn real production farm for picked titles "
            "(default: sleep_beat.run_pipeline / enqueue_pipeline in config)"
        ),
    ),
    min_queued: int | None = typer.Option(
        None, "--min-queued", help="Override sleep_beat.min_queued_titles"
    ),
) -> None:
    """Unattended ops cycle. Defaults ON: spawn detached Script→…→private YouTube."""
    result = run_beat(
        harvest_if_queue_low=harvest,
        run_pipeline=run_pipeline,
        min_queued_titles=min_queued,
    )
    console.print_json(data=result)


@app.command("crontab")
def crontab(
    install: bool = typer.Option(
        False, "--install", help="Append sleep-beat lines to user crontab"
    ),
) -> None:
    """Print (or install) crontab lines for sleep mode."""
    hint = install_crontab_hint()
    console.print(hint)
    if install:
        path = install_crontab()
        console.print(f"[green]Installed[/green] → {path}")
    else:
        console.print(
            "\n[dim]Install with: .venv/bin/python -m src.cli.sleep_factory crontab --install[/dim]\n"
            "[yellow]Human gates:[/yellow] approved=TRUE to farm; "
            "public_approved=TRUE to arm publishAt. Beat never auto-approves."
        )


if __name__ == "__main__":
    app()
