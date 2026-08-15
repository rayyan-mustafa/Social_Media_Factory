"""Learn RunPod availability windows from our probe JSONL (continuous, not lore).

Philosophy (365-day continuous improvement)
------------------------------------------
Human GPU demand shifts with seasons, holidays, semester calendars, etc.
We keep a **rolling ~365 days** of GraphQL probes and recompute suggestions
every watchdog tick. Windows are **never** promoted from a single GREEN.

Mature-before-promote
---------------------
An hour (hour-of-day or hour-of-week) becomes eligible for an official
primary/backup window only when it has **repeated GREEN evidence**:

- ≥ ``RUNPOD_LEARN_MATURE_MIN_DAYS`` distinct UTC calendar days with GREEN
- ≥ ``RUNPOD_LEARN_MATURE_MIN_SAMPLES`` probes in that hour (across lookback)
- GREEN rate ≥ ``RUNPOD_LEARN_MATURE_MIN_GREEN_RATE``

Plus global gate: total samples ≥ ``RUNPOD_LEARN_MIN_SAMPLES`` and
``RUNPOD_LEARN_AUTO_APPLY=1`` before live schedule override.

Thin data → keep defaults (07:00–11:00 / 03:00–06:00 UTC).
Seasonality: probes tagged by month; when a month has mature coverage,
month-specific suggestions are preferred over all-year aggregate.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.runpod.cost_log import utcnow_iso
from src.services.settings import ROOT

logger = logging.getLogger(__name__)

JSONL_PATH: Path = ROOT / "output" / "ops" / "runpod_capacity_benchmark.jsonl"
HEATMAP_PATH: Path = ROOT / "output" / "ops" / "runpod_availability_heatmap.json"
LEARNED_WINDOWS_PATH: Path = ROOT / "output" / "ops" / "runpod_learned_windows.json"
ARCHIVE_DIR: Path = ROOT / "output" / "ops" / "capacity_archive"
_LOCK = threading.Lock()

_DOW_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_DEFAULT_PRIMARY = "07:00-11:00"
_DEFAULT_BACKUP = "03:00-06:00"

# Meteorological seasons (UTC month).
_SEASON_BY_MONTH = {
    12: "DJF",
    1: "DJF",
    2: "DJF",
    3: "MAM",
    4: "MAM",
    5: "MAM",
    6: "JJA",
    7: "JJA",
    8: "JJA",
    9: "SON",
    10: "SON",
    11: "SON",
}


def _env_truthy(name: str, default: str = "0") -> bool:
    raw = (os.getenv(name) or default).strip().lower()
    return raw in {"1", "true", "yes", "on"}


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


def learn_min_samples() -> int:
    """Global minimum probes in lookback before any live window override."""
    return max(1, _env_int("RUNPOD_LEARN_MIN_SAMPLES", 50))


def learn_lookback_days() -> int:
    """Rolling history for seasonal signal (default 365 continuous days)."""
    return max(1, _env_int("RUNPOD_LEARN_LOOKBACK_DAYS", 365))


def learn_auto_apply() -> bool:
    return _env_truthy("RUNPOD_LEARN_AUTO_APPLY", "0")


def mature_min_days() -> int:
    """Distinct UTC days with GREEN required before an hour is promotable."""
    return max(2, _env_int("RUNPOD_LEARN_MATURE_MIN_DAYS", 5))


def mature_min_samples() -> int:
    """Minimum probes in that hour-of-day before promotable."""
    return max(2, _env_int("RUNPOD_LEARN_MATURE_MIN_SAMPLES", 8))


def mature_min_green_rate() -> float:
    return min(1.0, max(0.0, _env_float("RUNPOD_LEARN_MATURE_MIN_GREEN_RATE", 0.35)))


def primary_window_hours() -> int:
    return max(1, min(12, _env_int("RUNPOD_LEARN_PRIMARY_HOURS", 4)))


def backup_window_hours() -> int:
    return max(1, min(12, _env_int("RUNPOD_LEARN_BACKUP_HOURS", 3)))


def season_for_month(month: int) -> str:
    return _SEASON_BY_MONTH.get(int(month), "UNK")


@dataclass
class HourBucket:
    green: int = 0
    yellow: int = 0
    red: int = 0
    total: int = 0
    # Distinct UTC dates (YYYY-MM-DD) that saw ≥1 GREEN in this bucket.
    green_dates: set[str] = field(default_factory=set)
    sample_dates: set[str] = field(default_factory=set)
    by_gpu: dict[str, dict[str, int]] = field(default_factory=dict)

    def green_rate(self) -> float:
        return (self.green / self.total) if self.total else 0.0

    def green_day_count(self) -> int:
        return len(self.green_dates)

    def is_mature(
        self,
        *,
        min_days: int | None = None,
        min_samples: int | None = None,
        min_green_rate: float | None = None,
    ) -> bool:
        """Mature-before-promote: repeated GREEN pattern, not one-shot luck."""
        md = mature_min_days() if min_days is None else min_days
        ms = mature_min_samples() if min_samples is None else min_samples
        mgr = (
            mature_min_green_rate()
            if min_green_rate is None
            else min_green_rate
        )
        if self.total < ms:
            return False
        if self.green_day_count() < md:
            return False
        if self.green_rate() < mgr:
            return False
        return True

    def maturity_report(
        self,
        *,
        min_days: int | None = None,
        min_samples: int | None = None,
        min_green_rate: float | None = None,
    ) -> dict[str, Any]:
        md = mature_min_days() if min_days is None else min_days
        ms = mature_min_samples() if min_samples is None else min_samples
        mgr = (
            mature_min_green_rate()
            if min_green_rate is None
            else min_green_rate
        )
        return {
            "mature": self.is_mature(
                min_days=md, min_samples=ms, min_green_rate=mgr
            ),
            "green_days": self.green_day_count(),
            "need_green_days": md,
            "samples": self.total,
            "need_samples": ms,
            "green_rate": round(self.green_rate(), 4),
            "need_green_rate": mgr,
        }


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_benchmark_rows(
    *,
    path: Path | None = None,
    lookback_days: int | None = None,
    now: datetime | None = None,
    month: int | None = None,
    season: str | None = None,
) -> list[dict[str, Any]]:
    """Load JSONL probe rows within lookback window (optional month/season filter)."""
    src = path or JSONL_PATH
    if not src.exists():
        return []
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    days = lookback_days if lookback_days is not None else learn_lookback_days()
    cutoff = now - timedelta(days=days)
    rows: list[dict[str, Any]] = []
    with src.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_ts(row.get("ts_utc"))
            if ts is None or ts < cutoff:
                continue
            row_month = int(row.get("month_utc") or ts.month)
            row_season = str(row.get("season") or season_for_month(row_month))
            if month is not None and row_month != int(month):
                continue
            if season is not None and row_season != season:
                continue
            # Normalize tags for downstream
            row = dict(row)
            row["month_utc"] = row_month
            row["season"] = row_season
            rows.append(row)
    return rows


def _ensure_gpu_bucket(bucket: HourBucket, gpu_key: str) -> dict[str, int]:
    if gpu_key not in bucket.by_gpu:
        bucket.by_gpu[gpu_key] = {
            "green": 0,
            "yellow": 0,
            "red": 0,
            "total": 0,
            "available": 0,
        }
    return bucket.by_gpu[gpu_key]


def build_heatmap(
    rows: list[dict[str, Any]],
) -> dict[str, HourBucket]:
    """Key = ``dow:hour`` (dow 0=Mon … 6=Sun, hour 0–23 UTC)."""
    buckets: dict[str, HourBucket] = defaultdict(HourBucket)
    for row in rows:
        ts = _parse_ts(row.get("ts_utc"))
        if ts is None:
            continue
        key = f"{ts.weekday()}:{ts.hour:02d}"
        b = buckets[key]
        day = ts.strftime("%Y-%m-%d")
        cls = str(row.get("classification") or "").upper()
        b.total += 1
        b.sample_dates.add(day)
        if cls == "GREEN":
            b.green += 1
            b.green_dates.add(day)
        elif cls == "YELLOW":
            b.yellow += 1
        else:
            b.red += 1

        for lv in row.get("levels") or []:
            if not isinstance(lv, dict):
                continue
            gpu = str(lv.get("gpu_type_id") or "?")
            cloud = str(lv.get("cloud_type") or "?").upper()
            gkey = f"{gpu}:{cloud}"
            gb = _ensure_gpu_bucket(b, gkey)
            gb["total"] += 1
            avail = bool(lv.get("available"))
            if avail:
                gb["available"] += 1
            if cloud == "COMMUNITY" and avail:
                gb["green"] += 1
            elif cloud == "SECURE" and avail:
                gb["yellow"] += 1
            else:
                gb["red"] += 1
    return dict(buckets)


def aggregate_hour_of_day(
    heatmap: dict[str, HourBucket],
) -> list[HourBucket]:
    """Collapse dow×hour into 24 hour-of-day buckets (merge green_dates)."""
    by_hour: list[HourBucket] = [HourBucket() for _ in range(24)]
    for key, bucket in heatmap.items():
        try:
            _dow_s, hour_s = key.split(":", 1)
            hour = int(hour_s)
        except ValueError:
            continue
        if not 0 <= hour <= 23:
            continue
        dest = by_hour[hour]
        dest.green += bucket.green
        dest.yellow += bucket.yellow
        dest.red += bucket.red
        dest.total += bucket.total
        dest.green_dates |= bucket.green_dates
        dest.sample_dates |= bucket.sample_dates
    return by_hour


def _hour_score_mature(bucket: HourBucket) -> float:
    """Score only mature hours; immature → -1 (excluded from promotion)."""
    if not bucket.is_mature():
        return -1.0
    # Prefer higher GREEN rate; slight boost for more green-days.
    rate = bucket.green_rate()
    day_boost = min(0.15, 0.01 * bucket.green_day_count())
    return rate + day_boost


def _best_contiguous_block(
    hour_scores: list[float],
    width: int,
    *,
    avoid: set[int] | None = None,
    require_all_scored: bool = True,
) -> tuple[int, float] | None:
    """Best contiguous block; by default every hour in the block must be mature."""
    if width < 1 or width > 24:
        return None
    avoid = avoid or set()
    scores = [(-1.0 if h in avoid else hour_scores[h]) for h in range(24)]
    best: tuple[int, float] | None = None
    for start in range(24):
        block = [scores[(start + i) % 24] for i in range(width)]
        if require_all_scored and any(s < 0 for s in block):
            continue
        if all(s < 0 for s in block):
            continue
        usable = [max(0.0, s) for s in block]
        mean = sum(usable) / width
        if best is None or mean > best[1]:
            best = (start, mean)
    return best


def _format_window(start_h: int, width: int) -> str:
    end_h = (start_h + width) % 24
    return f"{start_h:02d}:00-{end_h:02d}:00"


def _window_spec(
    start: int,
    width: int,
    mean: float,
    by_hour: list[HourBucket],
) -> dict[str, Any]:
    hours_meta = {}
    all_mature = True
    for i in range(width):
        h = (start + i) % 24
        b = by_hour[h]
        mat = b.maturity_report()
        all_mature = all_mature and bool(mat["mature"])
        hours_meta[str(h)] = {
            "green": b.green,
            "yellow": b.yellow,
            "red": b.red,
            "total": b.total,
            "green_rate": round(b.green_rate(), 4),
            "green_days": b.green_day_count(),
            "maturity": mat,
        }
    return {
        "window_utc": _format_window(start, width),
        "start_hour_utc": start,
        "hours": width,
        "mean_green_score": round(mean, 4),
        "mature": all_mature,
        "hour_totals": hours_meta,
    }


def suggest_windows(
    heatmap: dict[str, HourBucket],
    *,
    primary_hours: int | None = None,
    backup_hours: int | None = None,
    require_mature: bool = True,
) -> dict[str, Any]:
    """Suggest primary/backup UTC windows from mature hour-of-day evidence.

    Immature hours (single GREEN day, thin samples, low rate) are **excluded**
    from promotion candidates. Exploratory raw scores are still reported.
    """
    ph = primary_hours if primary_hours is not None else primary_window_hours()
    bh = backup_hours if backup_hours is not None else backup_window_hours()
    by_hour = aggregate_hour_of_day(heatmap)

    mature_scores = [_hour_score_mature(by_hour[h]) for h in range(24)]
    # Exploratory (non-promotable) scores for ops visibility only
    exploratory = [
        (
            (by_hour[h].green + 0.5) / (by_hour[h].total + 1.0)
            if by_hour[h].total
            else -1.0
        )
        for h in range(24)
    ]

    primary = _best_contiguous_block(
        mature_scores, ph, require_all_scored=require_mature
    )
    avoid: set[int] = set()
    primary_spec: dict[str, Any] | None = None
    if primary:
        start, mean = primary
        avoid = {(start + i) % 24 for i in range(ph)}
        primary_spec = _window_spec(start, ph, mean, by_hour)

    backup = _best_contiguous_block(
        mature_scores, bh, avoid=avoid, require_all_scored=require_mature
    )
    backup_spec: dict[str, Any] | None = None
    if backup:
        start, mean = backup
        backup_spec = _window_spec(start, bh, mean, by_hour)

    # Soft exploratory suggestion (never auto-applied alone)
    explor_primary = _best_contiguous_block(
        exploratory, ph, require_all_scored=False
    )
    exploratory_primary = None
    if explor_primary:
        exploratory_primary = {
            "window_utc": _format_window(explor_primary[0], ph),
            "mean_score": round(explor_primary[1], 4),
            "note": "exploratory only — NOT promotable until maturity gates pass",
        }

    hour_summary = {}
    for h in range(24):
        if by_hour[h].total <= 0:
            continue
        hour_summary[f"{h:02d}"] = {
            "green": by_hour[h].green,
            "yellow": by_hour[h].yellow,
            "red": by_hour[h].red,
            "total": by_hour[h].total,
            "green_rate": round(by_hour[h].green_rate(), 4),
            "green_days": by_hour[h].green_day_count(),
            "mature": by_hour[h].is_mature(),
            "maturity": by_hour[h].maturity_report(),
            "mature_score": (
                round(mature_scores[h], 4) if mature_scores[h] >= 0 else None
            ),
        }

    return {
        "primary": primary_spec,
        "backup": backup_spec,
        "exploratory_primary": exploratory_primary,
        "by_hour_utc": hour_summary,
        "maturity_gates": {
            "min_green_days": mature_min_days(),
            "min_samples_per_hour": mature_min_samples(),
            "min_green_rate": mature_min_green_rate(),
            "rule": (
                "Do not promote a best hour from a single GREEN. "
                "Require repeated GREEN across multiple distinct UTC days "
                "plus sample count and green-rate floors."
            ),
        },
        "defaults_if_insufficient": {
            "primary": _DEFAULT_PRIMARY,
            "backup": _DEFAULT_BACKUP,
        },
    }


def _serialize_heatmap(heatmap: dict[str, HourBucket]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in sorted(heatmap.keys(), key=lambda k: (int(k.split(":")[0]), k)):
        b = heatmap[key]
        dow_i = int(key.split(":")[0])
        out[key] = {
            "dow": dow_i,
            "dow_name": _DOW_NAMES[dow_i] if 0 <= dow_i < 7 else "?",
            "hour_utc": int(key.split(":")[1]),
            "green": b.green,
            "yellow": b.yellow,
            "red": b.red,
            "total": b.total,
            "green_rate": round(b.green_rate(), 4),
            "green_days": b.green_day_count(),
            "mature": b.is_mature(),
            "maturity": b.maturity_report(),
            "by_gpu": b.by_gpu,
        }
    return out


def rotate_jsonl_if_needed(
    *,
    path: Path | None = None,
    keep_days: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Archive JSONL lines older than keep_days (default lookback) to gzip.

    Retains seasonal signal in archived yearly files; active JSONL stays lean.
    """
    src = path or JSONL_PATH
    days = keep_days if keep_days is not None else learn_lookback_days()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(days=days)
    if not src.exists():
        return {"rotated": False, "reason": "missing"}

    keep_lines: list[str] = []
    archive_by_year: dict[str, list[str]] = defaultdict(list)
    total = 0
    with src.open("r", encoding="utf-8") as fh:
        for line in fh:
            raw = line.strip()
            if not raw:
                continue
            total += 1
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                keep_lines.append(raw)
                continue
            ts = _parse_ts(row.get("ts_utc"))
            if ts is None or ts >= cutoff:
                keep_lines.append(raw)
            else:
                archive_by_year[str(ts.year)].append(raw)

    archived = sum(len(v) for v in archive_by_year.values())
    if archived == 0:
        return {"rotated": False, "total": total, "archived": 0}

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        for year, lines in archive_by_year.items():
            dest = ARCHIVE_DIR / f"runpod_capacity_benchmark_{year}.jsonl.gz"
            mode = "ab" if dest.exists() else "wb"
            with gzip.open(dest, mode) as gz:
                for ln in lines:
                    gz.write((ln + "\n").encode("utf-8"))
        src.write_text(
            ("\n".join(keep_lines) + ("\n" if keep_lines else "")),
            encoding="utf-8",
        )
    logger.info(
        "capacity jsonl rotate: kept=%s archived=%s cutoff=%s",
        len(keep_lines),
        archived,
        cutoff.date(),
    )
    return {
        "rotated": True,
        "total": total,
        "kept": len(keep_lines),
        "archived": archived,
        "cutoff_utc": cutoff.isoformat(),
    }


