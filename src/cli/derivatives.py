"""CLI for derivative packs (QUALITY FREEZE — image reuse only).

Examples:
  .venv/bin/python -m src.cli.derivatives plan
  .venv/bin/python -m src.cli.derivatives image-pack --job output/jobs/JOBDIR
  .venv/bin/python -m src.cli.derivatives image-pack --job JOBDIR --video-id abc --channel napstorian
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

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


@app.command("plan")
def plan_cmd() -> None:
    from src.content.derivatives import describe_derivative_plan

    console.print_json(data=describe_derivative_plan())


@app.command("image-pack")
def image_pack(
    job: Path = typer.Option(..., "--job", "-j", help="Job directory"),
    video_id: str | None = typer.Option(None, "--video-id"),
    channel: str = typer.Option("napstorian", "--channel", "-c"),
    max_images: int = typer.Option(10, "--max-images"),
) -> None:
    """Build Pinterest + IG/FB/quote/Threads packs from scene stills."""
    from src.content.derivatives import is_module_allowed
    from src.content.image_reuse import build_image_reuse_pack

    if not is_module_allowed("pinterest_pins"):
        console.print("[red]image reuse blocked by freeze config[/red]")
        raise typer.Exit(1)
    if not job.is_dir():
        console.print(f"[red]job dir not found: {job}[/red]")
        raise typer.Exit(1)
    manifest = build_image_reuse_pack(
        job,
        video_id=video_id,
        channel=channel,
        max_images=max_images,
    )
    console.print_json(data=manifest)


@app.command("podcast")
def podcast_frozen() -> None:
    """Frozen under QUALITY FREEZE."""
    console.print(
        "[yellow]podcast_audio is frozen until YT daily scorecard unfreeze gate "
        "(see output/ops/CONTENT_REUSE.md)[/yellow]"
    )
    raise typer.Exit(2)


if __name__ == "__main__":
    app()
