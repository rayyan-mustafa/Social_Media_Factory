"""Cinematic SFX mixer for EditModule (napstorian-first, $0 Mixkit/CC0 pack).

Moments (when enabled for channel):
  - cold-open whoosh + impact at t≈0
  - chapter plate hits at chapter boundaries
  - soft whoosh on map/infographic overlay starts
  - optional very soft ambient bed ducked under voice (amix, not sidechain)

Historian stays calm: default OFF (see ``resolve_compose_sfx``).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal

from src.services.settings import ROOT, get_settings

logger = logging.getLogger(__name__)

SFX_DIR = ROOT / "assets" / "sfx"

SfxKind = Literal[
    "cold_whoosh",
    "cold_impact",
    "chapter_hit",
    "infographic_whoosh",
    "ambient_bed",
]

# Relative filenames under assets/sfx/ (placeholders or Mixkit refresh pack).
_ASSET_FILES: dict[SfxKind, str] = {
    "cold_whoosh": "cold_whoosh.wav",
    "cold_impact": "cold_impact.wav",
    "chapter_hit": "chapter_hit.wav",
    "infographic_whoosh": "soft_whoosh.wav",
    "ambient_bed": "ambient_bed.wav",
}

# Default gains — always under Kokoro voice.
_DEFAULT_DB: dict[SfxKind, float] = {
    "cold_whoosh": -14.0,
    "cold_impact": -11.0,
    "chapter_hit": -15.0,
    "infographic_whoosh": -18.0,
    "ambient_bed": -28.0,
}


@dataclass
class SfxEvent:
    kind: SfxKind
    at_s: float
    path: Path
    volume_db: float


@dataclass
class ComposeSfxPlan:
    enabled: bool
    channel: str
    events: list[SfxEvent] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_meta(self) -> dict[str, Any]:
        return {
            "compose_sfx": self.enabled,
            "channel": self.channel,
            "event_count": len(self.events),
            "events": [
                {
                    "kind": e.kind,
                    "at_s": round(float(e.at_s), 3),
                    "file": e.path.name,
                    "volume_db": e.volume_db,
                }
                for e in self.events
            ],
            **self.meta,
        }


class ComposeSfxError(RuntimeError):
    pass


def resolve_compose_sfx(channel: str | None, settings: Any | None = None) -> bool:
    """Channel-aware gate: napstorian ON by default; historian OFF.

    ``COMPOSE_SFX=0`` master-disables all channels.
    ``COMPOSE_SFX_NAPSTORIAN=1`` / ``COMPOSE_SFX_HISTORIAN=0`` per channel.
    """
    s = settings or get_settings()
    if not bool(getattr(s, "compose_sfx", True)):
        return False
    ch = (channel or "napstorian").strip().lower()
    if ch in {"napping_historian", "historian", "sleep"}:
        return bool(getattr(s, "compose_sfx_historian", False))
    # napstorian + unknown → napstorian flag
    return bool(getattr(s, "compose_sfx_napstorian", True))


def ensure_sfx_assets(sfx_dir: Path | None = None) -> dict[str, Path]:
    """Ensure cinematic pack exists; generate silence-safe placeholders if missing."""
    root = Path(sfx_dir) if sfx_dir else Path(
        getattr(get_settings(), "compose_sfx_dir", None) or SFX_DIR
    )
    root.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for kind, name in _ASSET_FILES.items():
        path = root / name
        if not path.exists() or path.stat().st_size < 500:
            _generate_placeholder(kind, path)
        out[kind] = path
    return out


def plan_compose_sfx(
    *,
    channel: str,
    scene_clips: list[Any],
    chapter_boundary_after: set[int] | None = None,
    overlays: list[dict[str, Any]] | None = None,
    settings: Any | None = None,
) -> ComposeSfxPlan:
    """Build timed SFX events from clip durations + overlay / chapter markers."""
    s = settings or get_settings()
    enabled = resolve_compose_sfx(channel, s)
    plan = ComposeSfxPlan(
        enabled=enabled,
        channel=(channel or "napstorian").strip().lower(),
        meta={"assets_dir": str(Path(getattr(s, "compose_sfx_dir", None) or SFX_DIR))},
    )
    if not enabled:
        plan.meta["reason"] = "compose_sfx_disabled_for_channel"
        return plan

    assets = ensure_sfx_assets(Path(getattr(s, "compose_sfx_dir", None) or SFX_DIR))
    durations = [_clip_duration(c) for c in scene_clips]
    starts: list[float] = []
    t = 0.0
    for d in durations:
        starts.append(t)
        t += d
    total = t if t > 0 else 0.0
    plan.meta["timeline_s"] = round(total, 3)

    events: list[SfxEvent] = []

    # Cold open — whoosh then impact
    events.append(
        SfxEvent(
            "cold_whoosh",
            0.05,
            assets["cold_whoosh"],
            float(getattr(s, "compose_sfx_whoosh_db", _DEFAULT_DB["cold_whoosh"])),
        )
    )
    events.append(
        SfxEvent(
            "cold_impact",
            0.35,
            assets["cold_impact"],
            float(getattr(s, "compose_sfx_impact_db", _DEFAULT_DB["cold_impact"])),
        )
    )

    # Chapter plate hits
    boundaries = chapter_boundary_after or set()
    for pos in sorted(boundaries):
        # Boundary after clip index ``pos`` → next clip start
        nxt = pos + 1
        if 0 <= nxt < len(starts):
            events.append(
                SfxEvent(
                    "chapter_hit",
                    max(0.0, starts[nxt] - 0.05),
                    assets["chapter_hit"],
                    float(getattr(s, "compose_sfx_plate_db", _DEFAULT_DB["chapter_hit"])),
                )
            )

    # Infographic / map soft whooshes
    index_to_pos: dict[int, int] = {}
    for pos, clip in enumerate(scene_clips):
        raw_idx = None
        if hasattr(clip, "index"):
            raw_idx = getattr(clip, "index")
        elif isinstance(clip, dict):
            raw_idx = clip.get("index")
        try:
            if raw_idx is not None:
                index_to_pos[int(raw_idx)] = pos
        except (TypeError, ValueError):
            continue
    for ov in overlays or []:
        try:
            si = int(ov.get("scene_index"))
        except (TypeError, ValueError, AttributeError):
            continue
        pos = index_to_pos.get(si)
        if pos is None:
            continue
        style = str(ov.get("style") or "").lower()
        # Soft whoosh for map / timeline / pattern cards
        events.append(
            SfxEvent(
                "infographic_whoosh",
                starts[pos],
                assets["infographic_whoosh"],
                float(
                    getattr(s, "compose_sfx_infographic_db", _DEFAULT_DB["infographic_whoosh"])
                ),
            )
        )
        plan.meta.setdefault("infographic_styles", [])
        if isinstance(plan.meta["infographic_styles"], list):
            plan.meta["infographic_styles"].append(style or "card")

    # Soft ambient bed under stills (loop) — ducked via low volume, not colliding with VO
    if bool(getattr(s, "compose_sfx_ambient_bed", True)) and total > 1.0:
        events.append(
            SfxEvent(
                "ambient_bed",
                0.0,
                assets["ambient_bed"],
                float(getattr(s, "compose_sfx_ambient_db", _DEFAULT_DB["ambient_bed"])),
            )
        )

    # Mid-video story beats (Netflix pacing) — soft hits between plates so SFX
    # is felt throughout, not only at cold open.
    beat_iv = float(getattr(s, "compose_sfx_beat_interval_s", 70.0) or 0.0)
    if beat_iv > 0 and total > beat_iv + 5.0:
        occupied = sorted(float(e.at_s) for e in events if e.kind != "ambient_bed")
        beat_db = float(getattr(s, "compose_sfx_beat_db", -20.0))
        t = beat_iv
        while t < total - 4.0:
            if not any(abs(t - o) < 4.0 for o in occupied):
                events.append(
                    SfxEvent(
                        "chapter_hit",
                        t,
                        assets["chapter_hit"],
                        beat_db,
                    )
                )
                occupied.append(t)
                plan.meta.setdefault("story_beats", 0)
                if isinstance(plan.meta.get("story_beats"), int):
                    plan.meta["story_beats"] = int(plan.meta["story_beats"]) + 1
            t += beat_iv

    # Cap density: keep cold + chapter + story beats + up to N infographic whooshes
    max_whoosh = int(getattr(s, "compose_sfx_max_infographic_whooshes", 24) or 24)
    max_whoosh = max(8, max_whoosh)
    whooshes = [e for e in events if e.kind == "infographic_whoosh"]
    other = [e for e in events if e.kind != "infographic_whoosh"]
    if len(whooshes) > max_whoosh:
        # Spread: pick evenly across the timeline
        step = len(whooshes) / float(max_whoosh)
        picked = [whooshes[int(i * step)] for i in range(max_whoosh)]
        whooshes = picked
    plan.events = sorted(other + whooshes, key=lambda e: (e.at_s, e.kind))
    plan.meta["whoosh_cap"] = max_whoosh
    plan.meta["whoosh_count"] = len([e for e in plan.events if e.kind == "infographic_whoosh"])
    return plan


@contextmanager
def _sfx_mix_lock(final: Path) -> Iterator[None]:
    """Exclusive lock so duplicate farms/recovery scripts cannot race on final_sfx_tmp."""
    lock_path = final.with_name(".final_sfx.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o664)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _sfx_mix_timeout_s(duration_s: float) -> float:
    # Long-form napstorian (~26 min) needs headroom under CPU contention; cap at 2h.
    return min(max(900.0, float(duration_s) * 2.5), 7200.0)


def mix_compose_sfx_into_final(
    final_path: Path,
    plan: ComposeSfxPlan,
    *,
    out_path: Path | None = None,
) -> dict[str, Any]:
    """Remux cinematic SFX under existing final audio (video stream copy).

    Returns meta; no-op when plan disabled or no events.
    """
    if not plan.enabled or not plan.events:
        return {"applied": False, "reason": "disabled_or_empty", **plan.to_meta()}

    final = Path(final_path)
    if not final.is_file():
        raise ComposeSfxError(f"final missing: {final}")

    dest = Path(out_path) if out_path else final.with_name("final_sfx_tmp.mp4")
    dur = _probe_duration(final)
    if dur <= 0.5:
        raise ComposeSfxError(f"final duration too short: {dur}")

    timeout_s = _sfx_mix_timeout_s(dur)

    # Split one-shots vs ambient loop
    oneshots = [e for e in plan.events if e.kind != "ambient_bed"]
    ambients = [e for e in plan.events if e.kind == "ambient_bed"]

    inputs: list[str] = ["-i", str(final)]
    filters: list[str] = []
    mix_labels: list[str] = ["[0:a]"]
    inp_i = 1

    for e in oneshots:
        if not e.path.is_file():
            continue
        delay_ms = max(0, int(round(float(e.at_s) * 1000)))
        inputs.extend(["-i", str(e.path)])
        lab = f"s{inp_i}"
        filters.append(
            f"[{inp_i}:a]volume={e.volume_db:.1f}dB,"
            f"adelay={delay_ms}|{delay_ms},"
            f"asetpts=N/SR/TB[{lab}]"
        )
        mix_labels.append(f"[{lab}]")
        inp_i += 1

    for e in ambients:
        if not e.path.is_file():
            continue
        inputs.extend(["-stream_loop", "-1", "-i", str(e.path)])
        lab = f"s{inp_i}"
        filters.append(
            f"[{inp_i}:a]atrim=0:{dur:.3f},asetpts=N/SR/TB,"
            f"volume={e.volume_db:.1f}dB[{lab}]"
        )
        mix_labels.append(f"[{lab}]")
        inp_i += 1

    if len(mix_labels) == 1:
        return {"applied": False, "reason": "no_valid_assets", **plan.to_meta()}

    n = len(mix_labels)
    filters.append(
        "".join(mix_labels)
        + f"amix=inputs={n}:duration=first:dropout_transition=0:normalize=0[aout]"
    )
    fc = ";".join(filters)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        *inputs,
        "-filter_complex",
        fc,
        "-map",
        "0:v",
        "-map",
        "[aout]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-t",
        f"{dur:.3f}",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    with _sfx_mix_lock(final):
        dest.unlink(missing_ok=True)
        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            dest.unlink(missing_ok=True)
            raise ComposeSfxError(
                f"sfx mix timed out after {timeout_s:.0f}s (dur={dur:.1f}s)"
            ) from exc
        except subprocess.CalledProcessError as exc:
            dest.unlink(missing_ok=True)
            err = (exc.stderr or exc.stdout or "")[-800:]
            raise ComposeSfxError(f"sfx mix failed: {err}") from exc

    if out_path is None:
        # Atomic-ish replace: write tmp then replace final
        bak = final.with_name("final_pre_sfx.mp4")
        if not bak.exists():
            try:
                final.replace(bak)
            except OSError:
                # copy fallback
                import shutil

                shutil.copy2(final, bak)
                final.unlink(missing_ok=True)
            dest.replace(final)
        else:
            dest.replace(final)
        applied_path = str(final.resolve())
    else:
        applied_path = str(dest.resolve())

    return {
        "applied": True,
        "path": applied_path,
        **plan.to_meta(),
    }


def _clip_duration(clip: Any) -> float:
    try:
        if hasattr(clip, "duration_s"):
            return max(0.05, float(clip.duration_s or 0.05))
        if isinstance(clip, dict):
            return max(0.05, float(clip.get("duration_s") or 0.05))
    except (TypeError, ValueError):
        pass
    return 0.05


def _probe_duration(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    raw = subprocess.check_output(cmd, text=True)
    data = json.loads(raw)
    return float((data.get("format") or {}).get("duration") or 0.0)


def _generate_placeholder(kind: SfxKind, path: Path) -> None:
    """Generate short royalty-free procedural placeholders (ffmpeg lavfi)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "cold_whoosh":
        # Noise band sweep ~0.7s
        src = (
            "anoisesrc=color=pink:amplitude=0.35:duration=0.75,"
            "afade=t=in:st=0:d=0.08,afade=t=out:st=0.45:d=0.3,"
            "highpass=f=400,lowpass=f=6000"
        )
    elif kind == "cold_impact":
        src = (
            "sine=frequency=55:duration=0.55,"
            "afade=t=in:st=0:d=0.01,afade=t=out:st=0.12:d=0.42,"
            "volume=0.9"
        )
    elif kind == "chapter_hit":
        src = (
            "sine=frequency=90:duration=0.35,"
            "afade=t=in:st=0:d=0.005,afade=t=out:st=0.08:d=0.26,"
            "volume=0.7"
        )
    elif kind == "infographic_whoosh":
        src = (
            "anoisesrc=color=pink:amplitude=0.18:duration=0.55,"
            "afade=t=in:st=0:d=0.05,afade=t=out:st=0.28:d=0.26,"
            "highpass=f=600,lowpass=f=5000"
        )
    else:  # ambient_bed
        src = (
            "anoisesrc=color=brown:amplitude=0.04:duration=8.0,"
            "lowpass=f=500,volume=0.5"
        )
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        src,
        "-ar",
        "48000",
        "-ac",
        "2",
        str(path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        logger.warning("placeholder sfx failed %s: %s", path, exc)
        # Tiny silent wav so mixer never crashes
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-t",
                "0.3",
                str(path),
            ],
            check=False,
            capture_output=True,
        )
