"""CLI: 24/7 VOD-loop restream healthcheck / start / stop / supervise.

Usage::

  .venv/bin/python -m src.cli.vod_loop status --json
  .venv/bin/python -m src.cli.vod_loop smoke
  .venv/bin/python -m src.cli.vod_loop healthcheck --json
  .venv/bin/python -m src.cli.vod_loop start --channel napstorian
  .venv/bin/python -m src.cli.vod_loop stop --channel napstorian
  .venv/bin/python -m src.cli.vod_loop supervise --channel napstorian
  .venv/bin/python -m src.cli.vod_loop crontab --install
  .venv/bin/python -m src.cli.vod_loop systemd --write
  .venv/bin/python -m src.cli.vod_loop key-hygiene

CPU-only. Separate from RunPod GPU farm. Missing RTMP keys → skip / dry-run.
Never prints stream keys.
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

from src.streaming.vod_loop import (  # noqa: E402
    CHANNELS,
    adaptive_bitrate_tick,
    healthcheck_all,
    install_crontab,
    install_crontab_hint,
    install_systemd_hint,
    load_bitrate_policy,
    run_status,
    smoke_without_rtmp,
    start_channel,
    stop_channel,
    supervise_channel,
    write_systemd_unit,
)

app = typer.Typer(
    add_completion=False,
    help="VOD-loop YouTube Live restream (cheap 24/7; CPU only)",
)
console = Console()

_KEY_HYGIENE = """
## YouTube Live key rotation + StreamCast hygiene (do NOT paste keys here)

Production Live = VPS vod_loop (systemd). StreamCast desktop GUI is NOT production.
Retire StreamCast for prod after 48–72h green on VPS — see output/ops/VOD_LOOP.md.

1) Rotate keys in YouTube Studio (each channel):
   - Studio → Go live → Stream → Stream key → Reset / manage
   - Copy the NEW key only into VPS ~/new_yt_automation/.env as
     YT_LIVE_RTMP_KEY_NAPSTORIAN / YT_LIVE_RTMP_KEY_NAPPING_HISTORIAN
   - Never commit .env; never paste keys into chat / git / Agent Bus

2) Redact local StreamCast sessions on the HP laptop (backup then wipe):
   PowerShell (from repo root):
     .\\scripts\\redact_streamcast_sessions.ps1
   Optional explicit path:
     .\\scripts\\redact_streamcast_sessions.ps1 -SessionsPath "$env:APPDATA\\com.eagle.streaming-app\\sessions.json"
   The script copies sessions.json → sessions.json.bak-TIMESTAMP then clears
   stream_key / streamKey / rtmp_key style fields in-place. It never uploads.

3) Restart production path (VPS only):
   sudo systemctl restart vod-loop@napstorian vod-loop@napping_historian
   # cron */10 healthcheck is optional backup; systemd is primary
""".strip()


def _print_channels(payload: dict) -> None:
    channels = payload.get("channels") or {}
    for ch in CHANNELS:
        row = channels.get(ch) or {}
        health = (row.get("extra") or {}).get("health") or {}
        console.print(
            f"  [bold]{ch}[/bold] action={row.get('action')} "
            f"alive={row.get('alive')} supervise={row.get('supervisor_alive')} "
            f"rtmp_ready={row.get('rtmp_ready')} "
            f"playlist_ok={row.get('playlist_ok')} pid={row.get('pid')}"
        )
        if health:
            console.print(
                f"    [dim]restarts={health.get('restarts')} "
                f"stall_restarts={health.get('stall_restarts')} "
                f"last_progress={health.get('last_progress_ts')} "
                f"bitrate={health.get('last_bitrate')}[/dim]"
            )
        msg = row.get("message")
        if msg:
            # Never echo anything that looks like an RTMP URL with a key path.
            safe = msg
            if "rtmp://" in safe.lower() or "live2/" in safe.lower():
                safe = "[redacted message containing rtmp-like text]"
            console.print(f"    [dim]{safe}[/dim]")


@app.command("status")
def status_cmd(
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Write/read ops status JSON (bootstraps default playlists)."""
    payload = run_status(ensure_playlist=True)
    if json_out:
        console.print_json(data=payload)
    else:
        enc = payload.get("encode") or {}
        console.print(
            f"[bold]vod_loop[/bold] enabled={payload.get('enabled')} "
            f"encode={enc.get('bitrate')}@{enc.get('height')}p{enc.get('fps')} "
            f"ts={payload.get('ts')}"
        )
        _print_channels(payload)


