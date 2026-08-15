"""1-minute stream beat: health + status card + optional playlist refresh.

Cron: ``* * * * *`` → ``python -m src.cli.stream_beat``

- Runs ``vod_loop.healthcheck_all`` (systemd remains primary restart owner)
- Writes ``output/ops/STREAM_STATUS.md`` + refreshes status JSON
- Optionally refreshes playlists via ``vod_picker`` on a safe interval
  (between loops / when not mid-stall kill — picker only rewrites files;
  supervise picks up on next ffmpeg restart / concat loop boundary)

Never prints RTMP keys. Never touches gpu_lock / RunPod.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.streaming import vod_loop
from src.streaming.vod_picker import refresh_playlists

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "output" / "ops"
STATUS_MD = OPS / "STREAM_STATUS.md"
BEAT_JSON = OPS / "stream_beat.json"
BEAT_LOCK = OPS / "stream_beat.lock"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_truthy(name: str, default: str = "0") -> bool:
    return (os.getenv(name) or default).strip().lower() in {"1", "true", "yes", "on"}


def _try_lock(ttl_sec: float = 50.0) -> bool:
    """Exclusive flock so overlapping cron ticks don't stack healthchecks."""
    import fcntl

    OPS.mkdir(parents=True, exist_ok=True)
    now = time.time()
    try:
        fd = open(BEAT_LOCK, "a+", encoding="utf-8")
    except OSError:
        return False
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            # Stale lock: holder died without unlock — reclaim if mtime ancient.
            age = now - BEAT_LOCK.stat().st_mtime
            if age < ttl_sec:
                fd.close()
                return False
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            fd.close()
            return False
    try:
        fd.seek(0)
        fd.truncate()
        fd.write(str(now))
        fd.flush()
    except Exception:
        pass
    # Keep fd open for process lifetime (cron is short-lived); close on exit is fine.
    # Store on function to prevent GC closing the fd mid-beat.
    _try_lock._fd = fd  # type: ignore[attr-defined]
    return True


def write_stream_status_md(status: dict[str, Any], health: dict[str, Any]) -> Path:
    OPS.mkdir(parents=True, exist_ok=True)
    enc = status.get("encode") or {}
    lines = [
        "# STREAM STATUS",
        "",
        f"_Updated: `{_utc_now()}` · module `stream_beat` · no keys_",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| vod_loop enabled | `{status.get('enabled')}` |",
        f"| encode | `{enc.get('bitrate')}@{enc.get('height')}p{enc.get('fps')}` |",
        f"| health ok | `{health.get('ok')}` |",
        "",
        "## Channels",
        "",
        "| Channel | alive | supervise | rtmp_ready | playlist_ok | action | bitrate | restarts | ffmpeg_n |",
        "|---------|-------|-----------|------------|-------------|--------|---------|----------|----------|",
    ]
    channels = status.get("channels") or {}
    for ch in vod_loop.CHANNELS:
        row = channels.get(ch) or {}
        h = (row.get("extra") or {}).get("health") or {}
        try:
            ff_n = len(vod_loop.list_channel_ffmpeg_pids(ch))
        except Exception:
            ff_n = "—"
        lines.append(
            "| {ch} | {alive} | {sup} | {rtmp} | {pl} | `{act}` | {br} | {rs}/{ss} | {ff} |".format(
                ch=ch,
                alive=row.get("alive"),
                sup=row.get("supervisor_alive"),
                rtmp=row.get("rtmp_ready"),
                pl=row.get("playlist_ok"),
                act=row.get("action") or "—",
                br=h.get("last_bitrate") or "—",
                rs=h.get("restarts") or 0,
                ss=h.get("stall_restarts") or 0,
                ff=ff_n,
            )
        )
    lines.extend(
        [
            "",
            "## Go-live",
            "",
            "If `rtmp_ready=false`, set stream keys in `.env` (never in chat) then:",
            "`sudo systemctl enable --now vod-loop@napstorian`",
            "",
            "Dual-ingest (Studio: more than one ingestion on primary URL): usually a second "
            "VPS supervise/ffmpeg beside systemd (do not assume StreamCast). "
            "`ensure_single_ingest` keeps **one** primary encode. "
            "Watchdog re-lives via `systemctl restart vod-loop@CHANNEL` on stall / "
            "bitrate collapse / dual ingest / dead encode. Notes: `vod_loop_repair_notes.md`. "
            "Encode is infinite `-stream_loop -1` on playlist **head** (featured VOD); "
            "SMM soft-swaps underperforming / weak watch-time VODs to next most-viewed — "
            "see `smm_live_alerts.md` / `LIVE_UNLOCK.md`.",
            "",
            "Docs: [`LIVE_UNLOCK.md`](LIVE_UNLOCK.md) · [`VOD_LOOP.md`](VOD_LOOP.md)",
            "",
            "Automation: `stream_beat` cron `*/1` healthchecks; SMM `watch_live_streams` ~15m "
            "(encode repair + soft underperforming/watch-time VOD swap); "
            "`vod_picker` re-ranks views×AVD longform ~30m.",
            "",
        ]
    )
    swap_last = OPS / "smm_live_vod_swap_last.json"
    if swap_last.is_file():
        try:
            swap = json.loads(swap_last.read_text(encoding="utf-8"))
            lines.extend(
                [
                    "## Last SMM Live VOD swap",
                    "",
                    f"- at: `{swap.get('at')}`",
                    f"- channel: `{swap.get('channel')}`",
                    f"- changed: `{swap.get('changed')}` reason: `{swap.get('reason')}`",
                    f"- nudge: `{(swap.get('nudge') or {}).get('action')}`",
                    "",
                ]
            )
        except Exception:
            pass
    STATUS_MD.write_text("\n".join(lines), encoding="utf-8")
    return STATUS_MD


