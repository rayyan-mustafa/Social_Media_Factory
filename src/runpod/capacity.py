"""RunPod pre-flight capacity benchmark (no pod create).

Benchmark scheme name: ``runpod_stills_benchmark_v1``

Probes stock via GraphQL ``gpuTypes.lowestPrice.stockStatus`` for the stills
GPU chain from settings (default: **NVIDIA A40 Secure only**) without creating
pods — avoids cold-start + weight download when the intended tier is empty.

Classification
--------------
- GREEN  — intended stills path has stock:
  - Community factory: ≥1 Community level High/Medium/Low
  - Secure-only factory (primary cloud SECURE, no Community in chain): ≥1
    Secure level in stock (A40 Secure counts as proceed, not YELLOW forever)
- YELLOW — mixed factory where only Secure levels have stock
- RED    — no probed level has stock

Decision
--------
- GREEN  → proceed (existing fallback chain / sole Secure candidate)
- YELLOW → defer by default; proceed Secure-only if ``RUNPOD_ALLOW_SECURE=1``
- RED    → skip/defer (clear message; no create)

Farm / factory gate
-------------------
When stills are locked to Secure-only (e.g. A40 Secure), farm GREEN-light
treats available Secure stock on the configured chain as proceed. Community
factories remain Community-GREEN-only (YELLOW still blocked for farm).

Schedule windows (soft by default)
----------------------------------
- Primary: 07:00–11:00 UTC (12:00–16:00 PKT)
- Backup:  03:00–06:00 UTC (08:00–11:00 PKT)
- Soft advice unless ``RUNPOD_REQUIRE_WINDOW=1``
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Literal

from src.runpod.client import RunPodClient, RunPodClientError
from src.runpod.cost_log import utcnow_iso
from src.services.settings import ROOT

logger = logging.getLogger(__name__)

SCHEME_NAME = "runpod_stills_benchmark_v1"
JSONL_PATH: Path = ROOT / "output" / "ops" / "runpod_capacity_benchmark.jsonl"
_LOCK = threading.Lock()

# Default when settings unavailable — matches A40 Secure-only factory lock.
DEFAULT_PROBE_LEVELS: tuple[tuple[str, str], ...] = (
    ("NVIDIA A40", "SECURE"),
)

Classification = Literal["GREEN", "YELLOW", "RED"]
Decision = Literal["proceed", "proceed_secure_only", "defer", "skip"]

_STOCK_AVAILABLE = frozenset({"HIGH", "MEDIUM", "LOW"})
_WINDOW_RE = re.compile(
    r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$"
)


class CapacityDeferError(RuntimeError):
    """Raised when the benchmark scheme decides not to create a pod."""

    def __init__(self, message: str, result: CapacityBenchmarkResult):
        super().__init__(message)
        self.result = result


@dataclass
class GpuLevelProbe:
    gpu_type_id: str
    cloud_type: str
    stock_status: str | None
    available: bool
    price_usd_hr: float | None = None
    error: str | None = None


@dataclass
class CapacityBenchmarkResult:
    scheme: str
    ts_utc: str
    classification: Classification
    decision: Decision
    allow_create: bool
    levels: list[GpuLevelProbe] = field(default_factory=list)
    message: str = ""
    in_schedule_window: bool = True
    schedule_window: str = ""
    schedule_advice: str = ""
    allow_secure: bool = False
    require_window: bool = False
    community_available: list[str] = field(default_factory=list)
    secure_available: list[str] = field(default_factory=list)
    # Continuous learning tags (seasonality)
    month_utc: int | None = None
    season: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def _env_truthy(name: str, default: str = "0") -> bool:
    raw = (os.getenv(name) or default).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _settings():
    try:
        from src.services.settings import get_settings

        return get_settings()
    except Exception:  # noqa: BLE001
        return None


def capacity_probe_enabled() -> bool:
    """Default ON — set RUNPOD_CAPACITY_PROBE=0 to skip the pre-flight."""
    if "RUNPOD_CAPACITY_PROBE" in os.environ:
        return _env_truthy("RUNPOD_CAPACITY_PROBE", "1")
    s = _settings()
    if s is not None:
        return bool(getattr(s, "runpod_capacity_probe", True))
    return True


def allow_secure() -> bool:
    if "RUNPOD_ALLOW_SECURE" in os.environ:
        return _env_truthy("RUNPOD_ALLOW_SECURE", "0")
    s = _settings()
    if s is not None:
        return bool(getattr(s, "runpod_allow_secure", False))
    return False


def stills_gpu_chain_from_settings() -> list[tuple[str, str]]:
    """Primary + fallbacks for stills (deduped). Falls back to DEFAULT_PROBE_LEVELS."""
    from src.runpod.lifecycle import parse_gpu_fallback_csv

    s = _settings()
    if s is None:
        return list(DEFAULT_PROBE_LEVELS)
    primary_g = str(getattr(s, "runpod_stills_gpu_type_id", "") or "").strip()
    primary_c = str(getattr(s, "runpod_stills_cloud_type", "SECURE") or "SECURE").strip().upper()
    raw_fb = getattr(s, "runpod_stills_gpu_fallbacks", "") or ""
    chain: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for gpu, cloud in [(primary_g, primary_c), *parse_gpu_fallback_csv(str(raw_fb))]:
        g = str(gpu or "").strip()
        c = str(cloud or "COMMUNITY").strip().upper()
        if not g:
            continue
        key = (g, c)
        if key in seen:
            continue
        seen.add(key)
        chain.append(key)
    return chain or list(DEFAULT_PROBE_LEVELS)


def stills_secure_only_mode() -> bool:
    """True when stills primary+fallbacks have no Community tiers (A40 Secure lock)."""
    chain = stills_gpu_chain_from_settings()
    if not chain:
        return True
    return all(c == "SECURE" for _, c in chain)


def stills_probe_levels() -> list[tuple[str, str]]:
    """Levels to probe for capacity — settings chain, else DEFAULT_PROBE_LEVELS."""
    return stills_gpu_chain_from_settings()


def require_window() -> bool:
    if "RUNPOD_REQUIRE_WINDOW" in os.environ:
        return _env_truthy("RUNPOD_REQUIRE_WINDOW", "0")
    s = _settings()
    if s is not None:
        return bool(getattr(s, "runpod_require_window", False))
    return False


def _parse_window(raw: str) -> tuple[time, time] | None:
    m = _WINDOW_RE.match((raw or "").strip())
    if not m:
        return None
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    try:
        return time(h1, m1), time(h2, m2)
    except ValueError:
        return None


def schedule_windows_utc() -> list[tuple[str, time, time]]:
    s = _settings()
    primary = (
        os.getenv("RUNPOD_PRIMARY_WINDOW_UTC")
        or (getattr(s, "runpod_primary_window_utc", None) if s else None)
        or "07:00-11:00"
    ).strip()
    backup = (
        os.getenv("RUNPOD_BACKUP_WINDOW_UTC")
        or (getattr(s, "runpod_backup_window_utc", None) if s else None)
        or "03:00-06:00"
    ).strip()
    # Optional: override from learned heatmap when auto-apply + enough samples.
    try:
        from src.runpod.learn import load_learned_windows

        learned = load_learned_windows()
        if learned:
            primary, backup = learned
    except Exception:  # noqa: BLE001
        pass
    out: list[tuple[str, time, time]] = []
    for label, raw in (("primary", primary), ("backup", backup)):
        parsed = _parse_window(raw)
        if parsed:
            out.append((label, parsed[0], parsed[1]))
    return out


def _in_half_open_window(now_t: time, start: time, end: time) -> bool:
    """Inclusive start, exclusive end (UTC clock times; no overnight wrap)."""
    return start <= now_t < end


def evaluate_schedule(
    *,
    now: datetime | None = None,
) -> tuple[bool, str, str]:
    """Return (in_window, window_label, advice)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    now_t = now.timetz().replace(tzinfo=None)
    windows = schedule_windows_utc()
    for label, start, end in windows:
        if _in_half_open_window(now_t, start, end):
            return (
                True,
                label,
                f"Inside {label} window "
                f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')} UTC",
            )
    parts = [
        f"{label} {s.strftime('%H:%M')}-{e.strftime('%H:%M')} UTC"
        for label, s, e in windows
    ]
    if stills_secure_only_mode():
        advice = (
            "Outside preferred RunPod windows "
            f"({'; '.join(parts) or 'none configured'}). "
            "A40 Secure-only factory: no GPU fallbacks — wait for next GREEN "
            "(primary 07:00–11:00 UTC or backup 03:00–06:00 UTC)."
        )
    else:
        advice = (
            "Outside preferred RunPod windows "
            f"({'; '.join(parts) or 'none configured'}). "
            "Prefer primary 07:00–11:00 UTC or backup 03:00–06:00 UTC "
            "to improve Community stock odds."
        )
    return False, "", advice


