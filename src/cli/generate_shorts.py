"""CLI: 40s YouTube Shorts from owned longform finals.

Examples:
  .venv/bin/python -m src.cli.generate_shorts --job output/jobs/JOBDIR --allow-video-reuse
  .venv/bin/python -m src.cli.generate_shorts harvest --channel napstorian --allow-video-reuse
  .venv/bin/python -m src.cli.generate_shorts publish --channel napstorian --allow-video-reuse --dry-run
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


def _fail_if_frozen(*, allow_video_reuse: bool) -> None:
    from src.content.derivatives import shorts_clip_allowed

    if shorts_clip_allowed(allow_video_reuse=allow_video_reuse):
        return
    console.print(
        "[red]youtube_shorts_clip frozen[/red] — pass --allow-video-reuse "
        "or set smm.allow_youtube_shorts_clip=true (does not unfreeze podcast)."
    )
    raise typer.Exit(2)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    job: Path | None = typer.Option(None, "--job", "-j", help="Job directory with video/final.mp4"),
    channel: str | None = typer.Option(None, "--channel", "-c"),
    all_production: bool = typer.Option(
        False, "--all-production", help="Every production_enabled empire channel"
    ),
    layout: str = typer.Option("blur_pillar", "--layout"),
    allow_video_reuse: bool = typer.Option(
        False, "--allow-video-reuse", help="Allow youtube_shorts_clip under QUALITY FREEZE"
    ),
    limit: int = typer.Option(3, "--limit"),
) -> None:
    """Pack a Short when --job is set; otherwise show subcommand help."""
    if ctx.invoked_subcommand is not None:
        return
    if job is None and not all_production and not channel:
        console.print(ctx.get_help())
        raise typer.Exit(0)
    _fail_if_frozen(allow_video_reuse=allow_video_reuse)
    from src.agents.channel_empire import production_channel_names
    from src.agents.shorts_publish import pack_and_gate
    from src.agents.store import OpsStore
    from src.services.shorts_packager import resolve_final_mp4
    from src.services.youtube_channel_auth import normalize_youtube_channel

    jobs: list[Path] = []
    if job is not None:
        jobs.append(job)
    else:
        chans = (
            list(production_channel_names())
            if all_production
            else [normalize_youtube_channel(channel)]
        )
        store = OpsStore()
        n = 0
        for j in store.list_jobs():
            meta = j.meta or {}
            ch = str(meta.get("channel") or meta.get("sheet_tab") or "")
            try:
                chn = normalize_youtube_channel(ch) if ch else ""
            except Exception:  # noqa: BLE001
                chn = ch
            if chn not in chans:
                continue
            if not j.job_dir:
                continue
            p = Path(j.job_dir)
            if resolve_final_mp4(p) is None:
                continue
            jobs.append(p)
            n += 1
            if n >= limit:
                break
    if not jobs:
        console.print("[yellow]no jobs with final.mp4[/yellow]")
        raise typer.Exit(1)
    payloads = []
    for jdir in jobs:
        payloads.append(pack_and_gate(jdir, layout=layout))
    console.print_json(data={"n": len(payloads), "results": payloads})


@app.command("harvest")
def harvest_cmd(
    channel: str | None = typer.Option(None, "--channel", "-c"),
    all_production: bool = typer.Option(False, "--all-production"),
    allow_video_reuse: bool = typer.Option(False, "--allow-video-reuse"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    limit: int | None = typer.Option(None, "--limit"),
) -> None:
    """Invent parent-bound Shorts titles onto {channel}_shorts."""
    _fail_if_frozen(allow_video_reuse=allow_video_reuse)
    from src.agents.channel_empire import production_channel_names
    from src.agents.sheet_channels import ensure_shorts_tabs
    from src.agents.shorts_harvest import harvest_shorts_titles
    from src.services.youtube_channel_auth import normalize_youtube_channel

    ensure_shorts_tabs()
    chans = (
        list(production_channel_names())
        if all_production or not channel
        else [normalize_youtube_channel(channel)]
    )
    out = {
        ch: harvest_shorts_titles(channel=ch, dry_run=dry_run, limit=limit) for ch in chans
    }
    console.print_json(data=out)


@app.command("publish")
def publish_cmd(
    channel: str | None = typer.Option(None, "--channel", "-c"),
    all_production: bool = typer.Option(False, "--all-production"),
    allow_video_reuse: bool = typer.Option(False, "--allow-video-reuse"),
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run"),
    limit: int = typer.Option(1, "--limit"),
) -> None:
    """Pack + Gate S + private upload; auto-public when parent is public."""
    _fail_if_frozen(allow_video_reuse=allow_video_reuse)
    from src.agents.channel_empire import production_channel_names
    from src.agents.shorts_publish import publish_ready_shorts
    from src.services.youtube_channel_auth import normalize_youtube_channel

    ch = None if all_production or not channel else normalize_youtube_channel(channel)
    payload = publish_ready_shorts(
        channel=ch,
        dry_run=dry_run,
        allow_video_reuse=allow_video_reuse,
        limit=limit,
    )
    console.print_json(data=payload)


@app.command("ensure-tabs")
def ensure_tabs_cmd() -> None:
    """Create {channel}_shorts tabs for every empire channel."""
    from src.agents.sheet_channels import ensure_shorts_tabs

    console.print_json(data=ensure_shorts_tabs())


if __name__ == "__main__":
    app()
