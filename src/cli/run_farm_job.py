"""CLI: run one ops job through the real production pipeline (child process).

Spawned by sleep-beat / pick_and_enqueue — do not use for interactive farming;
prefer ``python -m src.cli.run_pipeline`` for manual runs.

Usage:
  .venv/bin/python -m src.cli.run_farm_job --job-id job_abc123
  .venv/bin/python -m src.cli.run_farm_job --job-id job_abc123 --mock-images --publish-dry-run
  .venv/bin/python -m src.cli.run_farm_job --job-id job_abc123 --stop-after-voice
  .venv/bin/python -m src.cli.run_farm_job --job-id job_abc123 --from-visuals --resume
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.farm import execute_farm_job  # noqa: E402

app = typer.Typer(add_completion=False, help="Execute one farm job (ops job_id)")
console = Console()


@app.command()
def main(
    job_id: str = typer.Option(..., "--job-id", help="Ops job id from output/ops/jobs.json"),
    out_dir: Path | None = typer.Option(None, "--out-dir", "-o"),
    mock_images: bool = typer.Option(False, "--mock-images"),
    test: bool = typer.Option(False, "--test", help="Cheap ~5 scene pacing"),
    publish: bool = typer.Option(
        True, "--publish/--no-publish", help="Private YouTube upload after Gate A"
    ),
    publish_dry_run: bool = typer.Option(
        False, "--publish-dry-run", help="Gate A + manifest only (no YouTube API)"
    ),
    resume: bool = typer.Option(
        False,
        "--resume/--no-resume",
        help="Reuse existing script/audio/valid stills in --out-dir (RepairWatchdog)",
    ),
    stop_after_voice: bool = typer.Option(
        False,
        "--stop-after-voice",
        help="Phase A: script+Kokoro(+bed) only; park as ready_for_stills (no RunPod)",
    ),
    from_visuals: bool = typer.Option(
        False,
        "--from-visuals",
        help="Phase B: resume from existing voice; RunPod stills→edit→publish",
    ),
    voice: str | None = typer.Option(None, "--voice", "-v"),
    speed: float | None = typer.Option(None, "--speed", "-s"),
) -> None:
    if stop_after_voice and from_visuals:
        console.print("[red]--stop-after-voice and --from-visuals are mutually exclusive[/red]")
        raise typer.Exit(2)
    result = execute_farm_job(
        job_id,
        out_dir=out_dir,
        mock_images=mock_images,
        publish=publish,
        publish_dry_run=publish_dry_run,
        test_mode=test,
        voice=voice,
        speed=speed,
        resume=resume or None,
        stop_after_voice=stop_after_voice,
        from_visuals=from_visuals,
    )
    console.print_json(data=result)
    if not result.get("ok"):
        raise typer.Exit(2)


if __name__ == "__main__":
    app()