def recompute_availability_learn(
    *,
    jsonl_path: Path | None = None,
    heatmap_path: Path | None = None,
    learned_path: Path | None = None,
    lookback_days: int | None = None,
    now: datetime | None = None,
    write: bool = True,
    rotate: bool = True,
) -> dict[str, Any]:
    """Rebuild heatmap + mature suggested windows (continuous 365d process)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    rotate_info: dict[str, Any] | None = None
    if rotate:
        try:
            rotate_info = rotate_jsonl_if_needed(
                path=jsonl_path, keep_days=lookback_days, now=now
            )
        except Exception as exc:  # noqa: BLE001
            rotate_info = {"rotated": False, "error": str(exc)[:200]}

    lb = lookback_days if lookback_days is not None else learn_lookback_days()
    rows_all = load_benchmark_rows(
        path=jsonl_path, lookback_days=lb, now=now
    )
    heatmap = build_heatmap(rows_all)
    suggestions = suggest_windows(heatmap)

    # Month / season overlays (prefer when mature coverage exists)
    cur_month = now.month
    cur_season = season_for_month(cur_month)
    rows_month = load_benchmark_rows(
        path=jsonl_path, lookback_days=lb, now=now, month=cur_month
    )
    rows_season = load_benchmark_rows(
        path=jsonl_path, lookback_days=lb, now=now, season=cur_season
    )
    month_suggestions = suggest_windows(build_heatmap(rows_month))
    season_suggestions = suggest_windows(build_heatmap(rows_season))

    # Prefer month → season → all-year for promotable windows
    chosen_scope = "all_year"
    chosen = suggestions
    if (month_suggestions.get("primary") or {}).get("mature") and len(
        rows_month
    ) >= max(20, learn_min_samples() // 2):
        chosen = month_suggestions
        chosen_scope = f"month_{cur_month:02d}"
    elif (season_suggestions.get("primary") or {}).get("mature") and len(
        rows_season
    ) >= max(30, learn_min_samples() // 2):
        chosen = season_suggestions
        chosen_scope = f"season_{cur_season}"

    min_samples = learn_min_samples()
    n = len(rows_all)
    enough_global = n >= min_samples
    primary_mature = bool((chosen.get("primary") or {}).get("mature"))
    backup_ok = chosen.get("backup") is None or bool(
        (chosen.get("backup") or {}).get("mature")
    )
    promotable = bool(
        enough_global and primary_mature and backup_ok and chosen.get("primary")
    )
    auto = learn_auto_apply()
    applied = bool(promotable and auto)

    primary_win = (
        (chosen.get("primary") or {}).get("window_utc")
        if promotable
        else None
    ) or _DEFAULT_PRIMARY
    backup_win = (
        (chosen.get("backup") or {}).get("window_utc")
        if promotable
        else None
    ) or _DEFAULT_BACKUP

    payload: dict[str, Any] = {
        "scheme": "runpod_stills_benchmark_v1",
        "updated_at_utc": utcnow_iso(),
        "philosophy": (
            "Continuous 365-day learning from OUR probes. "
            "Mature-before-promote: never promote a window from one GREEN. "
            "Seasonality via month/season tags once enough mature data exists."
        ),
        "lookback_days": lb,
        "sample_count": n,
        "sample_count_month": len(rows_month),
        "sample_count_season": len(rows_season),
        "current_month_utc": cur_month,
        "current_season": cur_season,
        "chosen_scope": chosen_scope,
        "min_samples_to_override": min_samples,
        "enough_samples": enough_global,
        "promotable": promotable,
        "primary_mature": primary_mature,
        "learn_auto_apply_env": auto,
        "windows_applied_to_schedule": applied,
        "maturity_gates": suggestions.get("maturity_gates"),
        "rotate": rotate_info,
        "note": (
            "Heatmap always updates. Live schedule overrides defaults only when: "
            "(1) sample_count ≥ min_samples, (2) primary window hours are mature "
            f"(≥{mature_min_days()} GREEN days, ≥{mature_min_samples()} samples/hour, "
            f"green_rate≥{mature_min_green_rate()}), "
            "(3) RUNPOD_LEARN_AUTO_APPLY=1. "
            "Exploratory suggestions are informational only."
        ),
        "suggested_primary_window_utc": (
            (chosen.get("primary") or {}).get("window_utc")
        ),
        "suggested_backup_window_utc": (
            (chosen.get("backup") or {}).get("window_utc")
        ),
        "exploratory_primary_window_utc": (
            (suggestions.get("exploratory_primary") or {}).get("window_utc")
        ),
        "effective_primary_window_utc": primary_win
        if applied
        else (os.getenv("RUNPOD_PRIMARY_WINDOW_UTC") or _DEFAULT_PRIMARY),
        "effective_backup_window_utc": backup_win
        if applied
        else (os.getenv("RUNPOD_BACKUP_WINDOW_UTC") or _DEFAULT_BACKUP),
        "suggestions": chosen,
        "suggestions_all_year": suggestions,
        "suggestions_month": month_suggestions,
        "suggestions_season": season_suggestions,
        "heatmap_hour_of_week": _serialize_heatmap(heatmap),
    }

    if write:
        dest = heatmap_path or HEATMAP_PATH
        dest.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            dest.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            if applied:
                learned = {
                    "updated_at_utc": payload["updated_at_utc"],
                    "sample_count": n,
                    "min_samples_to_override": min_samples,
                    "primary_window_utc": primary_win,
                    "backup_window_utc": backup_win,
                    "scope": chosen_scope,
                    "mature": True,
                    "maturity_gates": payload["maturity_gates"],
                    "source": str(dest),
                    "auto_applied": True,
                }
                lp = learned_path or LEARNED_WINDOWS_PATH
                lp.write_text(
                    json.dumps(learned, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
        logger.info(
            "availability learn: samples=%s promotable=%s applied=%s scope=%s → %s",
            n,
            promotable,
            applied,
            chosen_scope,
            dest,
        )
    return payload


def load_learned_windows(
    *,
    path: Path | None = None,
) -> tuple[str, str] | None:
    """Return (primary, backup) if auto-applied mature learned file is valid."""
    if not learn_auto_apply():
        return None
    src = path or LEARNED_WINDOWS_PATH
    if not src.exists():
        return None
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not data.get("auto_applied"):
        return None
    if not data.get("mature", True):
        return None
    if int(data.get("sample_count") or 0) < learn_min_samples():
        return None
    primary = str(data.get("primary_window_utc") or "").strip()
    backup = str(data.get("backup_window_utc") or "").strip()
    if not primary or not backup:
        return None
    return primary, backup