@app.command("smoke")
def smoke_cmd(
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Smoke test without RTMP: playlists + 2s local encode + healthcheck skip path."""
    payload = smoke_without_rtmp()
    if json_out:
        console.print_json(data=payload)
    else:
        console.print(
            f"[bold]vod_loop smoke[/bold] ok={payload.get('ok')} "
            f"(no RTMP required) encode={((payload.get('encode') or {}).get('bitrate'))}"
        )
        for ch, row in (payload.get("encode_smoke") or {}).items():
            console.print(f"  {ch}: ok={row.get('ok')} {row.get('out') or row.get('detail')}")
        for ch, row in (payload.get("status_summary") or {}).items():
            console.print(
                f"  {ch}: health={row.get('action')} rtmp_ready={row.get('rtmp_ready')}"
            )
    if not payload.get("ok"):
        raise typer.Exit(2)


@app.command("healthcheck")
def healthcheck_cmd(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Do not start ffmpeg / restart units; still report + adaptive decisions",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Cron entrypoint: restart dead/stalled loops; adaptive bitrate step; skip if RTMP keys absent."""
    payload = healthcheck_all(dry_run=dry_run)
    if json_out:
        console.print_json(data=payload)
    else:
        console.print(
            f"[bold]vod_loop healthcheck[/bold] ok={payload.get('ok')} "
            f"dry_run={dry_run}"
        )
        _print_channels(payload)
        adaptive = payload.get("adaptive") or {}
        cpu = adaptive.get("cpu") or {}
        console.print(
            f"  [dim]adaptive enabled={adaptive.get('enabled')} "
            f"changed={adaptive.get('changed')} "
            f"cpu_ok={cpu.get('ok')} load1={cpu.get('load1')} "
            f"limit={cpu.get('limit')} ladder={adaptive.get('ladder_k')}[/dim]"
        )
        for ch, row in (adaptive.get("channels") or {}).items():
            console.print(
                f"    [dim]{ch}: {row.get('action')} "
                f"rung={row.get('rung')} bitrate_k={row.get('bitrate_k')} "
                f"streak={row.get('healthy_streak')}[/dim]"
            )
    if not payload.get("ok"):
        raise typer.Exit(2)


@app.command("adaptive")
def adaptive_cmd(
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--apply",
        help="Default dry-run (no restart / no policy write). --apply persists + may restart.",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show / apply adaptive bitrate ladder decisions (never prints RTMP keys)."""
    policy = load_bitrate_policy(ensure=True)
    payload = adaptive_bitrate_tick(dry_run=dry_run)
    if json_out:
        console.print_json(data={"policy_summary": {
            "enabled": policy.get("enabled"),
            "ladder_k": policy.get("ladder_k"),
            "healthy_streak_needed": policy.get("healthy_streak_needed"),
            "channels": {
                ch: {
                    "rung": (policy.get("channels") or {}).get(ch, {}).get("rung"),
                    "bitrate_k": (policy.get("channels") or {}).get(ch, {}).get("bitrate_k"),
                    "healthy_streak": (policy.get("channels") or {}).get(ch, {}).get(
                        "healthy_streak"
                    ),
                }
                for ch in CHANNELS
            },
        }, "tick": payload})
    else:
        cpu = payload.get("cpu") or {}
        console.print(
            f"[bold]vod_loop adaptive[/bold] dry_run={dry_run} "
            f"enabled={payload.get('enabled')} changed={payload.get('changed')}"
        )
        console.print(
            f"  cpu_ok={cpu.get('ok')} load1={cpu.get('load1')} "
            f"cores={cpu.get('cores')} limit={cpu.get('limit')} "
            f"ladder={payload.get('ladder_k')}"
        )
        for ch, row in (payload.get("channels") or {}).items():
            console.print(
                f"  {ch}: action={row.get('action')} rung={row.get('rung')} "
                f"bitrate_k={row.get('bitrate_k')} streak={row.get('healthy_streak')}/"
                f"{row.get('healthy_streak_needed')} "
                f"stream_ok={(row.get('stream') or {}).get('ok')}"
            )
        console.print(f"  [dim]policy={payload.get('policy_path')}[/dim]")


@app.command("start")
def start_cmd(
    channel: Optional[str] = typer.Option(
        None,
        "--channel",
        "-c",
        help="napstorian | napping_historian (default: all)",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force", help="Restart if already running"),
    oneshot: bool = typer.Option(
        False,
        "--oneshot",
        help="Single FFmpeg process (no supervise loop); prefer supervise/systemd",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Start supervised reconnect loop (default) for one or both channels."""
    targets = [channel] if channel else list(CHANNELS)
    for ch in targets:
        if ch not in CHANNELS:
            console.print(f"[red]unknown channel[/red] {ch}; expected {CHANNELS}")
            raise typer.Exit(2)
    results = {
        ch: start_channel(ch, dry_run=dry_run, force=force, oneshot=oneshot).to_dict()
        for ch in targets
    }
    payload = {
        "ok": True,
        "action": "start",
        "dry_run": dry_run,
        "oneshot": oneshot,
        "channels": results,
    }
    run_status(ensure_playlist=False)
    if json_out:
        console.print_json(data=payload)
    else:
        console.print(f"[bold]vod_loop start[/bold] dry_run={dry_run} oneshot={oneshot}")
        _print_channels(payload)


@app.command("stop")
def stop_cmd(
    channel: Optional[str] = typer.Option(
        None,
        "--channel",
        "-c",
        help="napstorian | napping_historian (default: all)",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Stop supervise + FFmpeg loop process(es)."""
    targets = [channel] if channel else list(CHANNELS)
    for ch in targets:
        if ch not in CHANNELS:
            console.print(f"[red]unknown channel[/red] {ch}")
            raise typer.Exit(2)
    results = {ch: stop_channel(ch).to_dict() for ch in targets}
    payload = {"ok": True, "action": "stop", "channels": results}
    run_status(ensure_playlist=False)
    if json_out:
        console.print_json(data=payload)
    else:
        console.print("[bold]vod_loop stop[/bold]")
        _print_channels(payload)


@app.command("supervise")
def supervise_cmd(
    channel: str = typer.Option(..., "--channel", "-c", help="napstorian | napping_historian"),
    once: bool = typer.Option(
        False,
        "--once",
        help="Single restart cycle then exit (tests); default = infinite",
    ),
) -> None:
    """Foreground infinite supervise loop (systemd ExecStart target)."""
    if channel not in CHANNELS:
        console.print(f"[red]unknown channel[/red] {channel}; expected {CHANNELS}")
        raise typer.Exit(2)
    console.print(
        f"[bold]vod_loop supervise[/bold] channel={channel} "
        f"(infinite backoff; never prints RTMP keys)"
    )
    result = supervise_channel(channel, once=once)
    console.print(f"supervise finished cycles={result.get('cycles')} stopped={result.get('stopped')}")


@app.command("sync-live-meta")
def sync_live_meta_cmd(
    channel: Optional[str] = typer.Option(
        None,
        "--channel",
        "-c",
        help="napstorian | napping_historian (default: both)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Bypass idempotent/cooldown skip",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Reuse featured VOD title/description/thumbnail on the Live broadcast(s)."""
    from src.streaming.live_broadcast_meta import (
        sync_all_live_meta,
        sync_channel_live_meta,
    )

    if channel:
        if channel not in CHANNELS:
            console.print(f"[red]unknown channel[/red] {channel}")
            raise typer.Exit(2)
        payload = {
            "ok": False,
            "channels": {
                channel: sync_channel_live_meta(
                    channel, force=force, dry_run=dry_run
                )
            },
        }
        payload["ok"] = bool(payload["channels"][channel].get("ok"))
    else:
        payload = sync_all_live_meta(force=force, dry_run=dry_run)
    if json_out:
        console.print_json(data=payload)
    else:
        console.print("[bold]vod_loop sync-live-meta[/bold]")
        for ch, row in (payload.get("channels") or {}).items():
            meta = (row or {}).get("meta") or {}
            console.print(
                f"  {ch}: action={(row or {}).get('action')} "
                f"bcast={(row or {}).get('broadcast_id') or '—'} "
                f"title={meta.get('title') or '—'} "
                f"thumb_src={meta.get('thumbnail_src') or ((row or {}).get('apply') or {}).get('thumbnail_skipped') or '—'}"
            )


@app.command("crontab")
def crontab_cmd(
    install: bool = typer.Option(
        False,
        "--install",
        help="Arm */10 healthcheck under 365d always-on crontab",
    ),
) -> None:
    """Print or install vod_loop healthcheck cron line (optional backup; systemd primary)."""
    console.print(install_crontab_hint())
    console.print(
        "[dim]Optional safety net alongside systemd Restart=always (primary). "
        "Requires YT_LIVE_RTMP_* in .env to actually restream. "
        "See output/ops/VOD_LOOP.md[/dim]"
    )
    if install:
        path = install_crontab()
        console.print(f"[green]Installed[/green] → {path}")


@app.command("systemd")
def systemd_cmd(
    write: bool = typer.Option(
        True,
        "--write/--no-write",
        help="Write rendered unit under output/ops/vod-loop@.service",
    ),
    user: str = typer.Option("ubuntu", "--user", help="systemd User="),
) -> None:
    """Render vod-loop@.service (Restart=always). Needs sudo to install on VPS."""
    if write:
        path = write_systemd_unit(user=user)
        console.print(f"[green]Wrote[/green] {path}")
    console.print(install_systemd_hint())


@app.command("key-hygiene")
def key_hygiene_cmd() -> None:
    """Print key rotation + StreamCast redaction steps (never prints secrets)."""
    console.print(_KEY_HYGIENE)


if __name__ == "__main__":
    app()
