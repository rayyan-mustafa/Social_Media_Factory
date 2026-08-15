"""CLI: PublishModule — Gate A + private YouTube upload (+ AI disclosure).

Usage:
  # dry-run (no API call) — validates Gate A + writes publish_manifest.json
  .venv/bin/python -m src.cli.publish_video \\
    output/jobs/.../video/final.mp4 --dry-run --allow-short

  # real private upload (needs OAuth token first)
  .venv/bin/python -m src.cli.youtube_auth
  .venv/bin/python -m src.cli.publish_video output/jobs/.../video/final.mp4 \\
    --script output/jobs/.../script/script.json \\
    --visual-manifest output/jobs/.../images/visual_manifest.json \\
    --allow-short
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

from src.services.publish_youtube import PublishModule, PublishModuleError  # noqa: E402

app = typer.Typer(add_completion=False, help="PublishModule — private YouTube upload")
console = Console()


@app.command()
def main(
    final: Path = typer.Argument(..., exists=True, dir_okay=False, help="final.mp4"),
    script: Path | None = typer.Option(
        None, "--script", "-s", exists=True, dir_okay=False
    ),
    visual_manifest: Path | None = typer.Option(
        None, "--visual-manifest", "-i", exists=True, dir_okay=False
    ),
    job_dir: Path | None = typer.Option(
        None, "--job-dir", "-j", help="Where to write publish_manifest.json"
    ),
    title: str | None = typer.Option(None, "--title", "-t"),
    youtube_meta: Path | None = typer.Option(
        None,
        "--youtube-meta",
        "-m",
        help="Folder with title.txt, description.txt, tags.json, thumbnail.jpg",
    ),
    thumbnail: Path | None = typer.Option(
        None, "--thumbnail", exists=True, dir_okay=False, help="Override thumbnail image"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="No YouTube API call"),
    allow_short: bool = typer.Option(
        False,
        "--allow-short",
        help="Skip Gate A 7–12 min duration band (for test videos)",
    ),
    allow_placeholders: bool = typer.Option(
        False,
        "--allow-placeholders",
        help="Allow mock stills (tests only)",
    ),
) -> None:
    # Auto-discover job artifacts when given a job video path
    if job_dir is None:
        # .../job/video/final.mp4 → job dir
        if final.parent.name == "video":
            job_dir = final.parent.parent
    if script is None and job_dir is not None:
        cand = Path(job_dir) / "script" / "script.json"
        if cand.exists():
            script = cand
    if visual_manifest is None and job_dir is not None:
        cand = Path(job_dir) / "images" / "visual_manifest.json"
        if cand.exists():
            visual_manifest = cand
    if youtube_meta is None and job_dir is not None:
        cand = Path(job_dir) / "youtube_meta"
        if cand.is_dir():
            youtube_meta = cand

    console.print(f"[bold]PublishModule[/bold] {'DRY-RUN' if dry_run else 'UPLOAD'}")
    if youtube_meta:
        console.print(f"[dim]youtube_meta → {youtube_meta}[/dim]")
    try:
        result = PublishModule().publish_private(
            final_path=final,
            title=title,
            thumbnail_path=thumbnail,
            youtube_meta_dir=youtube_meta,
            script_path=script,
            visual_manifest=visual_manifest,
            job_dir=job_dir or final.parent,
            dry_run=dry_run,
            allow_short=allow_short,
            allow_placeholders=allow_placeholders,
        )
    except PublishModuleError as exc:
        console.print(f"[red]FAIL[/red] {exc}")
        raise typer.Exit(2) from exc
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    g = result.gate_a
    table = Table(title="Publish result")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("ok", "yes" if result.ok else "no")
    table.add_row("dry_run", "yes" if result.dry_run else "no")
    table.add_row("privacy", result.privacy_status)
    table.add_row("title", result.title)
    table.add_row("tags", str(len(result.tags)))
    table.add_row("video_id", result.video_id or "—")
    table.add_row("watch_url", result.watch_url or "—")
    table.add_row(
        "thumbnail",
        (
            "uploaded"
            if result.meta.get("thumbnail_uploaded")
            else (
                f"failed: {result.meta.get('thumbnail_error')}"
                if result.meta.get("thumbnail_error")
                else (result.meta.get("thumbnail_path") or "—")
            )
        ),
    )
    table.add_row(
        "gate_a",
        f"{'pass' if g.ok else 'fail'} {g.width}x{g.height} {g.duration_s:.1f}s"
        if g.duration_s is not None
        else ("pass" if g.ok else "fail"),
    )
    table.add_row("AI disclosure", "yes" if result.ai_disclosure else "no")
    console.print(table)
    for w in g.warnings:
        console.print(f"[yellow]warn[/yellow] {w}")
    console.print(f"[green]OK[/green] manifest → {result.publish_manifest_path}")


if __name__ == "__main__":
    app()
