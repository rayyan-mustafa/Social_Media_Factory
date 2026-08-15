"""CLI: EditModule — Ken Burns + concat → final.mp4 (VPS FFmpeg).

Usage:
  .venv/bin/python -m src.cli.generate_edit \\
    --voice-manifest output/audio/.../voice_manifest.json \\
    --visual-manifest output/images/.../visual_manifest.json

  # mock stills smoke:
  .venv/bin/python -m src.cli.generate_edit ... --allow-placeholders
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

from src.services.composer import EditModule, EditModuleError  # noqa: E402

app = typer.Typer(add_completion=False, help="EditModule — VPS FFmpeg compose")
console = Console()


@app.command()
def main(
    voice_manifest: Path = typer.Option(
        ...,
        "--voice-manifest",
        "-a",
        exists=True,
        dir_okay=False,
        help="voice_manifest.json from VoiceModule",
    ),
    visual_manifest: Path = typer.Option(
        ...,
        "--visual-manifest",
        "-i",
        exists=True,
        dir_okay=False,
        help="visual_manifest.json from VisualModule",
    ),
    out_dir: Path | None = typer.Option(None, "--out-dir", "-o"),
    no_resume: bool = typer.Option(False, "--no-resume"),
    allow_placeholders: bool = typer.Option(
        False,
        "--allow-placeholders",
        help="Allow mock/placeholder stills (tests only)",
    ),
) -> None:
    console.print("[bold]EditModule[/bold] composing…")
    try:
        result = EditModule().compose(
            voice_manifest=voice_manifest,
            visual_manifest=visual_manifest,
            out_dir=out_dir,
            resume=not no_resume,
            allow_placeholders=allow_placeholders or None,
        )
    except EditModuleError as exc:
        console.print(f"[red]FAIL[/red] {exc}")
        raise typer.Exit(2) from exc
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    v = result.validation
    table = Table(title="Edit validation")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("backend", result.backend)
    table.add_row("scenes/clips", f"{v.scene_count} / {v.clip_count}")
    table.add_row("dims", f"{v.width}x{v.height}")
    table.add_row("has_audio", "yes" if v.has_audio else "no")
    table.add_row(
        "duration_s", f"{v.duration_s:.2f}" if v.duration_s is not None else "—"
    )
    table.add_row("ok", "yes" if v.ok else "no")
    console.print(table)
    for w in v.warnings:
        console.print(f"[yellow]warn[/yellow] {w}")
    console.print(f"[green]OK[/green] final → {result.final_path}")


if __name__ == "__main__":
    app()
