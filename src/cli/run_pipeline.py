"""CLI: full Plan C pipeline (Modules 1–5) → final.mp4 → private YouTube.

Usage:
  # cheap local end-to-end + private upload:
  .venv/bin/python -m src.cli.run_pipeline "Why habits stick" --test \\
    -v am_michael -s 0.8

  # stop at final.mp4 (no YouTube):
  .venv/bin/python -m src.cli.run_pipeline "Topic" --test --no-publish

  # Gate A + publish_manifest only (no API call):
  .venv/bin/python -m src.cli.run_pipeline "Topic" --test --publish-dry-run

  # Resume an existing job folder (skip OpenRouter script regenerate):
  .venv/bin/python -m src.cli.run_pipeline "Topic" --resume --no-publish \\
    -o output/armada_e2e_test_...
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.pipeline import Pipeline, PipelineError  # noqa: E402

app = typer.Typer(
    add_completion=False,
    help="Run Script→Voice→Visual→Edit→Publish for one title/topic",
)
console = Console()


@app.command()
def main(
    title: str = typer.Argument(
        ...,
        help="Video title / topic idea (fed to ScriptModule)",
    ),
    niche_notes: str = typer.Option(
        "",
        "--niche-notes",
        "-n",
        help="Optional angle/tone notes for the outline",
    ),
    test: bool = typer.Option(
        False,
        "--test",
        help="Cheap pacing (~5 scenes) for this run only — does not edit .env",
    ),
    mock_images: bool = typer.Option(
        False,
        "--mock-images",
        help="Use local mock stills (no RunPod). Implies placeholders allowed.",
    ),
    limit_scenes: int | None = typer.Option(
        None,
        "--limit-scenes",
        "-L",
        help="Only generate first N stills/clips (debug)",
    ),
    speed: float | None = typer.Option(
        None,
        "--speed",
        "-s",
        help="Kokoro voice speed (e.g. 0.9 slower, 1.1 faster). Default: KOKORO_SPEED in .env",
    ),
    voice: str | None = typer.Option(
        None,
        "--voice",
        "-v",
        help="Kokoro voice id (default: KOKORO_VOICE in .env)",
    ),
    out_dir: Path | None = typer.Option(
        None,
        "--out-dir",
        "-o",
        help="Job folder (default: output/jobs/<timestamp>_<slug>)",
    ),
    publish: bool = typer.Option(
        True,
        "--publish/--no-publish",
        help="After Edit: Gate A + private YouTube upload (Module 5). Default: on.",
    ),
    publish_dry_run: bool = typer.Option(
        False,
        "--publish-dry-run",
        help="Run Gate A + write publish_manifest.json without YouTube API call",
    ),
    resume: bool = typer.Option(
        False,
        "--resume/--no-resume",
        help=(
            "Reuse script.json (and VoiceModule WAVs) already in --out-dir; "
            "skips OpenRouter script generation"
        ),
    ),
    stop_after_voice: bool = typer.Option(
        False,
        "--stop-after-voice",
        help="Phase A: stop after script+Kokoro(+bed); no RunPod / compose / publish",
    ),
    from_visuals: bool = typer.Option(
        False,
        "--from-visuals",
        help="Phase B: require existing script+voice in --out-dir; stills→edit→publish",
    ),
) -> None:
    if stop_after_voice and from_visuals:
        console.print("[red]--stop-after-voice and --from-visuals are mutually exclusive[/red]")
        raise typer.Exit(2)
    do_publish = publish and not publish_dry_run and not stop_after_voice
    console.print(
        Panel.fit(
            f"[bold]Pipeline[/bold] title/topic: {title}\n"
            f"test={test}  mock_images={mock_images}  "
            f"speed={speed if speed is not None else 'env'}  "
            f"voice={voice or 'env'}  limit_scenes={limit_scenes}\n"
            f"resume={resume}  stop_after_voice={stop_after_voice}  "
            f"from_visuals={from_visuals}\n"
            f"publish={do_publish}  publish_dry_run={publish_dry_run}",
            title="Modules 1–5",
        )
    )
    try:
        result = Pipeline().run(
            title,
            niche_notes=niche_notes,
            job_dir=out_dir,
            mock_images=mock_images,
            test_mode=test,
            limit_visual_scenes=limit_scenes,
            voice=voice,
            speed=speed,
            publish=do_publish,
            publish_dry_run=publish_dry_run and not stop_after_voice,
            resume=resume,
            stop_after_voice=stop_after_voice,
            from_visuals=from_visuals,
        )
    except PipelineError as exc:
        console.print(f"[red]FAIL[/red] {exc}")
        raise typer.Exit(2) from exc
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    console.print(f"[green]OK[/green] title: {result.title}")
    console.print(f"     scenes: {result.scene_count}")
    console.print(f"     job:    {result.job_dir}")
    if result.stopped_after:
        console.print(f"     stopped_after: {result.stopped_after} (no visuals yet)")
        console.print(f"     voice:  {result.voice_manifest}")
    else:
        console.print(f"     final:  {result.final_path}")
    if result.watch_url or result.publish_manifest:
        console.print(f"     privacy: {result.privacy_status or '—'}")
        console.print(f"     video_id: {result.video_id or '—'}")
        console.print(f"     watch:  {result.watch_url or '(dry-run / no upload)'}")
        if result.publish_manifest:
            console.print(f"     publish: {result.publish_manifest}")


if __name__ == "__main__":
    app()