def stock_is_available(stock_status: str | None) -> bool:
    if stock_status is None:
        return False
    return str(stock_status).strip().upper() in _STOCK_AVAILABLE


def probe_gpu_level(
    client: RunPodClient,
    gpu_type_id: str,
    cloud_type: str,
) -> GpuLevelProbe:
    """Best-effort stock check via GraphQL — never creates a pod."""
    cloud = (cloud_type or "COMMUNITY").strip().upper()
    secure = cloud == "SECURE"
    query = """
    query($id: String!, $secure: Boolean!) {
      gpuTypes(input: { id: $id }) {
        id
        displayName
        lowestPrice(input: { gpuCount: 1, secureCloud: $secure }) {
          stockStatus
          uninterruptablePrice
          availableGpuCounts
        }
      }
    }
    """
    try:
        data = client.graphql(
            query, {"id": gpu_type_id, "secure": secure}
        )
        if data.get("errors"):
            return GpuLevelProbe(
                gpu_type_id=gpu_type_id,
                cloud_type=cloud,
                stock_status=None,
                available=False,
                error=str(data["errors"])[:400],
            )
        gts = (data.get("data") or {}).get("gpuTypes") or []
        if not gts:
            return GpuLevelProbe(
                gpu_type_id=gpu_type_id,
                cloud_type=cloud,
                stock_status="None",
                available=False,
                error="gpu type not found",
            )
        lp = gts[0].get("lowestPrice") or {}
        status = lp.get("stockStatus")
        # GraphQL returns null stockStatus when out of stock / no offer.
        if status is None:
            status = "None"
        price = lp.get("uninterruptablePrice")
        try:
            price_f = float(price) if price is not None else None
        except (TypeError, ValueError):
            price_f = None
        return GpuLevelProbe(
            gpu_type_id=gpu_type_id,
            cloud_type=cloud,
            stock_status=str(status),
            available=stock_is_available(str(status)),
            price_usd_hr=price_f,
        )
    except RunPodClientError as exc:
        return GpuLevelProbe(
            gpu_type_id=gpu_type_id,
            cloud_type=cloud,
            stock_status=None,
            available=False,
            error=str(exc)[:400],
        )
    except Exception as exc:  # noqa: BLE001
        return GpuLevelProbe(
            gpu_type_id=gpu_type_id,
            cloud_type=cloud,
            stock_status=None,
            available=False,
            error=str(exc)[:400],
        )


