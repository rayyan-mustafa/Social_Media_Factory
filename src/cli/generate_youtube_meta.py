"""CLI: Generate YouTube packaging for a completed job.

Steps: OpenRouter metadata → thumbnail prompt (rules/OpenRouter) → Seedream thumbnail image.

Usage:
  .venv/bin/python -m src.cli.generate_youtube_meta \\
    --job-dir output/jobs/20260805T225525Z_What_If_Anne_Boleyn_Outlived_Henry_VIII

  # Thumbnail-only test into a custom folder:
  .venv/bin/python -m src.cli.generate_youtube_meta \\
    --script output/jobs/.../script/script.json \\
    --thumbnail-only --output-dir output/thumbnail_test

  # Rules-only thumbnail prompt (no LLM):
  .venv/bin/python -m src.cli.generate_youtube_meta --job-dir output/jobs/... \\
    --thumbnail-prompt-mode rules

  # Force OpenRouter for thumbnail prompt:
  .venv/bin/python -m src.cli.generate_youtube_meta --job-dir output/jobs/... \\
    --thumbnail-prompt-mode openrouter

  # Skip image generation (prompt + meta only):
  .venv/bin/python -m src.cli.generate_youtube_meta --job-dir output/jobs/... \\
    --skip-thumbnail-image
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

import typer
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.youtube_meta_generate import (  # noqa: E402
    YoutubeMetaGenerateError,
    generate_thumbnail_only,
    generate_youtube_pack,
)

app = typer.Typer(add_completion=False, help="Generate youtube_meta/ for a job")
console = Console()

ThumbnailPromptModeOpt = Literal["rules", "openrouter", "auto"]


@app.command()
def main(
    job_dir: Path | None = typer.Option(
        None,
        "--job-dir",
        "-j",
        exists=True,
        file_okay=False,
        help="Job folder (contains script/script.json)",
    ),
    script: Path | None = typer.Option(
        None,
        "--script",
        "-s",
        exists=True,
        dir_okay=False,
        help="Script JSON path (required for --thumbnail-only without --job-dir)",
    ),
    output_dir: Path | None = typer.Option(
        None,
        "--output-dir",
        "-o",
        help="Custom output directory (required with --thumbnail-only)",
    ),
    thumbnail_only: bool = typer.Option(
        False,
        "--thumbnail-only",
        help="Generate only thumbnail_prompt.txt + thumbnail.jpg (+ test_manifest.json)",
    ),
    seed_title: str | None = typer.Option(
        None,
        "--seed-title",
        help='High-CTR title seed, e.g. "What If Henry VIII Had Never Executed Anne Boleyn?"',
    ),
    thumbnail_prompt_mode: ThumbnailPromptModeOpt = typer.Option(
        "auto",
        "--thumbnail-prompt-mode",
        help=(
            "Thumbnail prompt strategy: "
            "auto = rules first, OpenRouter if low confidence; "
            "rules = heuristics only; "
            "openrouter = always use OpenRouter free model"
        ),
    ),
    skip_meta: bool = typer.Option(
        False, "--skip-meta", help="Skip OpenRouter title/description/tags"
    ),
    skip_thumbnail_prompt: bool = typer.Option(
        False, "--skip-thumbnail-prompt", help="Skip thumbnail_prompt.txt generation"
    ),
    skip_thumbnail_image: bool = typer.Option(
        False,
        "--skip-thumbnail-image",
        help="Skip WaveSpeed Seedream thumbnail.jpg generation",
    ),
    channel: str | None = typer.Option(
        None,
        "--channel",
        "-c",
        help="napstorian | napping_historian — selects thumbnail template pack",
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite existing output files"
    ),
) -> None:
    if thumbnail_only:
        _run_thumbnail_only(
            job_dir=job_dir,
            script=script,
            output_dir=output_dir,
            seed_title=seed_title,
            thumbnail_prompt_mode=thumbnail_prompt_mode,
            force=force,
            channel=channel,
        )
        return

    if job_dir is None:
        console.print("[red]FAIL[/red] --job-dir is required unless using --thumbnail-only")
        raise typer.Exit(2)

    console.print(f"[bold]YouTube packaging[/bold] → {job_dir}")
    console.print(f"[dim]thumbnail-prompt-mode={thumbnail_prompt_mode}[/dim]")
    try:
        result = generate_youtube_pack(
            job_dir,
            script_path=script,
            seed_title=seed_title,
            skip_meta=skip_meta,
            skip_thumbnail_prompt=skip_thumbnail_prompt,
            skip_thumbnail_image=skip_thumbnail_image,
            thumbnail_prompt_mode=thumbnail_prompt_mode,
            force=force,
            channel=channel,
        )
    except YoutubeMetaGenerateError as exc:
        console.print(f"[red]FAIL[/red] {exc}")
        raise typer.Exit(2) from exc
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    table = Table(title="youtube_meta/")
    table.add_column("File")
    table.add_column("Status")
    for label, path in (
        ("title.txt", result.title_path),
        ("description.txt", result.description_path),
        ("tags.json", result.tags_path),
        ("thumbnail_prompt.txt", result.thumbnail_prompt_path),
        ("thumbnail.jpg", result.thumbnail_path),
    ):
        if path is None:
            table.add_row(label, "—")
        elif path.exists():
            table.add_row(label, f"ok ({path.stat().st_size:,} B)")
        else:
            table.add_row(label, "missing")
    table.add_row("meta source", result.meta_source)
    table.add_row("thumbnail prompt mode", result.thumbnail_prompt_mode or "—")
    console.print(table)
    for w in result.warnings:
        console.print(f"[yellow]warn[/yellow] {w}")
    console.print(f"[green]OK[/green] manifest → {result.manifest_path}")


def _run_thumbnail_only(
    *,
    job_dir: Path | None,
    script: Path | None,
    output_dir: Path | None,
    seed_title: str | None,
    thumbnail_prompt_mode: ThumbnailPromptModeOpt,
    force: bool,
    channel: str | None = None,
) -> None:
    if output_dir is None:
        console.print("[red]FAIL[/red] --output-dir is required with --thumbnail-only")
        raise typer.Exit(2)

    sp = script
    if sp is None and job_dir is not None:
        cand = job_dir / "script" / "script.json"
        if cand.exists():
            sp = cand
    if sp is None:
        console.print(
            "[red]FAIL[/red] provide --script or --job-dir with script/script.json"
        )
        raise typer.Exit(2)

    console.print(f"[bold]Thumbnail test[/bold] script={sp}")
    console.print(f"[dim]output → {output_dir} | mode={thumbnail_prompt_mode}[/dim]")

    try:
        result = generate_thumbnail_only(
            sp,
            output_dir,
            seed_title=seed_title,
            thumbnail_prompt_mode=thumbnail_prompt_mode,
            force=force,
            channel=channel,
        )
    except YoutubeMetaGenerateError as exc:
        console.print(f"[red]FAIL[/red] {exc}")
        raise typer.Exit(2) from exc
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    table = Table(title=str(output_dir))
    table.add_column("File")
    table.add_column("Status")
    for label, path in (
        ("thumbnail_prompt.txt", result.thumbnail_prompt_path),
        ("thumbnail.jpg", result.thumbnail_path),
        ("test_manifest.json", result.manifest_path),
    ):
        if path is None:
            table.add_row(label, "—")
        elif path.exists():
            table.add_row(label, f"ok ({path.stat().st_size:,} B)")
        else:
            table.add_row(label, "missing")
    table.add_row("prompt mode", result.thumbnail_prompt_mode)
    for k, v in result.api_status.items():
        table.add_row(f"api/{k}", v)
    console.print(table)
    for w in result.warnings:
        console.print(f"[yellow]warn[/yellow] {w}")
    console.print(f"[green]OK[/green] manifest → {result.manifest_path}")


if __name__ == "__main__":
    app()