def _should_refresh_playlist(now: float) -> bool:
    if not _env_truthy("STREAM_BEAT_AUTO_PICK", "1"):
        return False
    # Default 30m so SMM views can reshuffle Live toward top performers.
    interval = float(os.getenv("STREAM_BEAT_PICK_INTERVAL_SEC") or 1800)
    marker = OPS / "stream_beat_last_pick.ts"
    if not marker.is_file():
        return True
    try:
        last = float(marker.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        return True
    return (now - last) >= interval


def _mark_pick(now: float) -> None:
    (OPS / "stream_beat_last_pick.ts").write_text(str(now), encoding="utf-8")


def _should_smm_watch(now: float) -> bool:
    if not _env_truthy("STREAM_BEAT_SMM_WATCH", "1"):
        return False
    # Default 15m — align with Live soft underperform evaluate cadence.
    interval = float(os.getenv("STREAM_BEAT_SMM_INTERVAL_SEC") or 900)
    marker = OPS / "stream_beat_last_smm.ts"
    if not marker.is_file():
        return True
    try:
        last = float(marker.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        return True
    return (now - last) >= interval


def _mark_smm(now: float) -> None:
    (OPS / "stream_beat_last_smm.ts").write_text(str(now), encoding="utf-8")


def run_beat(
    *,
    dry_run: bool = False,
    refresh_playlist: bool | None = None,
    pick_limit: int | None = None,
    smm_watch: bool | None = None,
) -> dict[str, Any]:
    if not _try_lock():
        return {
            "ok": True,
            "ts": _utc_now(),
            "module": "stream_beat",
            "skipped": "lock_held",
        }

    status = vod_loop.run_status(ensure_playlist=True)
    health = vod_loop.healthcheck_all(dry_run=dry_run)
    # Merge health actions into status card view
    for ch, row in (health.get("channels") or {}).items():
        if ch in (status.get("channels") or {}):
            status["channels"][ch]["action"] = row.get("action")
            status["channels"][ch]["message"] = row.get("message") or ""

    write_stream_status_md(status, health)

    now = time.time()
    smm_payload: dict[str, Any] | None = None
    do_smm = smm_watch if smm_watch is not None else _should_smm_watch(now)
    if do_smm and not dry_run:
        try:
            from src.agents.smm_agent import SocialMediaManager

            smm_payload = SocialMediaManager().watch_live_streams()
            _mark_smm(now)
        except Exception as exc:  # noqa: BLE001
            smm_payload = {"ok": False, "error": str(exc)}

    do_pick = (
        refresh_playlist
        if refresh_playlist is not None
        else _should_refresh_playlist(now)
    )
    pick_payload: dict[str, Any] | None = None
    if do_pick:
        # Safe rewrite: playlist file only; ffmpeg concat re-reads on restart.
        # Skip rewrite while channel reports stall / reconnect storm to avoid thrash.
        busy = False
        for ch, row in (health.get("channels") or {}).items():
            action = str(row.get("action") or "")
            if action.startswith("stall") or "reconnect" in action or action.startswith(
                "relive"
            ):
                busy = True
                break
            try:
                perf = vod_loop.performance_issues(ch)
                issues = set(perf.get("issues") or [])
                if issues & {"stall", "reconnect_storm"}:
                    busy = True
                    break
            except Exception:
                pass
        if not busy:
            from src.streaming.vod_picker import default_pick_limit

            pick_payload = refresh_playlists(
                limit=pick_limit if pick_limit is not None else default_pick_limit(),
                dry_run=dry_run,
            )
            if not dry_run:
                _mark_pick(now)
        else:
            pick_payload = {"ok": True, "skipped": "stall_busy"}

    payload = {
        "ok": bool(health.get("ok")),
        "ts": _utc_now(),
        "module": "stream_beat",
        "dry_run": dry_run,
        "status_md": str(STATUS_MD),
        "health_ok": health.get("ok"),
        "any_rtmp_ready": any(
            bool((row or {}).get("rtmp_ready"))
            for row in (status.get("channels") or {}).values()
        ),
        "channels": {
            ch: {
                "alive": (status.get("channels") or {}).get(ch, {}).get("alive"),
                "rtmp_ready": (status.get("channels") or {})
                .get(ch, {})
                .get("rtmp_ready"),
                "playlist_ok": (status.get("channels") or {})
                .get(ch, {})
                .get("playlist_ok"),
                "action": (health.get("channels") or {}).get(ch, {}).get("action"),
            }
            for ch in vod_loop.CHANNELS
        },
        "smm_watch": smm_payload,
        "playlist_refresh": pick_payload,
    }
    if not dry_run:
        BEAT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def install_crontab() -> Path:
    """Install ``* * * * *`` stream_beat (idempotent merge)."""
    OPS.mkdir(parents=True, exist_ok=True)
    py = ROOT / ".venv" / "bin" / "python"
    if not py.is_file():
        py = Path("python3")
    line = (
        f"* * * * * cd {ROOT.as_posix()} && {py.as_posix()} -m src.cli.stream_beat "
        f">> {OPS.as_posix()}/stream_beat.log 2>&1"
    )
    marker = "src.cli.stream_beat"
    hint = (
        "# === stream_beat */1 (vod_loop health + status card + optional vod_picker) ===\n"
        f"{line}\n"
    )
    hint_path = OPS / "crontab_stream_beat.txt"
    hint_path.write_text(hint, encoding="utf-8")

    import subprocess

    existing = ""
    try:
        existing = subprocess.check_output(["crontab", "-l"], text=True)
    except subprocess.CalledProcessError:
        existing = ""
    lines = existing.splitlines()
    kept = [ln for ln in lines if marker not in ln]
    kept = [ln for ln in kept if "stream_beat */1" not in ln]
    if kept and kept[-1].strip():
        kept.append("")
    kept.extend(hint.strip().splitlines())
    kept.append("")
    body = "\n".join(kept) + "\n"
    subprocess.run(
        ["crontab", "-"],
        input=body,
        check=True,
        text=True,
        capture_output=True,
    )
    return hint_path