def classify_levels(
    levels: list[GpuLevelProbe],
    *,
    secure_only_factory: bool | None = None,
) -> tuple[Classification, list[str], list[str]]:
    """Classify stock.

    Secure-only factory (A40 Secure lock): Secure stock → GREEN so farm can
    proceed. Mixed Community factories keep YELLOW when only Secure has stock.
    """
    if secure_only_factory is None:
        secure_only_factory = stills_secure_only_mode()
    community: list[str] = []
    secure: list[str] = []
    for lv in levels:
        if not lv.available:
            continue
        label = f"{lv.gpu_type_id}:{lv.cloud_type}"
        if lv.cloud_type == "COMMUNITY":
            community.append(label)
        else:
            secure.append(label)
    if community:
        return "GREEN", community, secure
    if secure:
        if secure_only_factory:
            return "GREEN", community, secure
        return "YELLOW", community, secure
    return "RED", community, secure


def decide(
    classification: Classification,
    *,
    allow_secure_flag: bool,
    in_window: bool,
    require_window_flag: bool,
    secure_only_factory: bool | None = None,
) -> tuple[Decision, bool, str]:
    """Map classification (+ flags) → decision / allow_create / message."""
    if secure_only_factory is None:
        secure_only_factory = stills_secure_only_mode()
    if require_window_flag and not in_window:
        return (
            "defer",
            False,
            "RUNPOD_REQUIRE_WINDOW=1 and outside primary/backup UTC windows — deferring",
        )
    if classification == "GREEN":
        if secure_only_factory:
            return (
                "proceed",
                True,
                "GREEN: Secure-only stills path has stock (A40 Secure factory) — proceed",
            )
        return (
            "proceed",
            True,
            "GREEN: Community capacity available — proceed with stills GPU chain",
        )
    if classification == "YELLOW":
        if secure_only_factory:
            # Secure-only factories should classify Secure stock as GREEN;
            # treat residual YELLOW as defer — never invent GPU fallbacks.
            return (
                "defer",
                False,
                "YELLOW under Secure-only factory — defer; wait for next GREEN "
                "(A40 Secure stock); no GPU fallbacks",
            )
        if allow_secure_flag:
            return (
                "proceed_secure_only",
                True,
                "YELLOW: Community empty; RUNPOD_ALLOW_SECURE=1 — proceed Secure-only",
            )
        return (
            "defer",
            False,
            "YELLOW: only Secure stock — deferring (set RUNPOD_ALLOW_SECURE=1 to proceed Secure, "
            "or wait for Community / backup window)",
        )
    if secure_only_factory:
        return (
            "skip",
            False,
            "RED: A40 Secure out of stock — skip; no fallbacks; wait for next GREEN window "
            "(do not cold-start / do not retry creates)",
        )
    return (
        "skip",
        False,
        "RED: no Community or Secure stock on probed GPUs — skip/defer (do not cold-start)",
    )


