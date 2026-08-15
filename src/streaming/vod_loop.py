"""24/7 cheap VOD-loop restream → YouTube Live RTMP.

Per channel (``napstorian``, ``napping_historian``): FFmpeg infinitely loops the
**featured** (playlist-head) own VOD into YouTube Live RTMP. Playlist tails
remain ranked next-most-viewed candidates for SMM soft-swap. CPU-only on the
VPS — **not** part of the RunPod / GPU farm path (separate systemd units,
separate PID/locks, no ``gpu_lock``).

**Production path = VPS vod_loop.** StreamCast / AzanX desktop GUI is an
optional local experiment only — wrong host model for unattended dual-channel
24/7 (device-locked license, Free=1 stream). Do **not** integrate StreamCast
binaries. Retire StreamCast for prod after 48–72h green on VPS (see
``output/ops/VOD_LOOP.md``).

Encode target (YouTube Live H.264 / AAC benchmarks)::

- RTMP(S), H.264, AAC ~128k stereo, CBR-ish, keyframe every 2s (≤4s)
- YT recs: 720p30 ~4 Mbps · 1080p30 ~10 Mbps · 1080p60 ~12 Mbps
- StreamCast docs were thinner at 1080p (6 Mbps); our default aims **~3800k
  @720p30** (3500–4000k band) when CPU/egress allow — raised from the old 2500k

Durability (see ``output/ops/VOD_LOOP.md`` / ``STREAMING_FAILURES.md``):
- Infinite supervise loop with exponential backoff (no 5-retry give-up)
- Silent-stall detection via FFmpeg ``-progress`` file
- systemd ``Restart=always`` primary; cron ``*/10`` healthcheck optional backup
- Adaptive bitrate ladder (``output/ops/stream_bitrate_policy.json``)

Policy notes (ops must follow):
- Stream **own** content only (channel VODs or explicitly owned ambient beds).
- Do **not** present the loop as a fake breaking-news / live-event broadcast.
- Keep titles/descriptions honest (e.g. ambient / archive loop).
- Missing RTMP URL/key → dry-run / skip with clear ops JSON (never crash cron).
- Rotate YouTube stream keys if they were ever read from StreamCast sessions.

Env (placeholders in ``.env.example`` — real keys only in ``.env``)::

  VOD_LOOP_ENABLED=1
  YT_LIVE_RTMP_URL_NAPSTORIAN=rtmp://a.rtmp.youtube.com/live2
  YT_LIVE_RTMP_KEY_NAPSTORIAN=
  YT_LIVE_RTMP_URL_NAPPING_HISTORIAN=rtmp://a.rtmp.youtube.com/live2
  YT_LIVE_RTMP_KEY_NAPPING_HISTORIAN=
  # Encode knobs (YouTube ~720p30 / 3500–4000k)
  # VOD_LOOP_BITRATE_K=3800   # or VOD_LOOP_BITRATE=3800 / 3800k
  # VOD_LOOP_ADAPTIVE=1
  # VOD_LOOP_ALLOW_1080=0
  # VOD_LOOP_HEIGHT=720
  # VOD_LOOP_FPS=30
  # VOD_LOOP_AUDIO_BITRATE=128k
  # VOD_LOOP_PRESET=veryfast
  # VOD_LOOP_STALL_SEC=180
  # VOD_LOOP_BACKOFF_BASE_SEC=5
  # VOD_LOOP_BACKOFF_MAX_SEC=300
  # VOD_LOOP_INPUT_MODE=featured   # featured=infinite -stream_loop -1 on playlist head
  #                                # concat=legacy concat demuxer (breaks on mixed A/V params)
  # Optional playlist overrides (ffmpeg concat demuxer format)
  # VOD_LOOP_PLAYLIST_NAPSTORIAN=config/streaming/playlist_napstorian.txt
  # VOD_LOOP_PLAYLIST_NAPPING_HISTORIAN=config/streaming/playlist_napping_historian.txt

Status / heartbeat / PID / logs live under ``output/ops/``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "output" / "ops"
STREAM_CFG = ROOT / "config" / "streaming"
STATUS_PATH = OPS / "vod_loop_status.json"
HEALTH_PATH = OPS / "vod_loop_health.json"
HEARTBEAT_PATH = OPS / "vod_loop_heartbeat.json"
STATUS_JSONL_PATH = OPS / "vod_loop_status.jsonl"
BITRATE_POLICY_PATH = OPS / "stream_bitrate_policy.json"
CRONTAB_HINT_PATH = OPS / "crontab_vod_loop.txt"
SYSTEMD_UNIT_EXAMPLE = STREAM_CFG / "vod-loop@.service.example"
SYSTEMD_UNIT_GENERATED = OPS / "vod-loop@.service"

CHANNELS: tuple[str, ...] = ("napstorian", "napping_historian")

# Defaults: YouTube Live H.264 ~4 Mbps @720p30 band (raised from legacy 2500k).
# StreamCast docs used ~1500–3000k locally — too thin for reliable YT health.
_DEFAULT_BITRATE_K = 3800
_DEFAULT_HEIGHT = 720
_DEFAULT_FPS = 30
_DEFAULT_AUDIO_BITRATE = "128k"
_DEFAULT_STALL_SEC = 180
_DEFAULT_BACKOFF_BASE = 5.0
_DEFAULT_BACKOFF_MAX = 300.0
_DEFAULT_PRESET = "veryfast"

# Industrial 720p30 ladder (kbps). Low rungs exist so 4-core VPS can stay realtime
# when farm xfade/compose competes; cap at 4500 unless 1080p explicitly allowed.
_DEFAULT_LADDER_K: tuple[int, ...] = (1500, 2000, 2500, 3500)
_DEFAULT_HEALTHY_STREAK = 3
_DEFAULT_CPU_LOAD_PER_CORE = 0.7
_DEFAULT_STALL_WINDOW_SEC = 1800
_FARM_YIELD_STATE_PATH = OPS / "vod_loop_farm_yield.json"
_LIVE_SPEED_MIN = 0.98
# Resume farm only after Live holds realtime this long (anti-thrash vs stream_beat).
_FARM_RESUME_HOLD_SEC = 180.0
_LIVE_SPEED_RESUME = 1.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_truthy(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def next_backoff_sec(
    current: float,
    *,
    base: float = _DEFAULT_BACKOFF_BASE,
    cap: float = _DEFAULT_BACKOFF_MAX,
    factor: float = 2.0,
) -> float:
    """Exponential backoff step: current*factor, clamped to [base, cap].

    Pure helper for unit tests / supervise loop. Never gives up — callers keep
    retrying forever; only the sleep duration grows (capped).
    """
    base_f = max(1.0, float(base))
    cap_f = max(base_f, float(cap))
    cur = max(base_f, float(current))
    nxt = cur * max(1.0, float(factor))
    return min(cap_f, max(base_f, nxt))


def parse_bitrate_k(raw: str | None, *, default: int = _DEFAULT_BITRATE_K) -> int:
    """Parse ``3800``, ``3800k``, ``3.8M`` style tokens → integer kbps (≥500)."""
    if raw is None:
        return max(500, int(default))
    s = str(raw).strip().lower().replace(" ", "")
    if not s:
        return max(500, int(default))
    mult = 1.0
    if s.endswith("mbit/s") or s.endswith("mbps"):
        mult = 1000.0
        s = s[: -len("mbit/s")] if s.endswith("mbit/s") else s[: -len("mbps")]
    elif s.endswith("kbit/s") or s.endswith("kbps"):
        s = s[: -len("kbit/s")] if s.endswith("kbit/s") else s[: -len("kbps")]
    elif s.endswith("m"):
        mult = 1000.0
        s = s[:-1]
    elif s.endswith("k"):
        s = s[:-1]
    try:
        val = float(s) * mult
    except ValueError:
        return max(500, int(default))
    return max(500, int(round(val)))


def _env_bitrate_k_override() -> int | None:
    """Explicit bitrate override from env.

    Accepts ``VOD_LOOP_BITRATE_K`` (preferred) or alias ``VOD_LOOP_BITRATE``.
    When set, adaptive ladder pick is frozen for encode.
    """
    raw = (os.getenv("VOD_LOOP_BITRATE_K") or os.getenv("VOD_LOOP_BITRATE") or "").strip()
    if not raw:
        return None
    return parse_bitrate_k(raw, default=_DEFAULT_BITRATE_K)


def stall_from_activity(
    *,
    activity_epoch: float | None,
    now: float | None = None,
    stall_sec: float = _DEFAULT_STALL_SEC,
    session_started_epoch: float | None = None,
) -> tuple[bool, str]:
    """Pure stall decision from progress activity timestamps.

    - No activity yet: stall only after ``stall_sec`` since session start.
    - Activity present: stall when ``now - activity > stall_sec``.
    """
    limit = max(30.0, float(stall_sec))
    t = float(now if now is not None else time.time())
    if activity_epoch is None:
        if session_started_epoch is not None:
            age = t - float(session_started_epoch)
            if age > limit:
                return True, f"no_progress_after_{int(age)}s"
        return False, "awaiting_first_progress"
    age = t - float(activity_epoch)
    if age > limit:
        return True, f"stale_{int(age)}s"
    return False, f"fresh_{int(age)}s"


def _load_dotenv() -> None:
    """Best-effort load of repo ``.env`` (cron often has a bare environment)."""
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
    except Exception:
        # Minimal parser fallback — never print values.
        try:
            for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                key, _, val = s.partition("=")
                key = key.strip()
                if not key or key in os.environ:
                    continue
                val = val.strip().strip("'").strip('"')
                os.environ[key] = val
        except Exception:
            pass


def _channel_env_suffix(channel: str) -> str:
    return channel.strip().upper().replace("-", "_")


def _pid_path(channel: str) -> Path:
    return OPS / f"vod_loop_{channel}.pid"


def _supervisor_pid_path(channel: str) -> Path:
    return OPS / f"vod_loop_{channel}.supervisor.pid"


def _log_path(channel: str) -> Path:
    return OPS / f"vod_loop_{channel}.log"


def _progress_path(channel: str) -> Path:
    return OPS / f"vod_loop_{channel}.progress"


def _playlist_path(channel: str) -> Path:
    override = (os.getenv(f"VOD_LOOP_PLAYLIST_{_channel_env_suffix(channel)}") or "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else ROOT / p
    return STREAM_CFG / f"playlist_{channel}.txt"


def _playlist_media_paths(channel: str) -> list[Path]:
    """Absolute media paths from the channel playlist (file '…' lines only)."""
    pl = _playlist_path(channel)
    if not pl.is_file():
        return []
    out: list[Path] = []
    try:
        lines = pl.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#") or not s.lower().startswith("file "):
            continue
        raw = s[5:].strip().strip("'").strip('"')
        p = Path(raw)
        if not p.is_absolute():
            p = (pl.parent / p).resolve()
        else:
            p = p.resolve()
        if p.is_file():
            out.append(p)
    return out


def _playlist_featured_media(channel: str) -> Path | None:
    """Playlist head — the VOD FFmpeg infinitely loops in ``featured`` input mode."""
    paths = _playlist_media_paths(channel)
    return paths[0] if paths else None


def input_mode() -> str:
    """``featured`` (default infinite single-file loop) or legacy ``concat`` demuxer."""
    raw = (os.getenv("VOD_LOOP_INPUT_MODE") or "featured").strip().lower()
    if raw in {"concat", "playlist", "demuxer", "multi"}:
        return "concat"
    return "featured"


def _split_live2_ingest(raw: str) -> tuple[str, str] | None:
    """Split ``rtmp(s)://host/live2/STREAMKEY`` → (base_url, key). Never logs raw."""
    s = (raw or "").strip()
    if not s:
        return None
    low = s.lower()
    if not (low.startswith("rtmp://") or low.startswith("rtmps://")):
        return None
    path = s.rstrip("/").split("?")[0]
    marker = "/live2/"
    idx = path.lower().find(marker)
    if idx < 0:
        if path.lower().endswith("/live2"):
            return path, ""
        return None
    base = path[: idx + len("/live2")]
    key = path[idx + len(marker) :]
    if not key or key.lower() == "live2":
        return base, ""
    # Refuse nested rtmp URLs as "keys" (mis-copied URL into KEY env).
    if "://" in key:
        return base, ""
    return base, key


def _rtmp_url_and_key(channel: str) -> tuple[str, str]:
    suf = _channel_env_suffix(channel)
    url = (os.getenv(f"YT_LIVE_RTMP_URL_{suf}") or "").strip()
    key = (os.getenv(f"YT_LIVE_RTMP_KEY_{suf}") or "").strip()

    # KEY mistakenly set to a full ingest URL (or URL-only) — normalize.
    if key.lower().startswith("rtmp://") or key.lower().startswith("rtmps://"):
        split_key = _split_live2_ingest(key)
        if split_key is not None:
            k_base, k_key = split_key
            if not url:
                url = k_base
            key = k_key

    # Allow a combined full URL in URL var (key empty).
    if url and not key:
        split_url = _split_live2_ingest(url)
        if split_url is not None:
            u_base, u_key = split_url
            if u_key:
                return u_base, u_key
            return u_base, ""
        if "/live2/" in url.rstrip("/").split("?")[0]:
            parts = url.rstrip("/").rsplit("/", 1)
            if len(parts) == 2 and parts[1] and parts[1] != "live2" and "://" not in parts[1]:
                return parts[0], parts[1]
    # Strip accidental nested URL if KEY still looks like a host path.
    if key and ("://" in key or key.lower().startswith("a.rtmp.") or key.lower().startswith("b.rtmp.")):
        key = ""
    return url, key


def _rtmp_destination(channel: str) -> str | None:
    url, key = _rtmp_url_and_key(channel)
    if not url:
        return None
    # Prefer primary ingest host from URL; backup is b.rtmp.youtube.com (same key)
    # but NEVER push two encodes to primary — see ensure_single_ingest / VOD_LOOP.md.
    if key:
        if "://" in key:
            return None
        return f"{url.rstrip('/')}/{key}"
    # URL alone only if it already embeds a key path beyond /live2
    if url.rstrip("/").endswith("/live2"):
        return None
    split = _split_live2_ingest(url)
    if split is not None:
        base, embedded = split
        if embedded:
            return f"{base.rstrip('/')}/{embedded}"
        return None
    return url


def _ffmpeg_bin() -> str:
    return (os.getenv("VOD_LOOP_FFMPEG") or "ffmpeg").strip() or "ffmpeg"


def _default_bitrate_policy() -> dict[str, Any]:
    channels: dict[str, Any] = {}
    default_rung = 2
    ladder = list(_DEFAULT_LADDER_K)
    bitrate_k = ladder[min(default_rung, len(ladder) - 1)]
    for ch in CHANNELS:
        channels[ch] = {
            "rung": default_rung,
            "bitrate_k": bitrate_k,
            "height": _DEFAULT_HEIGHT,
            "healthy_streak": 0,
            "last_action": "init",
            "updated_ts": None,
        }
    return {
        "version": 1,
        "enabled": True,
        "notes": (
            "Industrial YouTube Live ladder for VOD-loop. Never store RTMP keys here. "
            "Cron */10 healthcheck steps rung after healthy_streak_needed consecutive healthy checks."
        ),
        "height": _DEFAULT_HEIGHT,
        "fps": _DEFAULT_FPS,
        "ladder_k": ladder,
        "cap_k": ladder[-1],
        "default_rung": default_rung,
        "healthy_streak_needed": _DEFAULT_HEALTHY_STREAK,
        "cpu_load_per_core_max": _DEFAULT_CPU_LOAD_PER_CORE,
        "stall_recent_window_sec": _DEFAULT_STALL_WINDOW_SEC,
        "max_recent_stall_restarts": 0,
        "bitrate_tolerance_ratio": 0.55,
        "allow_1080p": False,
        "ladder_1080_k": [4500, 6000],
        "1080_cpu_load_per_core_max": 0.45,
        "1080_min_cores": 6,
        "step_down_on_unhealthy": True,
        "restart_on_step": True,
        "channels": channels,
    }


