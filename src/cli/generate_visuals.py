"""CLI: VisualModule — stills (+ optional sparse AI video clips).

Usage:
  .venv/bin/python -m src.cli.generate_visuals output/scripts/SCRIPT.json
  .venv/bin/python -m src.cli.generate_visuals SCRIPT.json --limit 1   # cheap smoke test
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.visuals_ai import VisualModule, VisualModuleError  # noqa: E402

app = typer.Typer(add_completion=False, help="VisualModule — stills + sparse video")
console = Console()


@app.command()
def main(
    script: Path = typer.Argument(..., exists=True, dir_okay=False),
    out_dir: Path | None = typer.Option(None, "--out-dir", "-o"),
    no_resume: bool = typer.Option(False, "--no-resume"),
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="Only first N scenes (cheap smoke test)"
    ),
) -> None:
    console.print(f"[bold]VisualModule[/bold] script: {script}")
    try:
        result = VisualModule().synthesize_script(
            script, out_dir=out_dir, resume=not no_resume, limit_scenes=limit
        )
    except VisualModuleError as exc:
        console.print(f"[red]FAIL[/red] {exc}")
        raise typer.Exit(2) from exc
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    _print_summary(result)
    console.print(f"[green]OK[/green] images → {result.out_dir}")
    if result.meta.get("video_scene_indices"):
        console.print(f"       video scenes: {result.meta['video_scene_indices']}")


def _print_summary(result) -> None:
    v = result.validation
    table = Table(title="Visual validation")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("backend", result.backend)
    table.add_row("scenes/images", f"{v.scene_count} / {v.image_count}")
    table.add_row("dims", f"{v.width}x{v.height}")
    table.add_row("video clips", f"{v.video_clip_count} / planned {v.video_scene_count}")
    table.add_row("median bytes", str(v.median_image_bytes) if v.median_image_bytes else "—")
    table.add_row("ok", "yes" if v.ok else "no")
    console.print(table)
    for w in v.warnings:
        console.print(f"[yellow]warn[/yellow] {w}")


if __name__ == "__main__":
    app()