def append_benchmark_jsonl(
    result: CapacityBenchmarkResult,
    *,
    path: Path | None = None,
) -> Path:
    dest = path or JSONL_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(result.to_dict(), ensure_ascii=False) + "\n"
    with _LOCK:
        with dest.open("a", encoding="utf-8") as fh:
            fh.write(line)
    return dest


def run_capacity_benchmark(
    *,
    client: RunPodClient | None = None,
    levels: list[tuple[str, str]] | None = None,
    log_jsonl: bool = True,
    jsonl_path: Path | None = None,
    now: datetime | None = None,
    allow_secure_flag: bool | None = None,
    require_window_flag: bool | None = None,
    secure_only_factory: bool | None = None,
) -> CapacityBenchmarkResult:
    """Execute ``runpod_stills_benchmark_v1`` — GraphQL only, zero pods."""
    probe_levels = list(levels if levels is not None else stills_probe_levels())
    secure_only = (
        stills_secure_only_mode()
        if secure_only_factory is None
        else secure_only_factory
    )
    allow_sec = allow_secure() if allow_secure_flag is None else allow_secure_flag
    req_win = require_window() if require_window_flag is None else require_window_flag
    in_window, window_label, schedule_advice = evaluate_schedule(now=now)

    rp = client or RunPodClient()
    probes: list[GpuLevelProbe] = []
    for gpu, cloud in probe_levels:
        probes.append(probe_gpu_level(rp, gpu, cloud))

    classification, community, secure = classify_levels(
        probes, secure_only_factory=secure_only
    )
    decision, allow_create, message = decide(
        classification,
        allow_secure_flag=allow_sec,
        in_window=in_window,
        require_window_flag=req_win,
        secure_only_factory=secure_only,
    )

    if not in_window and allow_create and not req_win:
        message = f"{message} | schedule soft-warn: {schedule_advice}"

    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    from src.runpod.learn import season_for_month

    result = CapacityBenchmarkResult(
        scheme=SCHEME_NAME,
        ts_utc=utcnow_iso(),
        classification=classification,
        decision=decision,
        allow_create=allow_create,
        levels=probes,
        message=message,
        in_schedule_window=in_window,
        schedule_window=window_label,
        schedule_advice=schedule_advice if not in_window else (
            f"Inside {window_label} window" if window_label else ""
        ),
        allow_secure=allow_sec,
        require_window=req_win,
        community_available=community,
        secure_available=secure,
        month_utc=now_utc.month,
        season=season_for_month(now_utc.month),
    )
    if log_jsonl:
        try:
            append_benchmark_jsonl(result, path=jsonl_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("capacity benchmark jsonl write failed: %s", exc)
    logger.info(
        "capacity benchmark %s classification=%s decision=%s allow_create=%s "
        "secure_only_factory=%s",
        SCHEME_NAME,
        classification,
        decision,
        allow_create,
        secure_only,
    )
    return result


def secure_only_chain(
    levels: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Filter probe/fallback chain to SECURE cloud only."""
    src = list(levels or DEFAULT_PROBE_LEVELS)
    return [(g, c) for g, c in src if str(c).upper() == "SECURE"]


def apply_secure_only_to_spec(spec: Any) -> Any:
    """Rewrite a PodSpec primary + fallbacks to Secure-only GPUs.

    Mutates and returns ``spec`` (PodSpec duck-typed).
    """
    chain = [(spec.gpu_type_id, spec.cloud_type), *list(spec.gpu_fallbacks or [])]
    secure = secure_only_chain(chain)
    if not secure:
        secure = secure_only_chain()
    if not secure:
        return spec
    primary_g, primary_c = secure[0]
    spec.gpu_type_id = primary_g
    spec.cloud_type = primary_c
    spec.gpu_fallbacks = secure[1:]
    return spec


def assert_capacity_allows_stills(
    *,
    client: RunPodClient | None = None,
    skip: bool = False,
    raise_on_defer: bool = True,
) -> CapacityBenchmarkResult | None:
    """Run benchmark before stills pod create.

    Returns result (or None if probe disabled/skipped). Raises
    ``CapacityDeferError`` when decision blocks create and ``raise_on_defer``.

    Note: one-off CLI may proceed on YELLOW if ``RUNPOD_ALLOW_SECURE=1``.
    **Farm / factory paths must use** ``assert_farm_capacity_green`` instead —
    factory is GREEN-only forever (YELLOW/RED never start production).
    """
    if skip or not capacity_probe_enabled():
        logger.info("capacity probe skipped (disabled or skip=True)")
        return None
    result = run_capacity_benchmark(client=client)
    if not result.allow_create and raise_on_defer:
        raise CapacityDeferError(result.message, result)
    return result


def farm_capacity_is_green(result: CapacityBenchmarkResult) -> bool:
    """Factory may start when classification is GREEN.

    Under A40 Secure-only settings, Secure stock is classified GREEN (see
    ``classify_levels``), so farms are not stuck on YELLOW forever.
    """
    return str(result.classification).upper() == "GREEN"


def assert_farm_capacity_green(
    *,
    client: RunPodClient | None = None,
    skip: bool = False,
    raise_on_defer: bool = True,
    log_jsonl: bool = True,
) -> CapacityBenchmarkResult | None:
    """Pre-flight for farm / sleep_beat / watchdog auto-start.

    **GREEN only.** For Community factories, YELLOW (Secure-only stock) never
    opens the factory. For Secure-only stills (A40 Secure lock), available
    Secure stock is classified GREEN and farms may proceed.

    ``RUNPOD_ALLOW_SECURE=1`` still gates one-off YELLOW on mixed factories;
    farm never treats mixed-factory YELLOW as green via that flag alone.
    """
    if skip:
        logger.info("farm capacity green-check skipped (skip=True)")
        return None
    secure_only = stills_secure_only_mode()
    # Farm always probes — ignore RUNPOD_CAPACITY_PROBE=0 so there is no
    # backdoor that skips the traffic light for production starts.
    result = run_capacity_benchmark(
        client=client,
        log_jsonl=log_jsonl,
        # Mixed Community factory: never treat YELLOW as proceed via allow_secure.
        # Secure-only factory: classification already GREEN when Secure in stock.
        allow_secure_flag=False if not secure_only else True,
        secure_only_factory=secure_only,
    )
    if farm_capacity_is_green(result):
        logger.info(
            "FARM GREEN LIGHT — classification=GREEN decision=%s secure_only=%s",
            result.decision,
            secure_only,
        )
        return result
    msg = (
        f"FARM BLOCKED (green-light only) — classification={result.classification} "
        f"decision={result.decision}. Factory never starts on YELLOW/RED. "
        f"{result.message}"
    )
    logger.warning(msg)
    if raise_on_defer:
        raise CapacityDeferError(msg, result)
    return result


def check_farm_capacity_green(
    *,
    client: RunPodClient | None = None,
    log_jsonl: bool = True,
) -> tuple[bool, str, CapacityBenchmarkResult | None]:
    """Non-raising GREEN-only gate for sleep_beat / watchdog / worker."""
    try:
        result = assert_farm_capacity_green(
            client=client,
            raise_on_defer=False,
            log_jsonl=log_jsonl,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"capacity probe failed: {exc}", None
    if result is None:
        return False, "capacity probe returned no result", None
    if farm_capacity_is_green(result):
        return True, result.message, result
    return (
        False,
        (
            f"not GREEN ({result.classification}) — farm blocked; "
            f"{result.message}"
        ),
        result,
    )
