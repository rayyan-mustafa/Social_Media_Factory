"""CLI: stream beat + vod picker (Phase 1 Live brain).

Usage::

  .venv/bin/python -m src.cli.stream_beat
  .venv/bin/python -m src.cli.stream_beat --json
  .venv/bin/python -m src.cli.stream_beat --dry-run --refresh-playlist
  .venv/bin/python -m src.cli.stream_beat crontab --install
  .venv/bin/python -m src.cli.vod_picker --channel napstorian --json
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.streaming.stream_beat import install_crontab, run_beat  # noqa: E402
from src.streaming.vod_picker import (  # noqa: E402
    CHANNELS,
    default_pick_limit,
    rank_vods,
    refresh_playlists,
)

app = typer.Typer(add_completion=False, help="1-minute stream beat + VOD picker")
console = Console()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    json_out: bool = typer.Option(False, "--json"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    refresh_playlist: Optional[bool] = typer.Option(
        None,
        "--refresh-playlist/--no-refresh-playlist",
        help="Force or skip playlist rewrite this tick",
    ),
    smm_watch: Optional[bool] = typer.Option(
        None,
        "--smm-watch/--no-smm-watch",
        help="Force or skip SMM live watch + views cache refresh",
    ),
    pick_limit: int = typer.Option(
        None,
        "--pick-limit",
        min=1,
        max=20,
        help="Ranked playlist pool size (default: VOD_PICKER_LIMIT or 8)",
    ),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    payload = run_beat(
        dry_run=dry_run,
        refresh_playlist=refresh_playlist,
        pick_limit=pick_limit if pick_limit is not None else default_pick_limit(),
        smm_watch=smm_watch,
    )
    if json_out:
        console.print_json(data=payload)
    else:
        console.print(
            f"[bold]stream_beat[/bold] ok={payload.get('ok')} "
            f"any_rtmp_ready={payload.get('any_rtmp_ready')} "
            f"skipped={payload.get('skipped')}"
        )
        for ch, row in (payload.get("channels") or {}).items():
            console.print(
                f"  {ch}: alive={row.get('alive')} rtmp={row.get('rtmp_ready')} "
                f"playlist={row.get('playlist_ok')} action={row.get('action')}"
            )
        smm = payload.get("smm_watch")
        if smm:
            console.print(
                f"  smm_watch ok={smm.get('ok')} healthy={smm.get('healthy')} "
                f"issues={len(smm.get('issues') or [])}"
            )
        pr = payload.get("playlist_refresh")
        if pr:
            console.print(f"  playlist_refresh ok={pr.get('ok')} skipped={pr.get('skipped')}")


@app.command("crontab")
def crontab_cmd(
    install: bool = typer.Option(False, "--install"),
) -> None:
    """Show or install */1 stream_beat cron line."""
    if install:
        path = install_crontab()
        console.print(f"installed; hint={path}")
    else:
        console.print(
            "Install with: python -m src.cli.stream_beat crontab --install\n"
            "Keeps */10 vod_loop healthcheck as backup."
        )


def _vod_picker_app() -> typer.Typer:
    papp = typer.Typer(add_completion=False, help="Rank/write own-VOD Live playlists")

    @papp.callback(invoke_without_command=True)
    def _pick(
        ctx: typer.Context,
        channel: Optional[str] = typer.Option(None, "--channel"),
        limit: int = typer.Option(
            None,
            "--limit",
            min=1,
            max=20,
            help="Ranked playlist pool size (default: VOD_PICKER_LIMIT or 8)",
        ),
        dry_run: bool = typer.Option(False, "--dry-run"),
        json_out: bool = typer.Option(False, "--json"),
        write: bool = typer.Option(True, "--write/--no-write"),
    ) -> None:
        if ctx.invoked_subcommand is not None:
            return
        chans = (channel,) if channel else CHANNELS
        if channel and channel not in CHANNELS:
            console.print(f"unknown channel {channel}; want {CHANNELS}")
            raise typer.Exit(2)
        lim = limit if limit is not None else default_pick_limit()
        if write and not dry_run:
            payload = refresh_playlists(channels=chans, limit=lim, dry_run=False)
        else:
            payload = {
                "ok": True,
                "dry_run": True,
                "limit": lim,
                "channels": {
                    ch: {
                        "ok": True,
                        "ranked": [
                            {
                                "path": c.path,
                                "score": c.score,
                                "duration_sec": c.duration_sec,
                                "views": c.views,
                                "reasons": c.reasons,
                            }
                            for c in rank_vods(ch, limit=lim)
                        ],
                    }
                    for ch in chans
                },
            }
        if json_out:
            console.print_json(data=payload)
        else:
            console.print(f"[bold]vod_picker[/bold] ok={payload.get('ok')}")
            for ch, row in (payload.get("channels") or {}).items():
                console.print(f"  {ch}: ok={row.get('ok')} entries={row.get('entries') or row.get('ranked')}")

    return papp


# Alternate entry: python -m src.cli.vod_picker
vod_picker_app = _vod_picker_app()


if __name__ == "__main__":
    app()