def load_bitrate_policy(*, ensure: bool = True) -> dict[str, Any]:
    """Load adaptive ladder + per-channel rung state (no secrets)."""
    _ensure_dirs()
    if not BITRATE_POLICY_PATH.is_file():
        policy = _default_bitrate_policy()
        if ensure:
            _atomic_write_json(BITRATE_POLICY_PATH, policy)
        return policy
    try:
        data = json.loads(BITRATE_POLICY_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("policy_not_object")
    except Exception:
        policy = _default_bitrate_policy()
        if ensure:
            _atomic_write_json(BITRATE_POLICY_PATH, policy)
        return policy
    base = _default_bitrate_policy()
    base.update({k: v for k, v in data.items() if k != "channels"})
    channels_in = data.get("channels") if isinstance(data.get("channels"), dict) else {}
    merged_channels: dict[str, Any] = {}
    for ch in CHANNELS:
        row = dict(base["channels"][ch])
        src = channels_in.get(ch) if isinstance(channels_in.get(ch), dict) else {}
        row.update({k: v for k, v in src.items() if "key" not in k.lower() and "rtmp" not in k.lower()})
        merged_channels[ch] = row
    base["channels"] = merged_channels
    ladder = base.get("ladder_k") or list(_DEFAULT_LADDER_K)
    try:
        ladder = [max(500, int(x)) for x in ladder]
    except Exception:
        ladder = list(_DEFAULT_LADDER_K)
    # 4-core hosts historically stuck at min=2500 with nowhere to step down under
    # farm CPU contention — ensure low rungs exist without wiping custom ladders.
    if _cpu_nproc() <= 4 and ladder and min(ladder) >= 2500:
        for low in (1500, 2000):
            if low not in ladder:
                ladder.insert(0, low)
        ladder = sorted(set(ladder))
    cap = max(500, int(base.get("cap_k") or ladder[-1]))
    ladder = [min(v, cap) for v in ladder]
    if not ladder:
        ladder = list(_DEFAULT_LADDER_K)
    base["ladder_k"] = ladder
    base["cap_k"] = cap
    return base


def save_bitrate_policy(policy: dict[str, Any]) -> Path:
    """Persist adaptive policy (never write RTMP material)."""
    safe = dict(policy)
    safe.pop("rtmp", None)
    chans = safe.get("channels")
    if isinstance(chans, dict):
        cleaned: dict[str, Any] = {}
        for ch, row in chans.items():
            if not isinstance(row, dict):
                continue
            cleaned[ch] = {
                k: v
                for k, v in row.items()
                if "key" not in k.lower() and "rtmp" not in k.lower() and "dest" not in k.lower()
            }
        safe["channels"] = cleaned
    safe["updated_ts"] = _utc_now()
    _atomic_write_json(BITRATE_POLICY_PATH, safe)
    return BITRATE_POLICY_PATH


def _cpu_nproc() -> int:
    try:
        return max(1, int(os.cpu_count() or 1))
    except Exception:
        return 1


def _loadavg_1() -> float | None:
    try:
        if hasattr(os, "getloadavg"):
            return float(os.getloadavg()[0])
    except Exception:
        pass
    try:
        raw = Path("/proc/loadavg").read_text(encoding="utf-8").split()[0]
        return float(raw)
    except Exception:
        return None


def cpu_health(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """CPU healthy when load1 < cpu_load_per_core_max * cores."""
    pol = policy or load_bitrate_policy(ensure=False)
    cores = _cpu_nproc()
    load1 = _loadavg_1()
    ratio = float(pol.get("cpu_load_per_core_max") or _DEFAULT_CPU_LOAD_PER_CORE)
    limit = ratio * cores
    ok = load1 is not None and load1 < limit
    return {
        "ok": ok,
        "cores": cores,
        "load1": load1,
        "limit": round(limit, 3),
        "ratio": ratio,
        "detail": (
            "unknown_load"
            if load1 is None
            else ("healthy" if ok else f"load1={load1:.2f}>={limit:.2f}")
        ),
    }


def _parse_bitrate_token(raw: Any) -> float | None:
    """Parse ffmpeg progress bitrate like '3850.2kbits/s' → kbps float."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s or s in {"n/a", "nan"}:
        return None
    num = ""
    for ch in s:
        if ch.isdigit() or ch == ".":
            num += ch
        elif num:
            break
    if not num:
        return None
    try:
        val = float(num)
    except ValueError:
        return None
    if "mbit" in s:
        return val * 1000.0
    return val


def _recent_stall_count(channel: str, window_sec: int) -> int:
    health = _channel_health(channel)
    stalls = int(health.get("stall_restarts") or 0)
    if stalls <= 0:
        return 0
    ts = health.get("last_restart_ts")
    reason = str(health.get("last_restart_reason") or "")
    if "stall" not in reason.lower():
        # Stalls historically counted but last restart wasn't stall — treat as not recent.
        return 0
    if not isinstance(ts, str) or not ts:
        return stalls
    try:
        epoch = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return stalls
    if (time.time() - epoch) <= max(60, int(window_sec)):
        return max(1, stalls)
    return 0


def stream_channel_healthy(
    channel: str,
    *,
    healthcheck_action: str | None = None,
    policy: dict[str, Any] | None = None,
    target_bitrate_k: int | None = None,
) -> dict[str, Any]:
    """Stream healthy: progress advancing, no recent stall_restarts, optional bitrate near target."""
    pol = policy or load_bitrate_policy(ensure=False)
    st = channel_status(channel)
    window = int(pol.get("stall_recent_window_sec") or _DEFAULT_STALL_WINDOW_SEC)
    max_stalls = int(pol.get("max_recent_stall_restarts") or 0)
    recent_stalls = _recent_stall_count(channel, window)
    stalled, stall_detail = _is_stalled(channel)
    action = (healthcheck_action or "").strip() or ("healthy" if st.alive and not stalled else "unknown")
    progress_ok = bool(st.alive) and (not stalled) and action in {
        "healthy",
        "already_running",
        "supervise_waiting",
    }
    # supervise_waiting alone is not enough for upward adaptive — need alive ffmpeg.
    if action == "supervise_waiting" and not st.alive:
        progress_ok = False
    bitrate_ok = True
    observed = None
    if target_bitrate_k and st.alive:
        health = _channel_health(channel)
        observed = _parse_bitrate_token(health.get("last_bitrate"))
        if observed is not None:
            tol = float(pol.get("bitrate_tolerance_ratio") or 0.55)
            bitrate_ok = observed >= (float(target_bitrate_k) * tol)
    ok = (
        bool(st.enabled)
        and bool(st.rtmp_ready)
        and progress_ok
        and recent_stalls <= max_stalls
        and bitrate_ok
    )
    reasons: list[str] = []
    if not st.enabled:
        reasons.append("disabled")
    if not st.rtmp_ready:
        reasons.append("missing_rtmp")
    if not progress_ok:
        reasons.append(f"progress:{action}:{stall_detail}")
    if recent_stalls > max_stalls:
        reasons.append(f"recent_stalls={recent_stalls}")
    if not bitrate_ok:
        reasons.append(f"bitrate_low={observed}")
    return {
        "ok": ok,
        "channel": channel,
        "alive": st.alive,
        "action": action,
        "recent_stalls": recent_stalls,
        "observed_bitrate_k": observed,
        "target_bitrate_k": target_bitrate_k,
        "detail": "healthy" if ok else ",".join(reasons) or "unhealthy",
    }


def encode_settings(channel: str | None = None) -> dict[str, Any]:
    """Resolved encode knobs (safe for status JSON — no secrets).

    When adaptive policy is enabled and ``channel`` is set, uses that channel's
    persisted rung bitrate/height from ``stream_bitrate_policy.json``. Explicit
    ``VOD_LOOP_BITRATE_K`` / ``VOD_LOOP_BITRATE`` still wins as an ops override.
    """
    policy = load_bitrate_policy(ensure=True)
    adaptive_on = bool(policy.get("enabled", True)) and _env_truthy("VOD_LOOP_ADAPTIVE", "1")
    override = _env_bitrate_k_override()
    bitrate_k = override if override is not None else _DEFAULT_BITRATE_K
    height = max(360, _env_int("VOD_LOOP_HEIGHT", _DEFAULT_HEIGHT))
    source = "env" if override is not None else "default"
    if adaptive_on and channel and override is None:
        ch = (policy.get("channels") or {}).get(channel) or {}
        try:
            bitrate_k = max(500, int(ch.get("bitrate_k") or bitrate_k))
        except Exception:
            pass
        try:
            height = max(360, int(ch.get("height") or policy.get("height") or height))
        except Exception:
            pass
        source = "adaptive_policy"
    elif adaptive_on and override is None and not channel:
        # Aggregate status: show policy default / max active rung.
        try:
            rungs = [
                int((policy.get("channels") or {}).get(ch, {}).get("bitrate_k") or _DEFAULT_BITRATE_K)
                for ch in CHANNELS
            ]
            bitrate_k = max(rungs) if rungs else bitrate_k
            source = "adaptive_policy"
        except Exception:
            pass
    bitrate_k = max(500, int(bitrate_k))
    fps = max(15, _env_int("VOD_LOOP_FPS", int(policy.get("fps") or _DEFAULT_FPS)))
    audio = (os.getenv("VOD_LOOP_AUDIO_BITRATE") or _DEFAULT_AUDIO_BITRATE).strip() or _DEFAULT_AUDIO_BITRATE
    preset = (os.getenv("VOD_LOOP_PRESET") or _DEFAULT_PRESET).strip() or _DEFAULT_PRESET
    # Under CPU pressure, force a lighter preset so dual Live stays realtime on 4 cores.
    cpu = cpu_health(policy)
    if not cpu.get("ok") and not (os.getenv("VOD_LOOP_PRESET") or "").strip():
        preset = "ultrafast"
    gop = fps * 2  # keyframe every 2s (YouTube ≤4s; we target 2s)
    bufsize_k = bitrate_k * 2
    threads = max(1, _env_int("VOD_LOOP_THREADS", 2 if _cpu_nproc() <= 4 else 0))
    return {
        "bitrate_k": bitrate_k,
        "bitrate": f"{bitrate_k}k",
        "maxrate": f"{bitrate_k}k",
        "bufsize": f"{bufsize_k}k",
        "height": height,
        "fps": fps,
        "gop": gop,
        "audio_bitrate": audio,
        "preset": preset,
        "threads": threads,
        "source": source,
        "adaptive": adaptive_on,
        "stall_sec": max(30, _env_int("VOD_LOOP_STALL_SEC", _DEFAULT_STALL_SEC)),
        "backoff_base_sec": max(1.0, _env_float("VOD_LOOP_BACKOFF_BASE_SEC", _DEFAULT_BACKOFF_BASE)),
        "backoff_max_sec": max(5.0, _env_float("VOD_LOOP_BACKOFF_MAX_SEC", _DEFAULT_BACKOFF_MAX)),
        "benchmarks": {
            "yt_720p30_kbps": 4000,
            "yt_1080p30_kbps": 10000,
            "yt_1080p60_kbps": 12000,
            "streamcast_720p_kbps": 3000,
            "our_default_kbps": _DEFAULT_BITRATE_K,
            "keyframe_sec": 2,
            "audio": "aac_128k_stereo",
        },
    }


def _ensure_dirs() -> None:
    OPS.mkdir(parents=True, exist_ok=True)
    STREAM_CFG.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _append_status_jsonl(row: dict[str, Any]) -> None:
    """Append one redacted metrics line (best-effort; never raises)."""
    try:
        _ensure_dirs()
        safe = {
            k: v
            for k, v in row.items()
            if "key" not in k.lower() and "rtmp" not in k.lower() and "dest" not in k.lower()
        }
        with STATUS_JSONL_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(safe, sort_keys=True, default=str) + "\n")
    except Exception:
        pass


def write_ops_heartbeat(
    *,
    source: str = "vod_loop",
    action: str | None = None,
    channels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write AGENT_BUS-friendly heartbeat snapshot (no secrets, no gpu_lock).

    Path: ``output/ops/vod_loop_heartbeat.json``. Safe for farm agents to read;
    never touches RunPod / farm locks.
    """
    _load_dotenv()
    _ensure_dirs()
    rows: dict[str, Any] = {}
    for ch in CHANNELS:
        st = channel_status(ch)
        health = _channel_health(ch)
        enc = encode_settings(ch)
        stalled, stall_detail = _is_stalled(ch) if st.alive else (False, "not_alive")
        rows[ch] = {
            "alive": st.alive,
            "supervisor_alive": st.supervisor_alive,
            "rtmp_ready": st.rtmp_ready,
            "playlist_ok": st.playlist_ok,
            "bitrate": enc.get("bitrate"),
            "height": enc.get("height"),
            "fps": enc.get("fps"),
            "restarts": health.get("restarts"),
            "stall_restarts": health.get("stall_restarts"),
            "last_progress_ts": health.get("last_progress_ts"),
            "last_bitrate": health.get("last_bitrate"),
            "last_restart_reason": health.get("last_restart_reason"),
            "stalled": stalled,
            "stall_detail": stall_detail,
        }
    if channels:
        # Overlay caller actions (e.g. healthcheck) without secrets.
        for ch, row in channels.items():
            if ch not in rows or not isinstance(row, dict):
                continue
            rows[ch]["action"] = row.get("action")
            if row.get("message") and "rtmp" not in str(row.get("message")).lower():
                rows[ch]["message"] = row.get("message")
    any_alive = any(bool(v.get("alive") or v.get("supervisor_alive")) for v in rows.values())
    any_ready = any(bool(v.get("rtmp_ready")) for v in rows.values())
    payload = {
        "ok": True,
        "ts": _utc_now(),
        "module": "vod_loop",
        "source": source,
        "action": action or "heartbeat",
        "enabled": _env_truthy("VOD_LOOP_ENABLED", "1"),
        "any_alive": any_alive,
        "any_rtmp_ready": any_ready,
        "separate_from_gpu_farm": True,
        "gpu_lock": False,
        "channels": rows,
        "status_path": str(STATUS_PATH),
        "health_path": str(HEALTH_PATH),
        "note": "CPU VOD-loop only; StreamCast not production. Never stores RTMP keys.",
    }
    _atomic_write_json(HEARTBEAT_PATH, payload)
    _append_status_jsonl(
        {
            "ts": payload["ts"],
            "source": source,
            "action": payload["action"],
            "any_alive": any_alive,
            "channels": {
                ch: {
                    "alive": rows[ch].get("alive"),
                    "restarts": rows[ch].get("restarts"),
                    "stall_restarts": rows[ch].get("stall_restarts"),
                    "bitrate": rows[ch].get("bitrate"),
                    "action": rows[ch].get("action"),
                }
                for ch in CHANNELS
            },
        }
    )
    return payload


def _read_health() -> dict[str, Any]:
    if not HEALTH_PATH.is_file():
        return {"channels": {}}
    try:
        data = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"channels": {}}
        data.setdefault("channels", {})
        return data
    except Exception:
        return {"channels": {}}


def _channel_health(channel: str) -> dict[str, Any]:
    health = _read_health()
    ch = health.get("channels", {}).get(channel) or {}
    if not isinstance(ch, dict):
        ch = {}
    return {
        "restarts": int(ch.get("restarts") or 0),
        "stall_restarts": int(ch.get("stall_restarts") or 0),
        "last_progress_ts": ch.get("last_progress_ts"),
        "last_frame": ch.get("last_frame"),
        "last_bitrate": ch.get("last_bitrate"),
        "last_out_time_ms": ch.get("last_out_time_ms"),
        "last_exit_code": ch.get("last_exit_code"),
        "last_restart_reason": ch.get("last_restart_reason"),
        "last_restart_ts": ch.get("last_restart_ts"),
        "backoff_sec": ch.get("backoff_sec"),
        "supervised": bool(ch.get("supervised")),
        "session_started_ts": ch.get("session_started_ts"),
    }


def _update_channel_health(channel: str, **fields: Any) -> dict[str, Any]:
    health = _read_health()
    channels = health.setdefault("channels", {})
    cur = dict(channels.get(channel) or {})
    cur.update({k: v for k, v in fields.items() if v is not None})
    cur["updated_ts"] = _utc_now()
    channels[channel] = cur
    health["ts"] = _utc_now()
    health["module"] = "vod_loop"
    _atomic_write_json(HEALTH_PATH, health)
    return cur


def _read_pid_file(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw.splitlines()[0].strip())
    except Exception:
        return None


def _read_pid(channel: str) -> int | None:
    return _read_pid_file(_pid_path(channel))


def _read_supervisor_pid(channel: str) -> int | None:
    return _read_pid_file(_supervisor_pid_path(channel))


def _pid_alive(pid: int | None, *, expect_substr: str | None = None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    if expect_substr:
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "ignore")
            if expect_substr.lower() not in cmdline.lower():
                return False
        except Exception:
            pass
    return True


def _clear_pid(channel: str) -> None:
    path = _pid_path(channel)
    if path.is_file():
        try:
            path.unlink()
        except Exception:
            pass


def _clear_supervisor_pid(channel: str) -> None:
    path = _supervisor_pid_path(channel)
    if path.is_file():
        try:
            path.unlink()
        except Exception:
            pass


def _kill_pid(pid: int, *, grace_checks: int = 20) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        return
    for _ in range(grace_checks):
        if not _pid_alive(pid):
            return
        time.sleep(0.25)
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def _cmdline_of(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "ignore")
    except Exception:
        return ""


def _ppid_of(pid: int) -> int | None:
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    for line in status.splitlines():
        if line.startswith("PPid:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _iter_pids_with_substr(*needles: str) -> list[int]:
    """Scan /proc for PIDs whose cmdline contains all needles (case-insensitive)."""
    wanted = [n.lower() for n in needles if n]
    if not wanted:
        return []
    found: list[int] = []
    try:
        entries = list(Path("/proc").iterdir())
    except Exception:
        return []
    for ent in entries:
        name = ent.name
        if not name.isdigit():
            continue
        try:
            pid = int(name)
        except ValueError:
            continue
        cmd = _cmdline_of(pid).lower()
        if not cmd:
            continue
        if all(n in cmd for n in wanted):
            found.append(pid)
    return found


def list_channel_ffmpeg_pids(channel: str) -> list[int]:
    """All live FFmpeg encodes for this channel playlist (may be >1 → dual ingest).

    Requires argv0 basename ``ffmpeg`` so diagnostic shells / pgrep wrappers that
    merely *mention* ffmpeg+playlist in their cmdline are not counted as encodes
    (that false dual_ingest caused spurious culls).

    Also matches the progress file and the current playlist-head media path so
    legacy ``historian_live_supervise.sh`` direct encodes (no concat playlist in
    argv) still count as this channel's ingest.
    """
    pl_file = f"playlist_{channel}.txt"
    prog = str(_progress_path(channel))
    featured = ""
    try:
        feat = _playlist_featured_media(channel)
        if feat is not None:
            featured = str(Path(feat).resolve())
    except Exception:
        featured = ""
    found: list[int] = []
    for pid in _iter_pids_with_substr("ffmpeg"):
        cmd = _cmdline_of(pid)
        if not cmd:
            continue
        argv0 = cmd.split("\0", 1)[0]
        if Path(argv0).name != "ffmpeg":
            continue
        flat = cmd.replace("\0", " ")
        if pl_file in flat or prog in flat or (featured and featured in flat):
            found.append(pid)
    return sorted(set(found))


def list_channel_supervise_pids(channel: str) -> list[int]:
    """All ``vod_loop supervise --channel <channel>`` processes for this channel."""
    found: list[int] = []
    for pid in _iter_pids_with_substr("supervise", channel):
        cmd = _cmdline_of(pid)
        if not cmd:
            continue
        argv0 = cmd.split("\0", 1)[0]
        # Must be a python interpreter (not a shell quoting the supervise cmdline).
        if "python" not in Path(argv0).name:
            continue
        # Require exact --channel <name> (null-separated argv or spaced).
        if f"--channel\0{channel}" not in cmd and f"--channel {channel}" not in cmd.replace(
            "\0", " "
        ):
            continue
        flat = cmd.replace("\0", " ")
        if "vod_loop" not in flat and "src.cli.vod_loop" not in flat:
            continue
        found.append(pid)
    return sorted(set(found))


def list_stray_live_wrapper_pids(channel: str) -> list[int]:
    """Legacy bash Live wrappers that race systemd/python supervise (same RTMP key).

    Historically ``scripts/historian_live_supervise.sh`` was used while
    ``vod-loop@napping_historian`` was masked — two supervisors on one historian
    key caused reconnect storms. Always cull these when systemd/python owns Live.
    """
    if channel != "napping_historian":
        return []
    needles = (
        "historian_live_supervise.sh",
        "historian_live_supervise",
    )
    found: list[int] = []
    for needle in needles:
        for pid in _iter_pids_with_substr(needle):
            cmd = _cmdline_of(pid)
            if not cmd:
                continue
            argv0 = Path(cmd.split("\0", 1)[0]).name
            # bash/sh wrappers or the script itself as argv0
            if argv0 in {"bash", "sh", "dash"} or "historian_live_supervise" in argv0:
                found.append(pid)
    return sorted(set(found))


_SUPERVISE_LOCK_FDS: dict[str, Any] = {}
_OP_LOCK_FDS: dict[str, Any] = {}
_HEALTHCHECK_LOCK_FD: Any | None = None


def _supervise_lock_path(channel: str) -> Path:
    return OPS / f"vod_loop_{channel}.supervise.lock"


def _op_lock_path(channel: str) -> Path:
    return OPS / f"vod_loop_{channel}.op.lock"


def _healthcheck_lock_path() -> Path:
    return OPS / "vod_loop_healthcheck.lock"


def _relive_marker_path(channel: str) -> Path:
    return OPS / f"vod_loop_{channel}_relive.ts"


def _systemctl_run(*args: str, timeout: float = 90) -> subprocess.CompletedProcess[str]:
    """Run systemctl; fall back to ``sudo -n`` for system units when needed."""
    cmd = ["systemctl", *args]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        return subprocess.CompletedProcess(cmd, 1, "", f"{type(exc).__name__}")
    if proc.returncode == 0:
        return proc
    sudo_cmd = ["sudo", "-n", "systemctl", *args]
    try:
        return subprocess.run(
            sudo_cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return proc


def _systemd_main_pid(channel: str) -> int | None:
    try:
        proc = _systemctl_run(
            "show", f"vod-loop@{channel}", "-p", "MainPID", "--value", timeout=5
        )
        main_pid = int((proc.stdout or "").strip() or "0")
        return main_pid if main_pid > 0 else None
    except Exception:
        return None


def _systemd_owns_channel(channel: str) -> bool:
    """True when vod-loop@CHANNEL is enabled, active, or has a MainPID.

    When systemd owns the channel, callers must NEVER spawn a detached
    ``vod_loop supervise`` (that was the dual-primary ingest race).
    """
    if _systemd_unit_active(channel) or _systemd_unit_enabled(channel):
        return True
    return _systemd_main_pid(channel) is not None


def _acquire_flock(path: Path, *, meta: str = "") -> Any | None:
    """Non-blocking exclusive flock; returns open fd on success (caller keeps it)."""
    import fcntl

    _ensure_dirs()
    try:
        fd = open(path, "a+", encoding="utf-8")
    except Exception:
        return None
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            fd.close()
        except Exception:
            pass
        return None
    except Exception:
        try:
            fd.close()
        except Exception:
            pass
        return None
    try:
        fd.seek(0)
        fd.truncate()
        fd.write(f"{os.getpid()}\n{_utc_now()}\n{meta}\n")
        fd.flush()
    except Exception:
        pass
    return fd


def _acquire_supervise_lock(channel: str) -> bool:
    """Non-blocking exclusive lock so only one supervise owns a channel.

    Never unlink the lock path while another process may hold flock on the
    inode — unlink+recreate allows two exclusive locks (classic race).
    """
    if channel in _SUPERVISE_LOCK_FDS:
        return True
    fd = _acquire_flock(_supervise_lock_path(channel), meta="supervise")
    if fd is None:
        return False
    _SUPERVISE_LOCK_FDS[channel] = fd
    return True


def _acquire_channel_op_lock(channel: str) -> bool:
    """Serialize healthcheck/relive/start per channel (stream_beat vs */10 cron)."""
    if channel in _OP_LOCK_FDS:
        return True
    fd = _acquire_flock(_op_lock_path(channel), meta="op")
    if fd is None:
        return False
    _OP_LOCK_FDS[channel] = fd
    return True


def _release_channel_op_lock(channel: str) -> None:
    fd = _OP_LOCK_FDS.pop(channel, None)
    if fd is None:
        return
    try:
        fd.close()
    except Exception:
        pass


def _acquire_healthcheck_lock() -> bool:
    global _HEALTHCHECK_LOCK_FD
    if _HEALTHCHECK_LOCK_FD is not None:
        return True
    fd = _acquire_flock(_healthcheck_lock_path(), meta="healthcheck")
    if fd is None:
        return False
    _HEALTHCHECK_LOCK_FD = fd
    return True


def ensure_single_ingest(
    channel: str,
    *,
    keep_ffmpeg_pid: int | None = None,
    keep_supervise_pid: int | None = None,
    allow_zero_ffmpeg: bool = False,
) -> dict[str, Any]:
    """Kill duplicate FFmpeg / supervise processes for one channel.

    YouTube primary ingest must receive **exactly one** encode. Two pushes to
    ``a.rtmp.youtube.com`` → Studio dual-ingest / poor quality.
    """
    killed_ff: list[int] = []
    killed_sup: list[int] = []
    killed_wrappers: list[int] = []
    keep_ff: int | None = None
    keep_sup: int | None = None
    ff: list[int] = []
    sup: list[int] = []

    # Always cull legacy bash wrappers first — they race the same RTMP key.
    for wpid in list_stray_live_wrapper_pids(channel):
        _kill_pid(wpid)
        killed_wrappers.append(wpid)

    for _round in range(3):
        ff = list_channel_ffmpeg_pids(channel)
        sup = list_channel_supervise_pids(channel)
        # Keep killing wrappers if they respawn mid-cull.
        for wpid in list_stray_live_wrapper_pids(channel):
            _kill_pid(wpid)
            killed_wrappers.append(wpid)

        keep_ff = keep_ffmpeg_pid if keep_ffmpeg_pid in ff else None
        if keep_ff is None and not allow_zero_ffmpeg:
            claimed = _read_pid(channel)
            if claimed in ff:
                keep_ff = claimed
        if keep_ff is None and not allow_zero_ffmpeg:
            cand_sup = keep_supervise_pid or _read_supervisor_pid(channel)
            if cand_sup:
                for pid in ff:
                    if _ppid_of(pid) == cand_sup:
                        keep_ff = pid
                        break
        if keep_ff is None and ff and not allow_zero_ffmpeg:
            # Prefer child of systemd MainPID, else youngest encode.
            main_pid = _systemd_main_pid(channel)
            if main_pid:
                for pid in ff:
                    if _ppid_of(pid) == main_pid:
                        keep_ff = pid
                        break
            if keep_ff is None:
                keep_ff = max(ff)

        keep_sup = keep_supervise_pid
        if keep_sup is None:
            keep_sup = _read_supervisor_pid(channel)
        if keep_sup not in sup:
            keep_sup = None
        if keep_sup is None and keep_ff is not None:
            parent = _ppid_of(keep_ff)
            if parent in sup:
                keep_sup = parent
        if keep_sup is None and sup:
            main_pid = _systemd_main_pid(channel)
            if main_pid in sup:
                keep_sup = main_pid
        if keep_sup is None and sup:
            keep_sup = max(sup)

        round_ff: list[int] = []
        round_sup: list[int] = []
        for pid in ff:
            if keep_ff is not None and pid == keep_ff:
                continue
            _kill_pid(pid)
            round_ff.append(pid)
            killed_ff.append(pid)
        for pid in list(sup):
            if keep_sup is not None and pid == keep_sup:
                continue
            if pid == os.getpid():
                continue
            _kill_pid(pid)
            round_sup.append(pid)
            killed_sup.append(pid)
            # Supervise death must not leave orphan encodes (own session/pgid).
            for fpid in list_channel_ffmpeg_pids(channel):
                if fpid == keep_ff:
                    continue
                if _ppid_of(fpid) == pid or keep_ff is None:
                    _kill_pid(fpid)
                    killed_ff.append(fpid)

        if not round_ff and not round_sup:
            break
        time.sleep(0.35)

    ff = list_channel_ffmpeg_pids(channel)
    sup = list_channel_supervise_pids(channel)
    if keep_ff is not None and keep_ff in ff:
        _pid_path(channel).write_text(str(keep_ff) + "\n", encoding="utf-8")
    elif not ff:
        _clear_pid(channel)
    if keep_sup is not None and keep_sup in sup:
        _supervisor_pid_path(channel).write_text(str(keep_sup) + "\n", encoding="utf-8")

    return {
        "channel": channel,
        "ffmpeg_pids": ff,
        "supervise_pids": sup,
        "kept_ffmpeg": keep_ff if keep_ff in ff else None,
        "kept_supervise": keep_sup if keep_sup in sup else None,
        "killed_ffmpeg": sorted(set(killed_ff)),
        "killed_supervise": sorted(set(killed_sup)),
        "killed_wrappers": sorted(set(killed_wrappers)),
        "dual_ingest": len(ff) > 1,
        "duplicate_supervise": len(sup) > 1 or bool(list_stray_live_wrapper_pids(channel)),
        "ts": _utc_now(),
    }


def _stale_supervise_lock_pid(channel: str) -> int | None:
    path = _supervise_lock_path(channel)
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip().splitlines()
        return int(raw[0].strip())
    except Exception:
        return None


def claim_supervisor_singleton(channel: str) -> dict[str, Any]:
    """Claim exclusive ownership before the supervise loop starts encoding.

    Lock **first**. Never kill a living lock holder (that caused dual-ingest
    flaps when a stray/restarting supervise culled the systemd owner). Never
    unlink the lock file — unlink+recreate breaks flock exclusivity.
    """
    _ensure_dirs()
    me = os.getpid()

    locked = False
    holder: int | None = None
    # Retry only while prior holder is dead (kernel releasing flock on close).
    for _ in range(40):
        locked = _acquire_supervise_lock(channel)
        if locked:
            break
        holder = _stale_supervise_lock_pid(channel)
        if holder and holder != me and _pid_alive(holder):
            return {
                "ok": False,
                "channel": channel,
                "action": "supervise_lock_held",
                "cleaned": None,
                "holder": holder,
                "ts": _utc_now(),
            }
        time.sleep(0.4)

    if not locked:
        return {
            "ok": False,
            "channel": channel,
            "action": "supervise_lock_held",
            "cleaned": None,
            "holder": _stale_supervise_lock_pid(channel),
            "ts": _utc_now(),
        }

    # We own the flock — cull orphan encodes / stray supervises only.
    cleaned = ensure_single_ingest(
        channel,
        keep_ffmpeg_pid=None,
        keep_supervise_pid=me if me in list_channel_supervise_pids(channel) else None,
        allow_zero_ffmpeg=True,
    )
    for pid in list_channel_ffmpeg_pids(channel):
        _kill_pid(pid)
        _clear_pid(channel)
    for pid in list_channel_supervise_pids(channel):
        if pid != me:
            _kill_pid(pid)

    _supervisor_pid_path(channel).write_text(str(me) + "\n", encoding="utf-8")
    return {
        "ok": True,
        "channel": channel,
        "action": "claimed",
        "pid": me,
        "cleaned": cleaned,
        "ts": _utc_now(),
    }


def _relive_cooldown_sec() -> float:
    return max(30.0, _env_float("VOD_LOOP_RELIVE_COOLDOWN_SEC", 120.0))


def _relive_allowed(channel: str) -> tuple[bool, str]:
    path = _relive_marker_path(channel)
    if not path.is_file():
        return True, "no_marker"
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return True, "marker_unreadable"
    cool = _relive_cooldown_sec()
    if age < cool:
        return False, f"cooldown_{int(cool - age)}s"
    return True, f"cooled_{int(age)}s"


def _mark_relive(channel: str, reason: str) -> None:
    _ensure_dirs()
    _relive_marker_path(channel).write_text(
        f"{_utc_now()}\nreason={reason}\n",
        encoding="utf-8",
    )


def append_repair_note(channel: str, *, reason: str, action: str, detail: str = "") -> Path:
    """Append a short ops repair note (no secrets)."""
    _ensure_dirs()
    path = OPS / "vod_loop_repair_notes.md"
    line = (
        f"- `{_utc_now()}` **{channel}** reason=`{reason}` action=`{action}`"
        + (f" detail=`{detail}`" if detail else "")
        + "\n"
    )
    if not path.is_file():
        path.write_text(
            "# VOD-loop repair notes\n\n"
            "_Auto-written by healthcheck / stream watchdog. No RTMP keys._\n\n",
            encoding="utf-8",
        )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
    return path


def _log_tail_ingest_symptoms(channel: str, *, max_lines: int = 120) -> list[str]:
    """Detect dual-ingest / RTMP open failures in **current** encode session log."""
    path = _log_path(channel)
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-max_lines:]
    except Exception:
        return []
    # Only inspect from the latest session header so prior dual-ingest noise is ignored.
    start_idx = 0
    for i, ln in enumerate(lines):
        if ln.startswith("# vod_loop start"):
            start_idx = i
    window = lines[start_idx:]
    joined = "\n".join(window).lower()
    symptoms: list[str] = []
    checks = (
        ("error opening output", "rtmp_open_error"),
        ("more than one ingestion", "dual_ingest_youtube"),
        ("primary ingestion", "dual_ingest_youtube"),
        ("input/output error", "rtmp_io_error"),
        ("connection refused", "rtmp_refused"),
        ("server returned 4", "rtmp_http_4xx"),
    )
    for needle, tag in checks:
        if needle in joined and tag not in symptoms:
            symptoms.append(tag)
    return symptoms


def _bitrate_collapsed(channel: str) -> tuple[bool, str]:
    """True when encode is alive but observed bitrate far below target after warm-up."""
    st = channel_status(channel)
    if not st.alive:
        return False, "not_alive"
    health = _channel_health(channel)
    observed = _parse_bitrate_token(health.get("last_bitrate"))
    if observed is None:
        return False, "no_bitrate"
    enc = encode_settings(channel)
    target = float(enc.get("bitrate_k") or _DEFAULT_BITRATE_K)
    # Warm-up: ignore collapse for first ~45s of session.
    session_ts = health.get("session_started_ts")
    if isinstance(session_ts, str) and session_ts:
        try:
            started = datetime.fromisoformat(session_ts.replace("Z", "+00:00")).timestamp()
            if (time.time() - started) < 45:
                return False, "warming"
        except Exception:
            pass
    floor = max(400.0, target * 0.35)
    if observed < floor:
        return True, f"bitrate_{observed:.0f}_lt_{floor:.0f}"
    return False, f"bitrate_ok_{observed:.0f}"


def _reconnect_storm(channel: str, *, window_sec: int = 600, min_restarts: int = 4) -> tuple[bool, str]:
    """Detect flapping reconnects (recent), not lifetime restart totals."""
    st = channel_status(channel)
    # Healthy encode with fresh progress is not a storm — ignore historical counters.
    if st.alive:
        stalled, _ = _is_stalled(channel)
        if not stalled:
            return False, "alive_stable"
    health = _channel_health(channel)
    stalls = int(health.get("stall_restarts") or 0)
    restarts = int(health.get("restarts") or 0)
    reason = str(health.get("last_restart_reason") or "").lower()
    ts = health.get("last_restart_ts")
    if not isinstance(ts, str) or not ts:
        return False, "no_restart_ts"
    try:
        epoch = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return False, "bad_restart_ts"
    age = time.time() - epoch
    if age > window_sec:
        return False, f"cooled_{int(age)}s"
    # Prefer stall_restarts (session flaps) over lifetime ``restarts``.
    if stalls >= min_restarts:
        return True, f"stall_restarts_{stalls}_age_{int(age)}s"
    failureish = any(
        tok in reason
        for tok in ("ffmpeg_exit", "stall", "rtmp", "start_failed", "relive", "dual")
    )
    if failureish and restarts >= max(8, min_restarts) and age < 180 and not st.alive:
        return True, f"dead_after_restarts_{restarts}"
    return False, f"ok_stalls_{stalls}_restarts_{restarts}"


def performance_issues(channel: str) -> dict[str, Any]:
    """Summarize Live performance problems for watchdog / SMM (no secrets)."""
    st = channel_status(channel)
    ff = list_channel_ffmpeg_pids(channel)
    sup = list_channel_supervise_pids(channel)
    collapsed, collapse_detail = _bitrate_collapsed(channel)
    storm, storm_detail = _reconnect_storm(channel)
    stalled, stall_detail = _is_stalled(channel) if st.alive else (False, "n/a")
    symptoms = _log_tail_ingest_symptoms(channel)
    issues: list[str] = []
    if len(ff) > 1:
        issues.append("dual_ingest")
    if len(sup) > 1 or list_stray_live_wrapper_pids(channel):
        issues.append("duplicate_supervise")
    if st.rtmp_ready and not st.alive and not st.supervisor_alive:
        issues.append("dead_encode")
    if st.rtmp_ready and st.supervisor_alive and not st.alive:
        # Between restarts is OK briefly; flag if also reconnect storm / log errors.
        if storm or symptoms:
            issues.append("supervise_without_encode")
    if collapsed:
        issues.append("bitrate_collapse")
    if storm:
        issues.append("reconnect_storm")
    if stalled and st.alive:
        issues.append("stall")
    for tag in symptoms:
        if tag not in issues:
            issues.append(tag)
    return {
        "channel": channel,
        "issues": issues,
        "ffmpeg_count": len(ff),
        "supervise_count": len(sup) + len(list_stray_live_wrapper_pids(channel)),
        "alive": st.alive,
        "supervisor_alive": st.supervisor_alive,
        "rtmp_ready": st.rtmp_ready,
        "collapse_detail": collapse_detail,
        "storm_detail": storm_detail,
        "stall_detail": stall_detail,
        "log_symptoms": symptoms,
        "needs_repair": bool(issues),
        "ts": _utc_now(),
    }


def relive_channel(
    channel: str,
    *,
    reason: str,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Stop stale encode(s), backoff if needed, restart systemd/supervise (re-live)."""
    _load_dotenv()
    _ensure_dirs()
    allowed, cool_detail = _relive_allowed(channel)
    if not allowed and not force:
        append_repair_note(
            channel,
            reason=reason,
            action="relive_skipped_cooldown",
            detail=cool_detail,
        )
        return {
            "ok": True,
            "channel": channel,
            "action": "relive_skipped_cooldown",
            "reason": reason,
            "detail": cool_detail,
            "ts": _utc_now(),
        }
    if dry_run:
        return {
            "ok": True,
            "channel": channel,
            "action": "relive_dry_run",
            "reason": reason,
            "ts": _utc_now(),
        }

    cleaned = ensure_single_ingest(channel, allow_zero_ffmpeg=True)
    # Always clear encodes before bounce so YouTube primary sees one reconnect.
    for pid in list_channel_ffmpeg_pids(channel):
        _kill_pid(pid)
    _clear_pid(channel)

    method = "supervise"
    restarted = False
    rc: int | None = None
    if _systemd_owns_channel(channel):
        method = "systemd"
        try:
            # restart = stop+start even if inactive; never spawn detached supervise.
            proc = _systemctl_run("restart", f"vod-loop@{channel}", timeout=90)
            rc = proc.returncode
            restarted = proc.returncode == 0
            if not restarted:
                append_repair_note(
                    channel,
                    reason=reason,
                    action="relive_systemd_error",
                    detail=f"rc={rc} {(proc.stderr or '')[:120]}",
                )
                return {
                    "ok": False,
                    "channel": channel,
                    "action": "relive_failed",
                    "method": method,
                    "reason": reason,
                    "error": f"systemctl_rc={rc}",
                    "cleaned": cleaned,
                    "ts": _utc_now(),
                }
        except Exception as exc:
            append_repair_note(
                channel,
                reason=reason,
                action="relive_systemd_error",
                detail=type(exc).__name__,
            )
            return {
                "ok": False,
                "channel": channel,
                "action": "relive_failed",
                "method": method,
                "reason": reason,
                "error": type(exc).__name__,
                "cleaned": cleaned,
                "ts": _utc_now(),
            }
    else:
        stop_channel(channel)
        st = start_channel(channel, dry_run=False, force=True)
        restarted = st.action in {"started", "started_supervise", "already_running", "systemd_started"}
        rc = None

    _mark_relive(channel, reason)
    _update_channel_health(
        channel,
        last_restart_reason=f"relive:{reason}",
        last_restart_ts=_utc_now(),
        restarts=int(_channel_health(channel).get("restarts") or 0) + 1,
    )
    note = append_repair_note(
        channel,
        reason=reason,
        action="relive",
        detail=f"method={method} ok={restarted} dual_was={cleaned.get('dual_ingest')}",
    )
    live_meta = None
    if restarted:
        live_meta = _maybe_sync_live_meta(channel, reason=f"relive:{reason}")
    return {
        "ok": restarted,
        "channel": channel,
        "action": "relive",
        "method": method,
        "reason": reason,
        "restarted": restarted,
        "rc": rc,
        "cleaned": cleaned,
        "note_path": str(note),
        "live_meta": live_meta,
        "ts": _utc_now(),
    }


def _maybe_sync_live_meta(channel: str, *, reason: str) -> dict[str, Any] | None:
    """Best-effort Live title/desc/thumb reuse — never raises into encode path."""
    try:
        from src.streaming.live_broadcast_meta import maybe_sync_after_encode_event

        return maybe_sync_after_encode_event(channel, reason=reason)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "action": "hook_error", "error": str(exc)[:160]}


def nudge_encode_for_playlist_swap(
    channel: str, *, dry_run: bool = False
) -> dict[str, Any]:
    """Safe playlist pickup: dedupe, kill only ffmpeg, let supervise respawn once.

    Does **not** start a second ingest beside an existing supervise/systemd unit.
    """
    _load_dotenv()
    cleaned = ensure_single_ingest(channel)
    st = channel_status(channel)
    if dry_run:
        return {
            "ok": True,
            "channel": channel,
            "action": "would_nudge_ffmpeg",
            "alive": st.alive,
            "supervisor_alive": st.supervisor_alive,
            "cleaned": cleaned,
            "ts": _utc_now(),
        }
    pid = st.pid
    if pid and _pid_alive(pid, expect_substr="ffmpeg"):
        _kill_pid(pid)
        _clear_pid(channel)
        _update_channel_health(
            channel,
            last_restart_reason="playlist_swap_nudge",
            last_restart_ts=_utc_now(),
        )
        append_repair_note(
            channel,
            reason="playlist_swap",
            action="ffmpeg_nudge",
            detail="supervise will respawn; single ingest",
        )
        live_meta = _maybe_sync_live_meta(channel, reason="playlist_swap_nudge")
        return {
            "ok": True,
            "channel": channel,
            "action": "ffmpeg_signaled",
            "cleaned": cleaned,
            "live_meta": live_meta,
            "ts": _utc_now(),
        }
    if st.supervisor_alive:
        return {
            "ok": True,
            "channel": channel,
            "action": "supervise_waiting",
            "cleaned": cleaned,
            "ts": _utc_now(),
        }
    # No supervise — systemd-only when unit owns the channel (never detach).
    if _systemd_owns_channel(channel):
        proc = _systemctl_run("try-restart", f"vod-loop@{channel}", timeout=90)
        if proc.returncode != 0:
            proc = _systemctl_run("restart", f"vod-loop@{channel}", timeout=90)
        live_meta = None
        if proc.returncode == 0:
            live_meta = _maybe_sync_live_meta(channel, reason="playlist_swap_systemd")
        return {
            "ok": proc.returncode == 0,
            "channel": channel,
            "action": "systemd_try_restart" if proc.returncode == 0 else "start_failed",
            "cleaned": cleaned,
            "live_meta": live_meta,
            "ts": _utc_now(),
        }
    started = start_channel(channel, dry_run=False, force=False)
    return {
        "ok": started.action
        in {"started", "started_supervise", "already_running", "systemd_started"},
        "channel": channel,
        "action": started.action,
        "cleaned": cleaned,
        "live_meta": (started.extra or {}).get("live_meta")
        if hasattr(started, "extra")
        else None,
        "ts": _utc_now(),
    }


def _systemd_unit_enabled(channel: str) -> bool:
    try:
        proc = _systemctl_run("is-enabled", f"vod-loop@{channel}", timeout=5)
        return (proc.stdout or "").strip() in {
            "enabled",
            "enabled-runtime",
            "linked",
            "linked-runtime",
            "static",
        }
    except Exception:
        return False


def ensure_default_playlists() -> dict[str, str]:
    """Create concat playlists pointing at a local ambient placeholder if missing."""
    _ensure_dirs()
    placeholder = STREAM_CFG / "ambient_placeholder.mp4"
    created: dict[str, str] = {}
    if not placeholder.is_file():
        # Short silent color bed — own ops asset, not third-party media.
        cmd = [
            _ffmpeg_bin(),
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x1a1a2e:s=1280x720:r=30:d=30",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "30",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(placeholder),
        ]
        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            created["ambient_placeholder"] = "created"
        except Exception as exc:
            created["ambient_placeholder"] = f"failed:{type(exc).__name__}"
    for ch in CHANNELS:
        pl = _playlist_path(ch)
        if pl.is_file():
            created[ch] = "exists"
            continue
        # Prefer curated own VOD paths when ops adds them; default = placeholder.
        target = placeholder if placeholder.is_file() else None
        smoke = ROOT / "output" / "video" / "smoke_ann_bolyn_edit" / "final.mp4"
        if target is None and smoke.is_file():
            target = smoke
        if target is None:
            created[ch] = "skipped_no_media"
            continue
        # concat demuxer lines; paths relative to playlist file dir work poorly —
        # use absolute paths.
        abs_media = target.resolve()
        pl.write_text(f"file '{abs_media.as_posix()}'\n", encoding="utf-8")
        created[ch] = f"created→{abs_media.name}"
    return created


def _playlist_ok(channel: str) -> tuple[bool, str]:
    pl = _playlist_path(channel)
    if not pl.is_file():
        return False, f"missing_playlist:{pl}"
    lines = [
        ln.strip()
        for ln in pl.read_text(encoding="utf-8", errors="ignore").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if not lines:
        return False, "empty_playlist"
    # Validate at least one file= entry resolves.
    found = 0
    for ln in lines:
        if not ln.lower().startswith("file "):
            continue
        raw = ln[5:].strip().strip("'").strip('"')
        p = Path(raw)
        if not p.is_absolute():
            p = (pl.parent / p).resolve()
        if p.is_file():
            found += 1
    if found == 0:
        return False, "playlist_files_missing"
    return True, f"entries={found}"


@dataclass
class ChannelStatus:
    channel: str
    enabled: bool
    has_rtmp_url: bool
    has_rtmp_key: bool
    rtmp_ready: bool
    playlist: str
    playlist_ok: bool
    playlist_detail: str
    pid: int | None = None
    alive: bool = False
    supervisor_pid: int | None = None
    supervisor_alive: bool = False
    action: str = "none"
    message: str = ""
    log_path: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Never expose RTMP URL/key material.
        d.pop("extra", None)
        if self.extra:
            safe = {
                k: v
                for k, v in self.extra.items()
                if "key" not in k.lower() and "rtmp" not in k.lower() and "dest" not in k.lower()
            }
            d["extra"] = safe
        return d


def channel_status(channel: str, *, ensure_playlist: bool = False) -> ChannelStatus:
    _load_dotenv()
    if ensure_playlist:
        ensure_default_playlists()
    url, key = _rtmp_url_and_key(channel)
    dest = _rtmp_destination(channel)
    pl = _playlist_path(channel)
    ok, detail = _playlist_ok(channel)
    pid = _read_pid(channel)
    alive = _pid_alive(pid, expect_substr="ffmpeg")
    if pid and not alive:
        _clear_pid(channel)
        pid = None
    # Adopt a live encode discovered via scan when pidfile is missing (legacy
    # historian_live_supervise / race survivors write progress but not pidfile).
    if not pid:
        scanned = list_channel_ffmpeg_pids(channel)
        if scanned:
            pid = scanned[0]
            alive = True
            try:
                _pid_path(channel).write_text(str(pid) + "\n", encoding="utf-8")
            except Exception:
                pass
    sup_pid = _read_supervisor_pid(channel)
    sup_alive = _pid_alive(sup_pid)
    if sup_pid and not sup_alive:
        _clear_supervisor_pid(channel)
        sup_pid = None
    if not sup_pid:
        scanned_sup = list_channel_supervise_pids(channel)
        if scanned_sup:
            # Prefer systemd MainPID when present.
            main = _systemd_main_pid(channel)
            sup_pid = main if main in scanned_sup else scanned_sup[0]
            sup_alive = True
            try:
                _supervisor_pid_path(channel).write_text(
                    str(sup_pid) + "\n", encoding="utf-8"
                )
            except Exception:
                pass
    health = _channel_health(channel)
    enc = encode_settings(channel)
    featured = _playlist_featured_media(channel)
    # Key may live in YT_LIVE_RTMP_KEY_* or be embedded in a full ingest URL.
    key_present = bool(key) or (dest is not None and bool(url) and not url.rstrip("/").endswith("/live2"))
    return ChannelStatus(
        channel=channel,
        enabled=_env_truthy("VOD_LOOP_ENABLED", "1"),
        has_rtmp_url=bool(url),
        has_rtmp_key=key_present,
        rtmp_ready=dest is not None,
        playlist=str(pl),
        playlist_ok=ok,
        playlist_detail=detail,
        pid=pid,
        alive=alive,
        supervisor_pid=sup_pid,
        supervisor_alive=sup_alive,
        log_path=str(_log_path(channel)),
        extra={
            "dest_configured": dest is not None,
            "input_mode": input_mode(),
            "featured_media": str(featured) if featured is not None else None,
            "encode": {
                "bitrate": enc["bitrate"],
                "height": enc["height"],
                "fps": enc["fps"],
                "gop": enc["gop"],
                "audio_bitrate": enc["audio_bitrate"],
                "source": enc.get("source"),
                "adaptive": enc.get("adaptive"),
            },
            "health": health,
            "progress_path": str(_progress_path(channel)),
        },
    )


def run_status(*, ensure_playlist: bool = True) -> dict[str, Any]:
    _load_dotenv()
    _ensure_dirs()
    playlist_note = ensure_default_playlists() if ensure_playlist else {}
    enc = encode_settings()
    channels = {ch: channel_status(ch).to_dict() for ch in CHANNELS}
    payload = {
        "ok": True,
        "ts": _utc_now(),
        "module": "vod_loop",
        "enabled": _env_truthy("VOD_LOOP_ENABLED", "1"),
        "ffmpeg": _ffmpeg_bin(),
        "encode": {
            "bitrate": enc["bitrate"],
            "maxrate": enc["maxrate"],
            "bufsize": enc["bufsize"],
            "height": enc["height"],
            "fps": enc["fps"],
            "gop": enc["gop"],
            "audio_bitrate": enc["audio_bitrate"],
            "preset": enc["preset"],
            "cbr_ish": True,
            "keyframe_sec": 2,
        },
        "stall_sec": enc["stall_sec"],
        "playlist_bootstrap": playlist_note,
        "channels": channels,
        "health_path": str(HEALTH_PATH),
        "policy": {
            "own_content_only": True,
            "no_fake_breaking_live": True,
            "separate_from_gpu_farm": True,
        },
    }
    _atomic_write_json(STATUS_PATH, payload)
    write_ops_heartbeat(source="status", action="status", channels=channels)
    return payload


def _video_audio_args(enc: dict[str, Any]) -> list[str]:
    height = int(enc["height"])
    fps = int(enc["fps"])
    gop = int(enc["gop"])
    # fast_bilinear: Live needs realtime more than lanczos polish; farm already
    # contending on the same 4 cores was keeping encodes at ~0.85–0.95×.
    vf = f"scale=-2:{height}:flags=fast_bilinear,fps={fps}"
    out = [
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        str(enc["preset"]),
        "-tune",
        "zerolatency",
        "-b:v",
        str(enc["bitrate"]),
        "-maxrate",
        str(enc["maxrate"]),
        "-bufsize",
        str(enc["bufsize"]),
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(gop),
        "-keyint_min",
        str(gop),
        "-sc_threshold",
        "0",
        "-r",
        str(fps),
        "-c:a",
        "aac",
        "-b:a",
        str(enc["audio_bitrate"]),
        "-ar",
        "44100",
        "-ac",
        "2",
    ]
    threads = int(enc.get("threads") or 0)
    if threads > 0:
        out.extend(["-threads", str(threads)])
    return out


def _parse_progress_speed(channel: str) -> float | None:
    """Last ffmpeg ``speed=`` token from the progress file (e.g. 0.87 → under-realtime)."""
    path = _progress_path(channel)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    speed: float | None = None
    for line in text.splitlines():
        if not line.startswith("speed="):
            continue
        raw = line.split("=", 1)[1].strip().rstrip("x").strip()
        try:
            speed = float(raw)
        except ValueError:
            continue
    return speed


def list_farm_ffmpeg_pids() -> list[int]:
    """Non-Live ffmpeg (compose/xfade/sfx) competing for CPU with RTMP encodes."""
    live: set[int] = set()
    for ch in CHANNELS:
        live.update(list_channel_ffmpeg_pids(ch))
    found: list[int] = []
    for pid in _iter_pids_with_substr("ffmpeg"):
        if pid in live:
            continue
        cmd = _cmdline_of(pid)
        if not cmd:
            continue
        argv0 = cmd.split("\0", 1)[0]
        if Path(argv0).name != "ffmpeg":
            continue
        flat = cmd.replace("\0", " ").lower()
        # Never touch anything pushing RTMP (belt-and-suspenders).
        if "rtmp://" in flat or "a.rtmp.youtube.com" in flat or "b.rtmp.youtube.com" in flat:
            continue
        found.append(pid)
    return sorted(set(found))


def yield_farm_cpu_for_live(*, dry_run: bool = False) -> dict[str, Any]:
    """Deprioritize / pause farm ffmpeg when Live encodes fall below realtime.

    Dual 720p Live on a 4-core VPS cannot share the box with two full-rate xfade
    encodes — Live drops to ~0.85×, YouTube disconnects, reconnect storm follows.
    Resume farm only after Live holds ≥``_LIVE_SPEED_RESUME`` for
    ``_FARM_RESUME_HOLD_SEC`` (anti thrash vs stream_beat).
    """
    _ensure_dirs()
    enabled = _env_truthy("VOD_LOOP_YIELD_FARM", "1")
    allow_stop = _env_truthy("VOD_LOOP_YIELD_STOP", "1")
    speeds = {ch: _parse_progress_speed(ch) for ch in CHANNELS}
    alive = {ch: channel_status(ch).alive for ch in CHANNELS}
    under = [
        ch
        for ch, sp in speeds.items()
        if alive.get(ch) and sp is not None and sp < _LIVE_SPEED_MIN
    ]
    # Prefer Live realtime signal over laggy loadavg: only force-yield when a
    # Live encode is actually under-realtime (or speed unknown while alive+CPU bad).
    unknown_while_hot = [
        ch
        for ch, sp in speeds.items()
        if alive.get(ch) and sp is None and not cpu_health().get("ok")
    ]
    need_yield = bool(under) or bool(unknown_while_hot)
    live_ok = all(
        (not alive.get(ch)) or (speeds.get(ch) is not None and speeds[ch] >= _LIVE_SPEED_MIN)
        for ch in CHANNELS
    )
    live_resume_ok = all(
        (not alive.get(ch))
        or (speeds.get(ch) is not None and speeds[ch] >= _LIVE_SPEED_RESUME)
        for ch in CHANNELS
    )
    farm = list_farm_ffmpeg_pids()
    state: dict[str, Any] = {}
    if _FARM_YIELD_STATE_PATH.is_file():
        try:
            raw = json.loads(_FARM_YIELD_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state = raw
        except Exception:
            state = {}
    stopped_prev = [int(x) for x in (state.get("stopped_pids") or []) if str(x).isdigit() or isinstance(x, int)]
    hold_sec = float(os.getenv("VOD_LOOP_FARM_RESUME_HOLD_SEC") or _FARM_RESUME_HOLD_SEC)
    live_ok_since = str(state.get("live_ok_since") or "") or None
    now_ts = _utc_now()
    if live_resume_ok:
        if not live_ok_since:
            live_ok_since = now_ts
    else:
        live_ok_since = None
    resume_hold_met = False
    if live_ok_since:
        try:
            from datetime import datetime

            started = datetime.fromisoformat(live_ok_since.replace("Z", "+00:00"))
            now_dt = datetime.fromisoformat(now_ts.replace("Z", "+00:00"))
            resume_hold_met = (now_dt - started).total_seconds() >= hold_sec
        except Exception:
            resume_hold_met = False

    actions: list[str] = []
    reniced: list[int] = []
    stopped: list[int] = []
    resumed: list[int] = []

    if not enabled:
        return {
            "ok": True,
            "enabled": False,
            "skipped": True,
            "speeds": speeds,
            "farm_pids": farm,
            "ts": _utc_now(),
        }

    if need_yield and farm:
        for pid in farm:
            if dry_run:
                actions.append(f"would_renice:{pid}")
                continue
            try:
                os.system(f"renice 19 -p {pid} >/dev/null 2>&1")  # noqa: S605
                reniced.append(pid)
            except Exception:
                pass
            if allow_stop:
                try:
                    os.kill(pid, signal.SIGSTOP)
                    stopped.append(pid)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    actions.append(f"stop_denied:{pid}")
        if stopped:
            actions.append("sigstop_farm")
        if reniced:
            actions.append("renice_farm_19")
        live_ok_since = None  # reset hold whenever we yield
    elif live_ok and live_resume_ok and resume_hold_met and stopped_prev:
        # Resume only after Live held ≥1.0× for hold_sec (prevents stop/cont thrash).
        for pid in stopped_prev:
            if dry_run:
                actions.append(f"would_cont:{pid}")
                continue
            try:
                os.kill(pid, signal.SIGCONT)
                resumed.append(pid)
            except ProcessLookupError:
                pass
            except PermissionError:
                actions.append(f"cont_denied:{pid}")
        if resumed:
            actions.append("sigcont_farm")
        # Keep farm reniced even after resume so Live wins scheduler ties.
        for pid in farm:
            if dry_run:
                continue
            try:
                os.system(f"renice 19 -p {pid} >/dev/null 2>&1")  # noqa: S605
                reniced.append(pid)
            except Exception:
                pass
        if reniced and "renice_farm_19" not in actions:
            actions.append("renice_farm_19")
    elif live_ok:
        # Holding: renice only; do not SIGCONT until resume_hold_met.
        for pid in farm:
            if dry_run:
                continue
            try:
                os.system(f"renice 19 -p {pid} >/dev/null 2>&1")  # noqa: S605
                reniced.append(pid)
            except Exception:
                pass
        if reniced:
            actions.append("renice_farm_19")
        if stopped_prev and not resume_hold_met:
            actions.append("resume_hold")

    new_stopped = sorted(set(stopped) | ({p for p in stopped_prev if p not in resumed and _pid_alive(p)}))
    if resumed and not need_yield and not under:
        new_stopped = [p for p in new_stopped if p not in resumed]

    payload = {
        "ok": True,
        "enabled": True,
        "dry_run": dry_run,
        "need_yield": need_yield,
        "under_realtime": under,
        "speeds": speeds,
        "farm_pids": farm,
        "reniced": reniced,
        "stopped": stopped,
        "resumed": resumed,
        "stopped_pids": new_stopped,
        "live_ok_since": live_ok_since,
        "resume_hold_met": resume_hold_met,
        "actions": actions,
        "ts": _utc_now(),
    }
    if not dry_run:
        _atomic_write_json(
            _FARM_YIELD_STATE_PATH,
            {
                "stopped_pids": new_stopped,
                "live_ok_since": live_ok_since,
                "last": payload,
                "updated_ts": _utc_now(),
            },
        )
        if actions:
            append_repair_note(
                "ops",
                reason="farm_cpu_yield",
                action="+".join(actions[:3]),
                detail=f"under={under} farm_n={len(farm)} stopped={len(new_stopped)}",
            )
    return payload


def _build_ffmpeg_cmd(channel: str, dest: str, *, progress_file: Path | None = None) -> list[str]:
    """Build FFmpeg argv for infinite Live encode.

    Default ``featured`` mode: ``-stream_loop -1 -i <playlist-head>``. This avoids
    concat-demuxer freezes when successive VODs have mismatched audio params
    (e.g. 24 kHz vs 96 kHz) — which previously stalled at ~end of the first
    ~30–40m VOD, triggered a restart, and ended the YouTube Live broadcast.

    Set ``VOD_LOOP_INPUT_MODE=concat`` only for the legacy multi-file demuxer.
    """
    enc = encode_settings(channel)
    mode = input_mode()
    cmd = [
        _ffmpeg_bin(),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-re",
        "-stream_loop",
        "-1",
    ]
    if mode == "concat":
        pl = _playlist_path(channel)
        cmd.extend(["-f", "concat", "-safe", "0", "-i", str(pl)])
    else:
        featured = _playlist_featured_media(channel)
        if featured is None:
            # Fall back to concat path so start_ffmpeg_once still surfaces playlist errors.
            pl = _playlist_path(channel)
            cmd.extend(["-f", "concat", "-safe", "0", "-i", str(pl)])
        else:
            cmd.extend(["-i", str(featured)])
    cmd.extend([*_video_audio_args(enc), "-f", "flv"])
    if progress_file is not None:
        cmd.extend(["-progress", str(progress_file), "-nostats"])
    cmd.append(dest)
    return cmd


def _parse_progress_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return {}
    # FFmpeg appends blocks; take the last complete-looking key set.
    data: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        data[k.strip()] = v.strip()
    out: dict[str, Any] = {}
    if "frame" in data:
        try:
            out["frame"] = int(data["frame"])
        except ValueError:
            out["frame"] = data["frame"]
    if "bitrate" in data:
        out["bitrate"] = data["bitrate"]
    if "out_time_ms" in data:
        try:
            out["out_time_ms"] = int(data["out_time_ms"])
        except ValueError:
            pass
    if "progress" in data:
        out["progress"] = data["progress"]
    try:
        out["mtime"] = path.stat().st_mtime
    except Exception:
        pass
    return out


def _progress_signal(channel: str) -> tuple[float | None, dict[str, Any]]:
    """Return (activity_epoch, parsed progress).

    Activity is **last time frame/out_time advanced** (health ``last_progress_ts``),
    not the progress-file mtime — FFmpeg can keep rewriting a frozen final block
    while wall-clock mtime moves (false "fresh" that delayed stall kills ~2m).
    """
    parsed = _parse_progress_file(_progress_path(channel))
    health = _channel_health(channel)
    last_ts = health.get("last_progress_ts")
    epoch_from_health: float | None = None
    if isinstance(last_ts, str) and last_ts:
        try:
            epoch_from_health = datetime.fromisoformat(last_ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            epoch_from_health = None
    return epoch_from_health, parsed


def _refresh_progress_health(channel: str) -> dict[str, Any]:
    parsed = _parse_progress_file(_progress_path(channel))
    if not parsed:
        return _channel_health(channel)
    fields: dict[str, Any] = {}
    if "frame" in parsed:
        fields["last_frame"] = parsed["frame"]
    if "bitrate" in parsed:
        fields["last_bitrate"] = parsed["bitrate"]
    if "out_time_ms" in parsed:
        fields["last_out_time_ms"] = parsed["out_time_ms"]
    # Only bump last_progress_ts when frame or out_time actually moves.
    prev = _channel_health(channel)
    moved = False
    if fields.get("last_frame") is not None and fields.get("last_frame") != prev.get("last_frame"):
        moved = True
    if fields.get("last_out_time_ms") is not None and fields.get(
        "last_out_time_ms"
    ) != prev.get("last_out_time_ms"):
        moved = True
    if moved:
        fields["last_progress_ts"] = _utc_now()
    if fields:
        return _update_channel_health(channel, **fields)
    return prev


def _is_stalled(channel: str, *, stall_sec: int | None = None) -> tuple[bool, str]:
    enc = encode_settings()
    limit = stall_sec if stall_sec is not None else int(enc["stall_sec"])
    activity, parsed = _progress_signal(channel)
    session_started_epoch: float | None = None
    if activity is None:
        health = _channel_health(channel)
        started = health.get("session_started_ts")
        if isinstance(started, str) and started:
            try:
                session_started_epoch = datetime.fromisoformat(
                    started.replace("Z", "+00:00")
                ).timestamp()
            except Exception:
                session_started_epoch = None
    stalled, detail = stall_from_activity(
        activity_epoch=activity,
        stall_sec=float(limit),
        session_started_epoch=session_started_epoch,
    )
    if stalled and activity is not None and "frame" in parsed:
        detail += f"_frame={parsed['frame']}"
    return stalled, detail


def start_ffmpeg_once(
    channel: str,
    *,
    dry_run: bool = False,
) -> ChannelStatus:
    """Spawn a single FFmpeg process (used by supervise / oneshot start).

    Always clears prior channel encodes first so two ffmpeg cannot coexist.
    Stays in the supervise process group (no ``start_new_session``) so SIGTERM
    / systemd KillMode can reap the encode with the supervisor.
    """
    _load_dotenv()
    _ensure_dirs()
    ensure_default_playlists()
    st = channel_status(channel)

    if not st.enabled:
        st.action = "disabled"
        st.message = "VOD_LOOP_ENABLED is off"
        return st

    if not st.playlist_ok:
        st.action = "error_playlist"
        st.message = st.playlist_detail
        return st

    if not st.rtmp_ready:
        st.action = "skipped_missing_rtmp"
        st.message = (
            f"Set YT_LIVE_RTMP_URL_{_channel_env_suffix(channel)} and "
            f"YT_LIVE_RTMP_KEY_{_channel_env_suffix(channel)} in .env "
            "(never commit real keys)"
        )
        return st

    dest = _rtmp_destination(channel)
    assert dest is not None
    progress = _progress_path(channel)
    try:
        if progress.is_file():
            progress.unlink()
    except Exception:
        pass
    cmd = _build_ffmpeg_cmd(channel, dest, progress_file=progress)

    if dry_run:
        st.action = "dry_run"
        st.message = "ffmpeg cmd built; RTMP present; not started"
        st.extra["cmd_bin"] = cmd[0]
        st.extra["cmd_argc"] = len(cmd)
        st.extra["encode"] = encode_settings(channel)
        return st

    # Hard single-ingest: never start a second encode beside a live one.
    for pid in list_channel_ffmpeg_pids(channel):
        _kill_pid(pid)
    _clear_pid(channel)
    deadline = time.time() + 6.0
    while list_channel_ffmpeg_pids(channel) and time.time() < deadline:
        time.sleep(0.2)
    leftover = list_channel_ffmpeg_pids(channel)
    if leftover:
        st.action = "start_failed"
        st.message = f"prior_ffmpeg_still_alive={leftover}"
        return st

    logf = _log_path(channel)
    enc = encode_settings(channel)
    header = (
        f"# vod_loop start {_utc_now()} channel={channel}\n"
        f"# input_mode={input_mode()} featured="
        f"{_playlist_featured_media(channel) or 'n/a'}\n"
        f"# encode bitrate={enc['bitrate']} height={enc['height']} "
        f"fps={enc['fps']} gop={enc['gop']} source={enc.get('source')} "
        f"(RTMP dest redacted)\n"
        f"# ffmpeg argv count={len(cmd)}\n"
    )
    with logf.open("a", encoding="utf-8") as lf:
        lf.write(header)

        def _live_preexec() -> None:
            # Best-effort: prefer Live over farm (negative nice needs CAP_SYS_NICE).
            try:
                os.nice(-5)
            except Exception:
                try:
                    os.nice(0)
                except Exception:
                    pass

        proc = subprocess.Popen(
            cmd,
            stdout=lf,
            stderr=subprocess.STDOUT,
            cwd=str(ROOT),
            # Keep ffmpeg in supervise cgroup/process tree for clean stop.
            start_new_session=False,
            preexec_fn=_live_preexec,
        )
    _pid_path(channel).write_text(str(proc.pid) + "\n", encoding="utf-8")
    _update_channel_health(
        channel,
        session_started_ts=_utc_now(),
        last_exit_code=None,
    )
    time.sleep(0.8)
    # Final race check: if another encode appeared, keep ours only if sole;
    # otherwise collapse to a single ingest.
    ff_now = list_channel_ffmpeg_pids(channel)
    if len(ff_now) > 1:
        ensure_single_ingest(channel, keep_ffmpeg_pid=proc.pid)
        ff_now = list_channel_ffmpeg_pids(channel)
    alive = _pid_alive(proc.pid, expect_substr="ffmpeg") and proc.pid in ff_now
    st.pid = proc.pid if alive else (ff_now[0] if len(ff_now) == 1 else None)
    st.alive = bool(st.pid) and _pid_alive(st.pid, expect_substr="ffmpeg")
    if st.alive:
        st.action = "started"
        st.message = f"pid={st.pid}"
    else:
        st.action = "start_failed"
        st.message = f"ffmpeg exited early; see {logf.name}"
        _clear_pid(channel)
    return st


def start_channel(
    channel: str,
    *,
    dry_run: bool = False,
    force: bool = False,
    oneshot: bool = False,
) -> ChannelStatus:
    """Start supervised loop (default) or a single FFmpeg process."""
    _load_dotenv()
    _ensure_dirs()
    ensure_default_playlists()
    st = channel_status(channel)

    if not st.enabled and not force:
        st.action = "disabled"
        st.message = "VOD_LOOP_ENABLED is off"
        return st

    if not st.playlist_ok:
        st.action = "error_playlist"
        st.message = st.playlist_detail
        return st

    if not st.rtmp_ready:
        st.action = "skipped_missing_rtmp"
        st.message = (
            f"Set YT_LIVE_RTMP_URL_{_channel_env_suffix(channel)} and "
            f"YT_LIVE_RTMP_KEY_{_channel_env_suffix(channel)} in .env "
            "(never commit real keys)"
        )
        return st

    if oneshot or _env_truthy("VOD_LOOP_ONESHOT", "0"):
        if _systemd_owns_channel(channel) and channel not in _SUPERVISE_LOCK_FDS:
            st.action = "refused_systemd_owns"
            st.message = "systemd owns channel; refuse oneshot beside vod-loop@ unit"
            return st
        if st.alive and not force:
            st.action = "already_running"
            st.message = f"pid={st.pid}"
            return st
        if st.alive and force:
            stop_channel(channel)
        return start_ffmpeg_once(channel, dry_run=dry_run)

    # systemd unit is sole owner — NEVER spawn a second detached supervise.
    if _systemd_owns_channel(channel):
        if dry_run:
            st.action = "dry_run"
            st.message = "systemd owns channel; would systemctl start/restart"
            st.extra["encode"] = encode_settings(channel)
            return st
        if _systemd_unit_active(channel) and not force:
            cleaned = ensure_single_ingest(
                channel,
                keep_ffmpeg_pid=st.pid,
                keep_supervise_pid=st.supervisor_pid,
            )
            st.action = "already_running"
            st.message = (
                f"systemd_active supervisor_pid={st.supervisor_pid} "
                f"ffmpeg_pid={st.pid} ffmpeg_n={len(cleaned.get('ffmpeg_pids') or [])}"
            )
            st.extra["ingest"] = {
                "dual_ingest": cleaned.get("dual_ingest"),
                "killed_ffmpeg": cleaned.get("killed_ffmpeg"),
                "killed_supervise": cleaned.get("killed_supervise"),
            }
            return st
        try:
            if _systemd_unit_active(channel) or force:
                action = "restart"
                proc = _systemctl_run("restart", f"vod-loop@{channel}", timeout=90)
            else:
                # Enabled but inactive: try-restart is a no-op → use start.
                action = "start"
                proc = _systemctl_run("start", f"vod-loop@{channel}", timeout=90)
                if proc.returncode != 0:
                    action = "restart"
                    proc = _systemctl_run("restart", f"vod-loop@{channel}", timeout=90)
            time.sleep(1.2)
            st = channel_status(channel)
            st.action = "systemd_started" if proc.returncode == 0 else "start_failed"
            st.message = f"systemctl {action} rc={proc.returncode}"
            if proc.returncode == 0:
                st.extra["live_meta"] = _maybe_sync_live_meta(
                    channel, reason="start_systemd"
                )
            return st
        except Exception as exc:
            st.action = "start_failed"
            st.message = f"systemd_error:{type(exc).__name__}"
            return st

    # Supervised mode (default): long-lived reconnect loop (no systemd unit).
    if st.supervisor_alive and not force:
        st.action = "already_running"
        st.message = f"supervisor_pid={st.supervisor_pid} ffmpeg_pid={st.pid}"
        return st

    if (st.supervisor_alive or st.alive) and force:
        stop_channel(channel)

    if dry_run:
        st.action = "dry_run"
        st.message = "supervise mode; RTMP present; not started"
        st.extra["encode"] = encode_settings(channel)
        return st

    # Last-chance gate: re-check systemd before any detached Popen.
    if _systemd_owns_channel(channel):
        st.action = "refused_systemd_owns"
        st.message = "systemd became owner; refuse detached supervise"
        return st

    py = os.getenv("VOD_LOOP_PYTHON") or str(ROOT / ".venv" / "bin" / "python")
    if not Path(py).is_file():
        py = os.environ.get("PYTHON") or "python3"
    logf = _log_path(channel)
    cmd = [py, "-m", "src.cli.vod_loop", "supervise", "--channel", channel]
    with logf.open("a", encoding="utf-8") as lf:
        lf.write(f"# vod_loop supervise spawn {_utc_now()} channel={channel}\n")
        proc = subprocess.Popen(
            cmd,
            stdout=lf,
            stderr=subprocess.STDOUT,
            cwd=str(ROOT),
            start_new_session=True,
        )
    _supervisor_pid_path(channel).write_text(str(proc.pid) + "\n", encoding="utf-8")
    _update_channel_health(channel, supervised=True, session_started_ts=_utc_now())
    time.sleep(1.0)
    st = channel_status(channel)
    if st.supervisor_alive or st.alive:
        st.action = "started_supervise"
        st.message = f"supervisor_pid={st.supervisor_pid} ffmpeg_pid={st.pid}"
        st.extra["live_meta"] = _maybe_sync_live_meta(channel, reason="start_supervise")
    else:
        st.action = "start_failed"
        st.message = f"supervisor exited early; see {logf.name}"
        _clear_supervisor_pid(channel)
    return st


def stop_channel(channel: str) -> ChannelStatus:
    _load_dotenv()
    st = channel_status(channel)
    sup = st.supervisor_pid
    pid = st.pid
    if not sup and not pid:
        st.action = "not_running"
        st.message = "no pid file"
        return st

    if sup:
        _kill_pid(sup)
        _clear_supervisor_pid(channel)
    if pid:
        _kill_pid(pid)
        _clear_pid(channel)
    # Re-read in case supervise respawned ffmpeg briefly.
    leftover = _read_pid(channel)
    if leftover and _pid_alive(leftover):
        _kill_pid(leftover)
        _clear_pid(channel)

    _update_channel_health(channel, supervised=False, last_restart_reason="stopped")
    st.action = "stopped"
    st.message = f"was_supervisor={sup} was_ffmpeg={pid}"
    st.alive = False
    st.pid = None
    st.supervisor_alive = False
    st.supervisor_pid = None
    return st


def supervise_channel(channel: str, *, once: bool = False) -> dict[str, Any]:
    """Infinite (or single-cycle) FFmpeg supervisor with backoff + stall detection.

    Never gives up after N failures — only exits on SIGTERM/SIGINT or channel
    disable / missing RTMP when ``once`` is used for tests.
    """
    _load_dotenv()
    _ensure_dirs()
    ensure_default_playlists()
    if channel not in CHANNELS:
        return {"ok": False, "error": f"unknown_channel:{channel}"}

    claim = claim_supervisor_singleton(channel)
    if not claim.get("ok"):
        # Another supervise already owns this channel — exit cleanly (no dual ingest).
        return {
            "ok": False,
            "channel": channel,
            "error": "supervise_lock_held",
            "claim": claim,
            "ts": _utc_now(),
        }

    stop = {"flag": False}

    def _handle_stop(signum: int, _frame: Any) -> None:
        stop["flag"] = True
        pid = _read_pid(channel)
        if pid and _pid_alive(pid):
            _kill_pid(pid)

    try:
        signal.signal(signal.SIGTERM, _handle_stop)
        signal.signal(signal.SIGINT, _handle_stop)
    except Exception:
        pass

    _supervisor_pid_path(channel).write_text(str(os.getpid()) + "\n", encoding="utf-8")
    enc = encode_settings(channel)
    backoff = float(enc["backoff_base_sec"])
    backoff_max = float(enc["backoff_max_sec"])
    cycles = 0

    _update_channel_health(
        channel,
        supervised=True,
        backoff_sec=backoff,
        session_started_ts=_utc_now(),
    )

    while not stop["flag"]:
        if not _env_truthy("VOD_LOOP_ENABLED", "1"):
            _update_channel_health(channel, last_restart_reason="disabled")
            break

        st = start_ffmpeg_once(channel, dry_run=False)
        if st.action == "skipped_missing_rtmp":
            _update_channel_health(channel, last_restart_reason="missing_rtmp")
            if once:
                break
            time.sleep(min(60.0, backoff_max))
            continue
        if st.action in {"disabled", "error_playlist", "start_failed"}:
            reason = st.action
            health = _channel_health(channel)
            _update_channel_health(
                channel,
                restarts=int(health.get("restarts") or 0) + 1,
                last_restart_reason=reason,
                last_restart_ts=_utc_now(),
                backoff_sec=backoff,
            )
            if once:
                break
            time.sleep(backoff)
            backoff = next_backoff_sec(
                backoff,
                base=float(enc["backoff_base_sec"]),
                cap=backoff_max,
            )
            continue

        ffmpeg_pid = st.pid
        assert ffmpeg_pid is not None
        last_seen_frame: Any = None
        last_seen_out_ms: Any = None
        last_move = time.time()
        exit_code: int | None = None
        restart_reason = "ffmpeg_exit"

        while not stop["flag"] and _pid_alive(ffmpeg_pid, expect_substr="ffmpeg"):
            _refresh_progress_health(channel)
            parsed = _parse_progress_file(_progress_path(channel))
            frame = parsed.get("frame")
            out_ms = parsed.get("out_time_ms")
            moved = False
            if frame is not None and frame != last_seen_frame:
                last_seen_frame = frame
                moved = True
            if out_ms is not None and out_ms != last_seen_out_ms:
                last_seen_out_ms = out_ms
                moved = True
            if moved:
                last_move = time.time()
                _update_channel_health(
                    channel,
                    last_progress_ts=_utc_now(),
                    last_out_time_ms=out_ms if out_ms is not None else _channel_health(channel).get("last_out_time_ms"),
                    last_bitrate=parsed.get("bitrate"),
                    last_frame=frame if frame is not None else _channel_health(channel).get("last_frame"),
                )

            stalled, detail = _is_stalled(channel)
            # Also use in-loop last_move in case progress file is sticky.
            if (time.time() - last_move) > float(enc["stall_sec"]):
                stalled, detail = True, f"loop_stale_{int(time.time() - last_move)}s"
            if stalled:
                restart_reason = f"stall:{detail}"
                health = _channel_health(channel)
                _update_channel_health(
                    channel,
                    stall_restarts=int(health.get("stall_restarts") or 0) + 1,
                    last_restart_reason=restart_reason,
                    last_restart_ts=_utc_now(),
                )
                _kill_pid(ffmpeg_pid)
                break
            time.sleep(5.0)

        if _pid_alive(ffmpeg_pid):
            _kill_pid(ffmpeg_pid)
        try:
            # Best-effort wait if we still have a handle — may already be gone.
            os.waitpid(ffmpeg_pid, os.WNOHANG)
        except Exception:
            pass
        _clear_pid(channel)

        health = _channel_health(channel)
        _update_channel_health(
            channel,
            restarts=int(health.get("restarts") or 0) + 1,
            last_restart_reason=restart_reason,
            last_restart_ts=_utc_now(),
            last_exit_code=exit_code,
            backoff_sec=backoff,
            supervised=True,
        )
        cycles += 1
        run_status(ensure_playlist=False)

        if once or stop["flag"]:
            break

        time.sleep(backoff)
        backoff = next_backoff_sec(
            backoff,
            base=float(enc["backoff_base_sec"]),
            cap=backoff_max,
        )
        # After a long healthy run, reset backoff (progress moved recently).
        stalled_now, _ = _is_stalled(channel, stall_sec=int(enc["stall_sec"]))
        if not stalled_now and (time.time() - last_move) < 60:
            backoff = float(enc["backoff_base_sec"])

    _clear_supervisor_pid(channel)
    _update_channel_health(channel, supervised=False)
    return {
        "ok": True,
        "channel": channel,
        "cycles": cycles,
        "stopped": stop["flag"],
        "ts": _utc_now(),
    }


def _systemd_unit_active(channel: str) -> bool:
    try:
        proc = _systemctl_run("is-active", f"vod-loop@{channel}", timeout=5)
        return (proc.stdout or "").strip() == "active"
    except Exception:
        return False


def _restart_channel_for_bitrate(channel: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Apply new encode settings by restarting systemd unit or supervise process."""
    if dry_run:
        return {
            "restarted": False,
            "dry_run": True,
            "method": "systemd" if _systemd_owns_channel(channel) else "supervise",
        }
    if _systemd_owns_channel(channel):
        try:
            proc = _systemctl_run("restart", f"vod-loop@{channel}", timeout=60)
            return {
                "restarted": proc.returncode == 0,
                "method": "systemd",
                "rc": proc.returncode,
            }
        except Exception as exc:
            return {"restarted": False, "method": "systemd", "error": type(exc).__name__}
    # Fallback: bounce in-process supervise / ffmpeg (no keys printed).
    try:
        stop_channel(channel)
        st = start_channel(channel, dry_run=False, force=False)
        return {
            "restarted": st.action in {
                "started",
                "started_supervise",
                "already_running",
                "systemd_started",
            },
            "method": "supervise",
            "action": st.action,
        }
    except Exception as exc:
        return {"restarted": False, "method": "supervise", "error": type(exc).__name__}


def _ladder_for_policy(policy: dict[str, Any], *, cpu: dict[str, Any]) -> tuple[list[int], int]:
    """Return (ladder_k, height) — optional 1080p only when CPU + policy allow."""
    ladder = [max(500, int(x)) for x in (policy.get("ladder_k") or list(_DEFAULT_LADDER_K))]
    cap = max(500, int(policy.get("cap_k") or ladder[-1]))
    ladder = [min(v, cap) for v in ladder]
    height = max(360, int(policy.get("height") or _DEFAULT_HEIGHT))
    allow_1080 = bool(policy.get("allow_1080p")) and _env_truthy("VOD_LOOP_ALLOW_1080", "0")
    min_cores = int(policy.get("1080_min_cores") or 6)
    ratio_1080 = float(policy.get("1080_cpu_load_per_core_max") or 0.45)
    cores = int(cpu.get("cores") or _cpu_nproc())
    load1 = cpu.get("load1")
    if (
        allow_1080
        and cores >= min_cores
        and load1 is not None
        and float(load1) < ratio_1080 * cores
    ):
        ladder_1080 = [max(500, int(x)) for x in (policy.get("ladder_1080_k") or [4500, 6000])]
        # Extend ladder with 1080 rungs only when explicitly unlocked.
        ladder = ladder + [v for v in ladder_1080 if v not in ladder]
        height = 1080
    return ladder, height


def adaptive_bitrate_tick(
    *,
    dry_run: bool = False,
    healthcheck_results: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Step bitrate up/down from industrial ladder based on CPU + stream health.

    Up: both CPU healthy and channel stream healthy for K consecutive */10 checks.
    Down/hold: unhealthy → step down one rung (if enabled) or hold streak at 0.
    Persists rung in ``output/ops/stream_bitrate_policy.json`` (never RTMP keys).
    """
    _load_dotenv()
    _ensure_dirs()
    policy = load_bitrate_policy(ensure=True)
    adaptive_on = bool(policy.get("enabled", True)) and _env_truthy("VOD_LOOP_ADAPTIVE", "1")
    cpu = cpu_health(policy)
    needed = max(1, int(policy.get("healthy_streak_needed") or _DEFAULT_HEALTHY_STREAK))
    step_down = bool(policy.get("step_down_on_unhealthy", True))
    restart_on_step = bool(policy.get("restart_on_step", True))
    ladder, height_default = _ladder_for_policy(policy, cpu=cpu)
    hc = healthcheck_results or {}
    channels_out: dict[str, Any] = {}
    changed = False

    if not adaptive_on:
        return {
            "ok": True,
            "enabled": False,
            "skipped": True,
            "reason": "adaptive_disabled",
            "cpu": cpu,
            "policy_path": str(BITRATE_POLICY_PATH),
            "channels": {},
            "ts": _utc_now(),
        }

    for ch in CHANNELS:
        row = dict((policy.get("channels") or {}).get(ch) or {})
        try:
            rung = int(row.get("rung") if row.get("rung") is not None else policy.get("default_rung") or 2)
        except Exception:
            rung = 2
        rung = max(0, min(rung, len(ladder) - 1))
        bitrate_k = int(ladder[rung])
        try:
            streak = int(row.get("healthy_streak") or 0)
        except Exception:
            streak = 0
        hc_action = None
        if isinstance(hc.get(ch), dict):
            hc_action = hc[ch].get("action")
        stream = stream_channel_healthy(
            ch,
            healthcheck_action=hc_action,
            policy=policy,
            target_bitrate_k=bitrate_k,
        )
        both_ok = bool(cpu.get("ok")) and bool(stream.get("ok"))
        action = "hold"
        restarted: dict[str, Any] | None = None
        prev_rung = rung
        prev_bitrate = bitrate_k
        st = channel_status(ch)
        active_like = bool(st.rtmp_ready) and (
            st.alive
            or st.supervisor_alive
            or (hc_action or "")
            not in {"skipped_missing_rtmp", "disabled", ""}
        )

        if both_ok:
            streak += 1
            if streak >= needed and rung < (len(ladder) - 1):
                rung += 1
                bitrate_k = int(ladder[rung])
                streak = 0
                action = "step_up"
                changed = True
            else:
                action = "hold_healthy"
                bitrate_k = int(ladder[rung])
        else:
            streak = 0
            bitrate_k = int(ladder[rung])
            if not active_like:
                action = "hold_inactive"
            elif step_down and rung > 0 and (
                not cpu.get("ok")
                or st.alive
                or (hc_action or "").startswith("stall")
            ):
                rung -= 1
                bitrate_k = int(ladder[rung])
                action = "step_down"
                changed = True
            else:
                action = "hold_unhealthy"

        height = int(row.get("height") or height_default)
        if height_default == 1080 and action == "step_up":
            height = 1080
        elif int(policy.get("height") or 720) == 720:
            height = int(policy.get("height") or 720)

        if action in {"step_up", "step_down"} and restart_on_step:
            restarted = _restart_channel_for_bitrate(ch, dry_run=dry_run)

        row.update(
            {
                "rung": rung,
                "bitrate_k": bitrate_k,
                "height": height,
                "healthy_streak": streak,
                "last_action": action,
                "last_cpu_ok": bool(cpu.get("ok")),
                "last_stream_ok": bool(stream.get("ok")),
                "last_stream_detail": stream.get("detail"),
                "prev_rung": prev_rung,
                "prev_bitrate_k": prev_bitrate,
                "updated_ts": _utc_now(),
            }
        )
        policy.setdefault("channels", {})[ch] = row
        channels_out[ch] = {
            "action": action,
            "rung": rung,
            "bitrate_k": bitrate_k,
            "height": height,
            "healthy_streak": streak,
            "healthy_streak_needed": needed,
            "stream": stream,
            "restart": restarted,
            "changed": action in {"step_up", "step_down"},
        }

    if not dry_run:
        save_bitrate_policy(policy)

    payload = {
        "ok": True,
        "enabled": True,
        "dry_run": dry_run,
        "changed": any(v.get("changed") for v in channels_out.values()),
        "cpu": cpu,
        "ladder_k": ladder,
        "height_default": height_default,
        "healthy_streak_needed": needed,
        "channels": channels_out,
        "policy_path": str(BITRATE_POLICY_PATH),
        "ts": _utc_now(),
        "note": "RTMP keys never stored in policy JSON",
    }
    if not dry_run:
        _atomic_write_json(OPS / "stream_bitrate_adaptive_last.json", payload)
    return payload


def healthcheck_channel(channel: str, *, dry_run: bool = False) -> ChannelStatus:
    """Restart if configured but dead, stalled, dual-ingest, or poor Live performance."""
    _load_dotenv()
    ensure_default_playlists()
    if not dry_run and not _acquire_channel_op_lock(channel):
        st = channel_status(channel)
        st.action = "op_lock_held"
        st.message = "another healthcheck/relive owns this channel"
        return st
    try:
        return _healthcheck_channel_locked(channel, dry_run=dry_run)
    finally:
        if not dry_run:
            _release_channel_op_lock(channel)


def _healthcheck_channel_locked(channel: str, *, dry_run: bool = False) -> ChannelStatus:
    st = channel_status(channel)

    if not st.enabled:
        st.action = "disabled"
        st.message = "VOD_LOOP_ENABLED off"
        return st

    if not st.rtmp_ready:
        st.action = "skipped_missing_rtmp"
        st.message = "RTMP URL/key not configured — dry-run/skip"
        return st

    if not st.playlist_ok:
        st.action = "error_playlist"
        st.message = st.playlist_detail
        return st

    # Dual-ingest / duplicate supervise — always collapse to one primary encode.
    ff_pids = list_channel_ffmpeg_pids(channel)
    sup_pids = list_channel_supervise_pids(channel)
    st.extra["ffmpeg_count"] = len(ff_pids)
    st.extra["supervise_count"] = len(sup_pids)
    if len(ff_pids) > 1 or len(sup_pids) > 1:
        st.action = "dual_ingest_repair"
        st.message = f"ffmpeg_n={len(ff_pids)} supervise_n={len(sup_pids)}"
        if dry_run:
            return st
        cleaned = ensure_single_ingest(
            channel,
            keep_ffmpeg_pid=st.pid,
            keep_supervise_pid=st.supervisor_pid,
        )
        append_repair_note(
            channel,
            reason="dual_ingest",
            action="ensure_single_ingest",
            detail=(
                f"killed_ff={cleaned.get('killed_ffmpeg')} "
                f"killed_sup={cleaned.get('killed_supervise')}"
            ),
        )
        # If duplicates persist or systemd is confused, full re-live via systemctl only.
        if len(list_channel_ffmpeg_pids(channel)) > 1 or len(list_channel_supervise_pids(channel)) > 1:
            relive = relive_channel(channel, reason="dual_ingest", dry_run=False)
            st.action = "relive_dual_ingest"
            st.message = f"relive={relive.get('action')} method={relive.get('method')}"
            st.extra["relive"] = {
                k: relive.get(k)
                for k in ("ok", "action", "method", "reason", "restarted")
            }
            return st
        st = channel_status(channel)
        st.action = "dual_ingest_cleared"
        st.message = f"kept_ffmpeg={cleaned.get('kept_ffmpeg')}"
        st.extra["ffmpeg_count"] = len(list_channel_ffmpeg_pids(channel))
        return st

    perf = performance_issues(channel)
    st.extra["performance"] = {
        "issues": perf.get("issues"),
        "needs_repair": perf.get("needs_repair"),
        "ffmpeg_count": perf.get("ffmpeg_count"),
    }
    repair_triggers = {
        "bitrate_collapse",
        "reconnect_storm",
        "dead_encode",
        "rtmp_open_error",
        "rtmp_io_error",
        "dual_ingest_youtube",
        "supervise_without_encode",
    }
    hit = [i for i in (perf.get("issues") or []) if i in repair_triggers]
    if hit and not dry_run:
        # Soften: log-error / reconnect_storm alone must not bounce a healthy encode.
        hard: list[str] = []
        for i in hit:
            if i == "reconnect_storm" and st.alive:
                continue
            if i in {"rtmp_open_error", "rtmp_io_error", "dual_ingest_youtube"} and st.alive:
                # Current session log may still be mid-recovery; require not-alive or collapse.
                if "bitrate_collapse" not in hit and "dead_encode" not in hit:
                    continue
            if i == "supervise_without_encode" and "reconnect_storm" not in hit:
                # Brief gaps between ffmpeg exits are normal — waiting path handles timeout.
                continue
            hard.append(i)
        if hard:
            relive = relive_channel(channel, reason="+".join(hard[:3]), dry_run=False)
            st.action = "relive_performance"
            st.message = f"issues={hard} relive={relive.get('action')}"
            st.extra["relive"] = {
                k: relive.get(k)
                for k in ("ok", "action", "method", "reason", "restarted")
            }
            return st

    # Refresh progress into health JSON when alive.
    if st.alive:
        _refresh_progress_health(channel)
        stalled, detail = _is_stalled(channel)
        if stalled:
            st.action = "stall_restart"
            st.message = detail
            if dry_run:
                return st
            if st.pid:
                _kill_pid(st.pid)
                _clear_pid(channel)
            health = _channel_health(channel)
            _update_channel_health(
                channel,
                stall_restarts=int(health.get("stall_restarts") or 0) + 1,
                restarts=int(health.get("restarts") or 0) + 1,
                last_restart_reason=f"healthcheck_stall:{detail}",
                last_restart_ts=_utc_now(),
            )
            if st.supervisor_alive:
                st.message = f"killed stalled ffmpeg; supervise will respawn ({detail})"
                st.action = "stall_signaled"
                return st
            # No supervise — bounce via systemd if present, else start.
            if _systemd_owns_channel(channel):
                relive = relive_channel(channel, reason=f"stall:{detail}", dry_run=False)
                st.action = "relive_stall"
                st.message = f"relive={relive.get('action')}"
                return st
            return start_channel(channel, dry_run=False, force=False)

        st.action = "healthy"
        st.message = f"pid={st.pid} ffmpeg_n={len(ff_pids)}"
        st.extra["health"] = _channel_health(channel)
        # Idempotent packaging sync when Live is healthy (title/desc/thumb vs featured).
        if not dry_run:
            st.extra["live_meta"] = _maybe_sync_live_meta(
                channel, reason="healthcheck_healthy"
            )
        return st

    if st.supervisor_alive:
        # Supervisor up but ffmpeg not yet / between restarts.
        # If waiting too long with rtmp_ready, nudge re-live (performance watchdog).
        health = _channel_health(channel)
        session_ts = health.get("session_started_ts") or health.get("last_restart_ts")
        waiting_too_long = False
        if isinstance(session_ts, str) and session_ts:
            try:
                epoch = datetime.fromisoformat(session_ts.replace("Z", "+00:00")).timestamp()
                waiting_too_long = (time.time() - epoch) > 90
            except Exception:
                waiting_too_long = False
        if waiting_too_long and not dry_run:
            relive = relive_channel(channel, reason="supervise_waiting_timeout", dry_run=False)
            st.action = "relive_waiting"
            st.message = f"supervisor_pid={st.supervisor_pid} relive={relive.get('action')}"
            return st
        st.action = "supervise_waiting"
        st.message = f"supervisor_pid={st.supervisor_pid}"
        return st

    # Dead or never started → prefer systemd start/restart (never detach a second supervise).
    if _systemd_owns_channel(channel):
        if dry_run:
            st.action = "would_systemd_start"
            st.message = "systemd unit enabled; would start/relive"
            return st
        relive = relive_channel(channel, reason="dead_encode", dry_run=False)
        st.action = "relive_dead"
        st.message = f"relive={relive.get('action')} method={relive.get('method')}"
        st.extra["relive"] = {
            k: relive.get(k) for k in ("ok", "action", "method", "reason", "restarted")
        }
        return st
    return start_channel(channel, dry_run=dry_run, force=False)


def healthcheck_all(*, dry_run: bool = False) -> dict[str, Any]:
    _load_dotenv()
    _ensure_dirs()
    if not dry_run and not _acquire_healthcheck_lock():
        return {
            "ok": True,
            "ts": _utc_now(),
            "module": "vod_loop",
            "action": "healthcheck",
            "skipped": "healthcheck_lock_held",
            "channels": {},
        }
    # Yield farm CPU before stall/relive decisions so Live can recover realtime.
    farm_yield: dict[str, Any] = {}
    try:
        farm_yield = yield_farm_cpu_for_live(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        farm_yield = {"ok": False, "error": str(exc)[:200]}
    results = {ch: healthcheck_channel(ch, dry_run=dry_run).to_dict() for ch in CHANNELS}
    any_error = any(
        str(v.get("action", "")).startswith("error") or v.get("action") == "start_failed"
        for v in results.values()
    )
    adaptive = adaptive_bitrate_tick(dry_run=dry_run, healthcheck_results=results)
    enc = encode_settings()
    payload = {
        "ok": not any_error,
        "ts": _utc_now(),
        "module": "vod_loop",
        "action": "healthcheck",
        "dry_run": dry_run,
        "enabled": _env_truthy("VOD_LOOP_ENABLED", "1"),
        "farm_yield": {
            "need_yield": farm_yield.get("need_yield"),
            "under_realtime": farm_yield.get("under_realtime"),
            "actions": farm_yield.get("actions"),
            "stopped_n": len(farm_yield.get("stopped_pids") or []),
            "speeds": farm_yield.get("speeds"),
        },
        "encode": {
            "bitrate": enc["bitrate"],
            "height": enc["height"],
            "fps": enc["fps"],
            "gop": enc["gop"],
            "source": enc.get("source"),
            "adaptive": enc.get("adaptive"),
            "preset": enc.get("preset"),
            "threads": enc.get("threads"),
        },
        "adaptive": {
            "enabled": adaptive.get("enabled"),
            "changed": adaptive.get("changed"),
            "cpu": adaptive.get("cpu"),
            "ladder_k": adaptive.get("ladder_k"),
            "channels": {
                ch: {
                    "action": (adaptive.get("channels") or {}).get(ch, {}).get("action"),
                    "rung": (adaptive.get("channels") or {}).get(ch, {}).get("rung"),
                    "bitrate_k": (adaptive.get("channels") or {}).get(ch, {}).get("bitrate_k"),
                    "healthy_streak": (adaptive.get("channels") or {}).get(ch, {}).get(
                        "healthy_streak"
                    ),
                }
                for ch in CHANNELS
            },
            "policy_path": adaptive.get("policy_path"),
        },
        "channels": results,
        "status_path": str(STATUS_PATH),
        "health_path": str(HEALTH_PATH),
    }
    _atomic_write_json(STATUS_PATH, payload)
    write_ops_heartbeat(source="healthcheck", action="healthcheck", channels=results)
    # Resolve-then-email (cooldown) — never spam on every */10 tick.
    try:
        from src.streaming.live_alerts import after_healthcheck_alerts

        payload["live_alerts"] = after_healthcheck_alerts(payload, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        payload["live_alerts"] = {"ok": False, "error": str(exc)[:200]}
    return payload


def install_crontab_hint() -> str:
    py = str(ROOT / ".venv" / "bin" / "python")
    line = (
        f"*/10 * * * * cd {ROOT} && {py} -m src.cli.vod_loop healthcheck "
        f">> {OPS / 'vod_loop.log'} 2>&1"
    )
    return (
        "# === 365d VOD-loop livestream healthcheck (CPU; not GPU farm) ===\n"
        f"{line}\n"
    )


def install_crontab() -> Path:
    """Merge vod_loop healthcheck into user crontab (idempotent)."""
    _ensure_dirs()
    hint = install_crontab_hint()
    CRONTAB_HINT_PATH.write_text(hint, encoding="utf-8")
    marker = "src.cli.vod_loop healthcheck"
    proc = subprocess.run(
        ["crontab", "-l"],
        check=False,
        capture_output=True,
        text=True,
    )
    existing = proc.stdout or ""
    lines = existing.splitlines()
    kept = [ln for ln in lines if marker not in ln]
    # Drop our section header if orphaned duplicate.
    out_lines = [ln for ln in kept if "VOD-loop livestream healthcheck" not in ln]
    if out_lines and out_lines[-1].strip():
        out_lines.append("")
    out_lines.extend(hint.strip().splitlines())
    out_lines.append("")
    body = "\n".join(out_lines) + "\n"
    subprocess.run(
        ["crontab", "-"],
        input=body,
        check=True,
        text=True,
        capture_output=True,
    )
    return CRONTAB_HINT_PATH


def render_systemd_unit(*, root: Path | None = None, user: str = "ubuntu") -> str:
    """Render vod-loop@.service with absolute paths for this checkout."""
    root = root or ROOT
    py = root / ".venv" / "bin" / "python"
    return f"""# Generated by src.streaming.vod_loop — do not put RTMP keys here.
# Docs: {root.as_posix()}/output/ops/VOD_LOOP.md
# Enable: sudo systemctl enable --now vod-loop@napstorian
#         sudo systemctl enable --now vod-loop@napping_historian
# Cron */10 healthcheck is optional backup (systemd is primary durability).
# Isolated from RunPod GPU farm — no gpu_lock / no StreamCast binaries.

[Unit]
Description=VOD-loop YouTube Live supervise (%i)
Documentation=file://{root.as_posix()}/output/ops/VOD_LOOP.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={root.as_posix()}
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-{root.as_posix()}/.env
ExecStart={py.as_posix()} -m src.cli.vod_loop supervise --channel %i
Restart=always
RestartSec=10
# Soft stop → supervise SIGTERM → ffmpeg SIGTERM
KillMode=mixed
TimeoutStopSec=30
# In-process exponential backoff (5s→…→300s) handles ingest flaps;
# systemd RestartSec is the outer safety net after supervise exits.

[Install]
WantedBy=multi-user.target
"""


def write_systemd_unit(*, user: str = "ubuntu") -> Path:
    _ensure_dirs()
    body = render_systemd_unit(root=ROOT, user=user)
    SYSTEMD_UNIT_GENERATED.write_text(body, encoding="utf-8")
    # Keep example template in sync for operators browsing config/.
    try:
        SYSTEMD_UNIT_EXAMPLE.write_text(body, encoding="utf-8")
    except Exception:
        pass
    return SYSTEMD_UNIT_GENERATED


def install_systemd_hint() -> str:
    unit = write_systemd_unit()
    return (
        f"# Wrote {unit}\n"
        f"# On VPS (requires sudo) — or: bash scripts/install_vod_loop_systemd.sh\n"
        f"#   sudo cp {unit} /etc/systemd/system/vod-loop@.service\n"
        f"#   sudo systemctl daemon-reload\n"
        f"#   sudo systemctl enable --now vod-loop@napstorian\n"
        f"#   sudo systemctl enable --now vod-loop@napping_historian\n"
        f"# Optional backup: python -m src.cli.vod_loop crontab --install\n"
        f"# After 48–72h green: retire StreamCast for production Live.\n"
    )


def smoke_without_rtmp() -> dict[str, Any]:
    """Ops smoke: bootstrap playlists + status + healthcheck skip path (no RTMP)."""
    _load_dotenv()
    boot = ensure_default_playlists()
    status = run_status(ensure_playlist=False)
    hc = healthcheck_all(dry_run=True)
    adaptive = hc.get("adaptive") or adaptive_bitrate_tick(dry_run=True)
    # Verify ffmpeg can read concat once (null mux, few seconds) if playlist ok.
    encode_smoke: dict[str, Any] = {}
    for ch in CHANNELS:
        enc = encode_settings(ch)
        ok, detail = _playlist_ok(ch)
        if not ok:
            encode_smoke[ch] = {"ok": False, "detail": detail}
            continue
        pl = _playlist_path(ch)
        out = OPS / f"vod_loop_smoke_{ch}.mp4"
        cmd = [
            _ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(pl),
            "-t",
            "2",
            *_video_audio_args(enc),
            "-movflags",
            "+faststart",
            str(out),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=90)
            encode_smoke[ch] = {
                "ok": True,
                "bytes": out.stat().st_size if out.is_file() else 0,
                "out": out.name,
                "bitrate": enc["bitrate"],
                "height": enc["height"],
                "fps": enc["fps"],
                "source": enc.get("source"),
            }
        except subprocess.CalledProcessError as exc:
            encode_smoke[ch] = {
                "ok": False,
                "detail": (exc.stderr or "")[-400:] or type(exc).__name__,
            }
        except Exception as exc:
            encode_smoke[ch] = {"ok": False, "detail": type(exc).__name__}

    # Dry-run cmd build must redact dest and still succeed structurally.
    dry_cmds: dict[str, Any] = {}
    for ch in CHANNELS:
        enc = encode_settings(ch)
        # Fake dest only for argv structure check — never written to status secrets.
        cmd = _build_ffmpeg_cmd(ch, "rtmp://example.invalid/live2/REDACTED", progress_file=_progress_path(ch))
        joined = " ".join(cmd)
        dry_cmds[ch] = {
            "argc": len(cmd),
            "has_maxrate": "-maxrate" in cmd,
            "has_bufsize": "-bufsize" in cmd,
            "has_progress": "-progress" in cmd,
            "bitrate_token": enc["bitrate"] in joined,
            "gop": str(enc["gop"]) in cmd,
        }

    enc_summary = encode_settings()
    payload = {
        "ok": all(v.get("ok") for v in encode_smoke.values()) if encode_smoke else False,
        "ts": _utc_now(),
        "module": "vod_loop",
        "action": "smoke_without_rtmp",
        "encode": {
            "bitrate": enc_summary["bitrate"],
            "height": enc_summary["height"],
            "fps": enc_summary["fps"],
            "gop": enc_summary["gop"],
            "audio_bitrate": enc_summary["audio_bitrate"],
            "source": enc_summary.get("source"),
            "adaptive": enc_summary.get("adaptive"),
        },
        "adaptive": adaptive,
        "playlist_bootstrap": boot,
        "status_summary": {
            ch: {
                "action": (hc.get("channels") or {}).get(ch, {}).get("action"),
                "rtmp_ready": (status.get("channels") or {}).get(ch, {}).get("rtmp_ready"),
                "playlist_ok": (status.get("channels") or {}).get(ch, {}).get("playlist_ok"),
            }
            for ch in CHANNELS
        },
        "encode_smoke": encode_smoke,
        "dry_cmd_checks": dry_cmds,
        "status_path": str(STATUS_PATH),
        "health_path": str(HEALTH_PATH),
        "policy_path": str(BITRATE_POLICY_PATH),
    }
    _atomic_write_json(OPS / "vod_loop_smoke.json", payload)
    _atomic_write_json(STATUS_PATH, {**status, "last_smoke": payload})
    return payload
