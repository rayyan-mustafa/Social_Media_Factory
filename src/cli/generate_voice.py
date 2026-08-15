"""CLI: VoiceModule — Kokoro TTS, one WAV per script scene.

Requires the Kokoro Python env (3.12):
  .venv-kokoro/bin/python -m src.cli.generate_voice path/to/script.json
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

from src.services.tts_kokoro import (  # noqa: E402
    VoiceModule,
    VoiceModuleError,
    VoiceValidationError,
)

app = typer.Typer(
    add_completion=False,
    help="VoiceModule — Kokoro on VPS, one WAV per scene",
)
console = Console()


@app.command()
def main(
    script: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        help="Script JSON from ScriptModule (output/scripts/*.json)",
    ),
    out_dir: Path | None = typer.Option(
        None,
        "--out-dir",
        "-o",
        help="Output folder for scene_XXX.wav (default: output/audio/<timestamp>_*)",
    ),
    voice: str | None = typer.Option(
        None, "--voice", "-v", help="Kokoro voice id (default from .env KOKORO_VOICE)"
    ),
    speed: float | None = typer.Option(
        None, "--speed", "-s", help="Speech speed (default KOKORO_SPEED, usually 1.0)"
    ),
    no_resume: bool = typer.Option(
        False,
        "--no-resume",
        help="Regenerate all WAVs even if scene_XXX.wav already exists",
    ),
) -> None:
    """Synthesize narration WAVs for every scene in a script JSON."""
    console.print(f"[bold]VoiceModule[/bold] script: {script}")
    try:
        result = VoiceModule(voice=voice, speed=speed).synthesize_script(
            script,
            out_dir=out_dir,
            resume=not no_resume,
        )
    except VoiceValidationError as exc:
        result = exc.result
        _print_summary(result)
        console.print(f"[yellow]manifest[/yellow] {result.meta.get('manifest')}")
        console.print(f"[red]FAIL[/red] {exc}")
        raise typer.Exit(2) from exc
    except (VoiceModuleError, Exception) as exc:  # noqa: BLE001
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    _print_summary(result)
    console.print(f"[green]OK[/green] wavs → {result.out_dir}")
    console.print(f"       manifest → {result.meta.get('manifest')}")


def _print_summary(result) -> None:
    v = result.validation
    table = Table(title="Voice validation")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("title", result.title)
    table.add_row("voice", f"{result.voice} @ {result.speed}x")
    table.add_row("scenes / wavs", f"{v.scene_count} / {v.wav_count}")
    table.add_row("total audio", f"{v.total_duration_s:.1f}s ({v.total_duration_s/60:.2f} min)")
    table.add_row(
        "median / min / max",
        f"{v.median_duration_s:.2f}s / {v.min_duration_s:.2f}s / {v.max_duration_s:.2f}s"
        if v.median_duration_s is not None
        else "—",
    )
    table.add_row("gallery target", f"~{v.target_seconds_per_scene:.0f}s / scene")
    table.add_row("ok", "yes" if v.ok else "no")
    console.print(table)
    for w in v.warnings:
        console.print(f"[yellow]warn[/yellow] {w}")
    for e in v.errors:
        console.print(f"[red]error[/red] {e}")
    skipped = sum(1 for s in result.scenes if s.skipped)
    if skipped:
        console.print(f"[dim]resumed {skipped} existing wav(s)[/dim]")
    console.print("\n[dim]First scenes:[/dim]")
    for s in result.scenes[:3]:
        console.print(f"  [{s.index:03d}] {s.duration_s:.2f}s  {s.text[:70]}")


if __name__ == "__main__":
    app()
