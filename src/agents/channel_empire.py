"""Multi-channel niche empire registry + expansion / revenue gates.

Reads ``config/channel_empire.json``. Core production stays napstorian +
napping_historian until harden bars pass; Wave 1/2 skins are staged
(prompts + competitors ready) with ``production_enabled: false``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.services.settings import CONFIG_DIR, ROOT

logger = logging.getLogger(__name__)

EMPIRE_PATH = CONFIG_DIR / "channel_empire.json"
NETWORK_REVENUE_PATH = CONFIG_DIR / "network_revenue.json"
OPS_GATE_PATH = ROOT / "output" / "ops" / "channel_expansion_gate.json"
HARDEN_STATUS_PATH = ROOT / "output" / "ops" / "core_harden_status.json"


def load_empire() -> dict[str, Any]:
    if not EMPIRE_PATH.is_file():
        return {}
    try:
        data = json.loads(EMPIRE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("channel_empire load failed: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def all_known_channel_names() -> list[str]:
    """Every channel registered in the empire (production + staged)."""
    emp = load_empire()
    chans = emp.get("channels") or {}
    if isinstance(chans, dict) and chans:
        return list(chans.keys())
    return ["napstorian", "napping_historian"]


def production_channel_names() -> list[str]:
    """Channels allowed in live sheet harvest / farm pick."""
    emp = load_empire()
    chans = emp.get("channels") or {}
    out: list[str] = []
    if isinstance(chans, dict):
        for name, entry in chans.items():
            if isinstance(entry, dict) and entry.get("production_enabled", False):
                out.append(str(name))
    if not out:
        return ["napstorian", "napping_historian"]
    return out


def staged_channel_names(*, wave: int | None = None) -> list[str]:
    emp = load_empire()
    chans = emp.get("channels") or {}
    out: list[str] = []
    if not isinstance(chans, dict):
        return out
    for name, entry in chans.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("production_enabled"):
            continue
        if wave is not None and int(entry.get("wave") or 0) != int(wave):
            continue
        out.append(str(name))
    return out


def channel_entry(channel: str) -> dict[str, Any]:
    emp = load_empire()
    chans = emp.get("channels") or {}
    if not isinstance(chans, dict):
        return {}
    entry = chans.get((channel or "").strip()) or {}
    return entry if isinstance(entry, dict) else {}


def core_harden_cfg() -> dict[str, Any]:
    emp = load_empire()
    cfg = emp.get("core_harden") or {}
    return cfg if isinstance(cfg, dict) else {}


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _ceo_goals() -> dict[str, Any]:
    return _load_json(ROOT / "output" / "ops" / "ceo_channel_goals.json")


def _benchmarks() -> dict[str, Any]:
    return _load_json(ROOT / "output" / "ops" / "benchmarks.json")


def _recent_archival_pass_rate(limit_jobs: int = 12) -> dict[str, Any]:
    """Scan recent job vision_judge stamps for archival pass rate."""
    jobs_root = ROOT / "output" / "jobs"
    if not jobs_root.is_dir():
        return {"ok": False, "reason": "no_jobs_dir", "pass_rate": 0.0, "n_jobs": 0}
    stamps: list[Path] = []
    try:
        for job in sorted(jobs_root.iterdir(), key=lambda p: p.name, reverse=True):
            stamp = job / "assets" / "fetched" / "vision_judge.json"
            if stamp.is_file():
                stamps.append(stamp)
            if len(stamps) >= limit_jobs:
                break
    except OSError:
        return {"ok": False, "reason": "jobs_scan_failed", "pass_rate": 0.0, "n_jobs": 0}

    n_scenes = 0
    n_pass = 0
    n_policy = 0
    for stamp in stamps:
        data = _load_json(stamp)
        if data.get("policy") == "per_scene_wiki_met_primary_flux_on_reject":
            n_policy += 1
        try:
            n_scenes += int(data.get("n_scenes") or 0)
            n_pass += int(data.get("n_pass") or 0)
        except (TypeError, ValueError):
            continue
    rate = (n_pass / n_scenes) if n_scenes else 0.0
    return {
        "ok": True,
        "pass_rate": round(rate, 4),
        "n_pass": n_pass,
        "n_scenes": n_scenes,
        "n_jobs_scanned": len(stamps),
        "n_rmagine_policy_jobs": n_policy,
    }


def _channel_metric_snapshot(channel: str) -> dict[str, Any]:
    """Best-effort CTR/AVD/first-60s from goals + benchmarks."""
    harden = core_harden_cfg()
    goals = _ceo_goals()
    ch_goals = ((goals.get("channels") or {}).get(channel) or {}) if goals else {}
    benches = _benchmarks()
    by = (benches.get("channel_winners_by_channel") or {}).get(channel) or {}
    # Prefer explicit live scorecard if present
    scorecard = _load_json(ROOT / "output" / "ops" / "daily_scorecard.json")
    sc_ch = ((scorecard.get("channels") or {}).get(channel) or {}) if scorecard else {}

    def _f(d: dict, *keys: str, default: float | None = None) -> float | None:
        for k in keys:
            if k in d and d[k] is not None:
                try:
                    return float(d[k])
                except (TypeError, ValueError):
                    continue
        return default

    ctr = _f(sc_ch, "ctr_pct", "ctr") or _f(by, "ctr_pct", "median_ctr_pct")
    avd = _f(sc_ch, "avd_pct", "avd") or _f(by, "avd_pct", "median_avd_pct")
    first60 = _f(sc_ch, "first_60s_retention_pct", "first_60s_pct") or _f(
        by, "first_60s_retention_pct", "median_first_60s_pct"
    )

    ctr_min = float(harden.get("ctr_pct_min") or ch_goals.get("ctr_pct_min") or 4.0)
    avd_min = float(harden.get("avd_pct_min") or ch_goals.get("avd_pct_min") or 40.0)
    f60_min = float(
        harden.get("first_60s_retention_pct_min")
        or ch_goals.get("first_60s_retention_pct_min")
        or 70.0
    )

    # Missing metrics → not green yet (must wait for scorecard / reporting).
    missing = [k for k, v in (("ctr", ctr), ("avd", avd), ("first_60s", first60)) if v is None]
    bars_ok = (
        not missing
        and ctr is not None
        and avd is not None
        and first60 is not None
        and ctr >= ctr_min
        and avd >= avd_min
        and first60 >= f60_min
    )
    return {
        "channel": channel,
        "ctr_pct": ctr,
        "avd_pct": avd,
        "first_60s_retention_pct": first60,
        "ctr_pct_min": ctr_min,
        "avd_pct_min": avd_min,
        "first_60s_retention_pct_min": f60_min,
        "videos_per_month_locked": int(harden.get("videos_per_month_locked") or 15),
        "missing_metrics": missing,
        "bars_ok": bars_ok,
        "volume_raise_allowed": False,  # locked until Rayyan + bars; never auto-raise
    }


def evaluate_core_harden() -> dict[str, Any]:
    """Whether both core channels may unlock Wave 1 expansion."""
    harden = core_harden_cfg()
    core = list(harden.get("channels") or ["napstorian", "napping_historian"])
    archival_min = float(
        (load_empire().get("visual_policy") or {}).get("archival_majority_min") or 0.51
    )
    archival = _recent_archival_pass_rate()
    archival_ok = bool(archival.get("ok")) and float(archival.get("pass_rate") or 0) >= archival_min

    per: dict[str, Any] = {}
    all_bars = True
    for ch in core:
        snap = _channel_metric_snapshot(ch)
        per[ch] = snap
        if not snap.get("bars_ok"):
            all_bars = False

    ready = bool(all_bars and archival_ok)
    blockers: list[str] = []
    if not all_bars:
        for ch, snap in per.items():
            if snap.get("missing_metrics"):
                blockers.append(f"{ch}:missing:{','.join(snap['missing_metrics'])}")
            elif not snap.get("bars_ok"):
                blockers.append(f"{ch}:below_bars")
    if not archival_ok:
        blockers.append(
            f"archival_pass_rate<{archival_min} (have {archival.get('pass_rate')})"
        )

    report = {
        "ok": True,
        "ready_for_wave1": ready,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "core_channels": core,
        "per_channel": per,
        "archival": archival,
        "archival_majority_min": archival_min,
        "archival_ok": archival_ok,
        "videos_per_month_locked": int(harden.get("videos_per_month_locked") or 15),
        "do_not_raise_volume_until_bars": bool(
            harden.get("do_not_raise_volume_until_bars", True)
        ),
        "blockers": blockers,
        "rmagine_policy": "per_scene_wiki_met_primary_flux_on_reject",
    }
    try:
        HARDEN_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        HARDEN_STATUS_PATH.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("harden status write failed: %s", exc)
    return report


def evaluate_expansion_gate(channel: str | None = None) -> dict[str, Any]:
    """Gate for activating a staged Wave 1/2 channel into production."""
    harden = evaluate_core_harden()
    emp = load_empire()
    order = list(emp.get("activation_order") or [])
    target = (channel or "").strip() or (order[0] if order else "")
    entry = channel_entry(target) if target else {}
    wave = int(entry.get("wave") or 0) if entry else 0

    reasons: list[str] = []
    allowed = True
    if not target:
        allowed = False
        reasons.append("no_target_channel")
    if entry.get("production_enabled"):
        reasons.append("already_production_enabled")
    if not harden.get("ready_for_wave1"):
        allowed = False
        reasons.extend(harden.get("blockers") or ["core_harden_not_ready"])
    if wave >= 2:
        # Wave 2 needs at least one Wave 1 channel monetized (or production).
        w1 = staged_channel_names(wave=1)
        w1_prod = [
            n
            for n, e in (emp.get("channels") or {}).items()
            if isinstance(e, dict) and int(e.get("wave") or 0) == 1 and e.get("production_enabled")
        ]
        monetized = _monetized_channel_count()
        if not w1_prod and monetized < 3:
            allowed = False
            reasons.append("wave2_requires_wave1_activated_or_3_monetized")

    # Skin readiness: prompts + competitors + seeds must exist
    skin = check_channel_skin_ready(target) if target else {"ready": False}
    if not skin.get("ready"):
        allowed = False
        reasons.extend(skin.get("missing") or ["skin_incomplete"])

    report = {
        "ok": True,
        "channel": target,
        "wave": wave,
        "allowed": allowed and bool(skin.get("ready")),
        "reasons": reasons,
        "harden": {
            "ready_for_wave1": harden.get("ready_for_wave1"),
            "blockers": harden.get("blockers"),
            "archival_ok": harden.get("archival_ok"),
        },
        "skin": skin,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        OPS_GATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        OPS_GATE_PATH.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("expansion gate write failed: %s", exc)
    return report


def check_channel_skin_ready(channel: str) -> dict[str, Any]:
    entry = channel_entry(channel)
    missing: list[str] = []
    prompts = entry.get("prompts_dir") or f"config/prompts/{channel}"
    prompts_path = ROOT / prompts
    for fname in ("outline.txt", "chapter_expand.txt", "hook_cold_open.txt", "thumbnail_template.txt"):
        if not (prompts_path / fname).is_file():
            missing.append(f"prompt:{fname}")
    comp = entry.get("competitors_file") or f"config/competitors_{channel}.json"
    if not (ROOT / comp).is_file():
        missing.append("competitors")
    seeds = entry.get("seed_titles_file") or f"config/seed_titles_{channel}.json"
    if not (ROOT / seeds).is_file():
        missing.append("seed_titles")
    if entry.get("requires_disclaimer"):
        disc = entry.get("disclaimer_file") or "config/sop/money_history_disclaimer.txt"
        if not (ROOT / disc).is_file():
            missing.append("disclaimer")
    return {
        "channel": channel,
        "ready": not missing,
        "missing": missing,
        "prompts_dir": prompts,
        "competitors_file": comp,
        "seed_titles_file": seeds,
    }


def _monetized_channel_count() -> int:
    """Count channels marked monetized in network_revenue state or empire flags."""
    rev = _load_json(NETWORK_REVENUE_PATH)
    state = rev.get("state") if isinstance(rev.get("state"), dict) else {}
    counted = state.get("monetized_channels")
    if isinstance(counted, list):
        return len(counted)
    # Fallback: production-enabled channels with youtube id in agents_settings
    try:
        agents = _load_json(CONFIG_DIR / "agents_settings.json")
        n = 0
        for item in agents.get("sheet_channels") or []:
            if isinstance(item, dict) and str(item.get("youtube_channel_id") or "").strip():
                n += 1
        return n
    except Exception:  # noqa: BLE001
        return len(production_channel_names())


def network_revenue_status() -> dict[str, Any]:
    emp = load_empire()
    wave3 = (emp.get("waves") or {}).get("wave3_network_revenue") or {}
    need = int(wave3.get("unlock_when_monetized_channels_gte") or 6)
    have = _monetized_channel_count()
    unlocked = have >= need
    rev = _load_json(NETWORK_REVENUE_PATH)
    layers = rev.get("layers") if isinstance(rev.get("layers"), dict) else {}
    return {
        "ok": True,
        "unlocked": unlocked,
        "monetized_channels": have,
        "required": need,
        "layers_configured": list(layers.keys()) if layers else list(wave3.get("layers") or []),
        "message": (
            "Network revenue layers may run"
            if unlocked
            else f"Need {need - have} more monetized channel(s) before affiliates/sponsors/licensing"
        ),
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


def write_empire_ops_digest() -> Path:
    """Refresh harden + expansion + revenue digests for ops."""
    harden = evaluate_core_harden()
    next_ch = (load_empire().get("activation_order") or ["art_mysteries"])[0]
    gate = evaluate_expansion_gate(next_ch)
    revenue = network_revenue_status()
    digest = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "harden": harden,
        "next_activation": gate,
        "network_revenue": revenue,
        "production_channels": production_channel_names(),
        "staged_wave1": staged_channel_names(wave=1),
        "staged_wave2": staged_channel_names(wave=2),
    }
    path = ROOT / "output" / "ops" / "channel_empire_digest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(digest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md = ROOT / "output" / "ops" / "CHANNEL_EMPIRE_STATUS.md"
    lines = [
        "# Channel empire status",
        "",
        f"_Generated: {digest['updated_at']}_",
        "",
        f"- Core harden ready for Wave 1: **{harden.get('ready_for_wave1')}**",
        f"- Archival OK: {harden.get('archival_ok')} (rate={((harden.get('archival') or {}).get('pass_rate'))})",
        f"- Volume lock: {harden.get('videos_per_month_locked')}/mo (do not raise until bars)",
        f"- Next activate candidate: `{next_ch}` allowed={gate.get('allowed')}",
        f"- Network revenue unlocked: {revenue.get('unlocked')} ({revenue.get('monetized_channels')}/{revenue.get('required')})",
        "",
        "## Blockers",
    ]
    for b in harden.get("blockers") or []:
        lines.append(f"- {b}")
    for r in gate.get("reasons") or []:
        lines.append(f"- gate: {r}")
    lines.append("")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
