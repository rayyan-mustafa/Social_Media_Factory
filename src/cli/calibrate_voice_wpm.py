"""CLI: calibrate Kokoro voice WPM and write config/voice_wpm.json.

After changing KOKORO_VOICE or KOKORO_SPEED, re-run before the next epic/longform pick.

Examples:
  .venv-kokoro/bin/python -m src.cli.calibrate_voice_wpm
  .venv-kokoro/bin/python -m src.cli.calibrate_voice_wpm --from-job output/jobs/<id>
  .venv/bin/python -m src.cli.calibrate_voice_wpm --words 177 --duration-s 60
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.voice_wpm import (  # noqa: E402
    VOICE_WPM_PATH,
    calibrate_synthesize,
    load_voice_wpm_record,
    measure_from_audio,
    measure_from_job_dir,
    phase_b_words_from_wpm,
    save_voice_wpm,
)

app = typer.Typer(
    add_completion=False,
    help="Calibrate Kokoro WPM → config/voice_wpm.json (drives dual pacing)",
)
console = Console()


@app.command()
def main(
    synthesize: bool = typer.Option(
        True,
        "--synthesize/--no-synthesize",
        help="Synthesize a ~60s calibration clip with current Kokoro voice/speed",
    ),
    from_job: Path | None = typer.Option(
        None,
        "--from-job",
        help="Measure WPM from an existing job dir (script words ÷ audio duration)",
    ),
    words: int | None = typer.Option(
        None, "--words", help="Manual word count (use with --duration-s)"
    ),
    duration_s: float | None = typer.Option(
        None, "--duration-s", help="Manual audio duration in seconds"
    ),
    voice: str | None = typer.Option(None, "--voice", "-v", help="Override Kokoro voice"),
    speed: float | None = typer.Option(None, "--speed", "-s", help="Override Kokoro speed"),
    out: Path | None = typer.Option(
        None, "--out", help=f"Output JSON path (default: {VOICE_WPM_PATH})"
    ),
    text: str | None = typer.Option(
        None, "--text", help="Override calibration narration text for --synthesize"
    ),
) -> None:
    """Measure WPM and write config/voice_wpm.json."""
    out_path = Path(out) if out else VOICE_WPM_PATH
    try:
        if from_job is not None:
            record = measure_from_job_dir(from_job, voice=voice, speed=speed)
        elif words is not None and duration_s is not None:
            # Dummy text with exact word count for measure_from_audio
            filler = " ".join(["word"] * int(words))
            record = measure_from_audio(
                text=filler,
                duration_s=float(duration_s),
                voice=voice,
                speed=speed,
                source="manual",
            )
        elif synthesize:
            console.print("[bold]Synthesizing[/bold] calibration clip (Kokoro)…")
            record, wav = calibrate_synthesize(text=text, voice=voice, speed=speed)
            console.print(f"  wav → {wav}")
        else:
            console.print(
                "[red]Provide --synthesize (default), --from-job, or --words + --duration-s[/red]"
            )
            raise typer.Exit(2)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    path = save_voice_wpm(record, path=out_path)
    b60 = phase_b_words_from_wpm(record.wpm, 60.0)
    b120 = phase_b_words_from_wpm(record.wpm, 120.0)
    console.print(f"[green]OK[/green] wrote {path}")
    console.print_json(data=json.loads(path.read_text(encoding="utf-8")))
    console.print(
        f"Phase B words @ 60s ≈ {b60} · @ 120s ≈ {b120} "
        f"(voice={record.voice} speed={record.speed})"
    )
    existing = load_voice_wpm_record(path=path)
    if existing:
        console.print(
            "[dim]Re-run after changing KOKORO_VOICE or KOKORO_SPEED before next farm pick.[/dim]"
        )


if __name__ == "__main__":
    app()
