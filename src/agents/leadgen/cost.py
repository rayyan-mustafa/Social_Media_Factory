"""Lead-gen cost sheet — forecast BEFORE job, actual AFTER. Never uses YouTube CostGuardian."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.services.settings import CONFIG_DIR, ROOT

RATES_PATH = CONFIG_DIR / "leadgen" / "cost_rates.json"
SPEND_LOG = ROOT / "output" / "ops" / "leadgen_spend.jsonl"
OPS = ROOT / "output" / "ops"


def load_rates() -> dict[str, Any]:
    if not RATES_PATH.is_file():
        return {}
    return json.loads(RATES_PATH.read_text(encoding="utf-8"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def forecast(*, limit: int, source: str = "nominatim") -> dict[str, Any]:
    """Cost sheet BEFORE the job. Places vs Nominatim changes paid line."""
    rates = load_rates()
    b = rates.get("benchmarks") or {}
    n = max(1, int(limit))
    pages = max(1, (n + 19) // 20)
    if source == "places" or source == "places_new":
        places = pages * float(b.get("places_textsearch_usd") or 0.032)
        details = n * float(b.get("places_details_usd") or 0.017)
    else:
        places = 0.0
        details = 0.0
    line = {
        "vps_usd": 0.0,
        "runpod_usd": 0.0,
        "kokoro_usd": 0.0,
        "places_usd": round(places + details, 4),
        "llm_usd": 0.0,
        "other_paid_usd": 0.0,
    }
    total = round(sum(line.values()), 4)
    return {
        "when": "before",
        "at": _now(),
        "source_assumed": source,
        "limit": n,
        "line_items": line,
        "total_usd": total,
        "caps": rates.get("caps") or {},
        "bans": rates.get("bans") or [],
        "distribution": rates.get("distribution") or {},
        "note": "Forecast. RunPod/Kokoro must stay 0. Nominatim hunt is $0.",
    }


def actual(
    *,
    source: str,
    leads_n: int,
    places_searches: int = 0,
    places_details: int = 0,
    elapsed_s: float = 0.0,
    homepage_fetches: int = 0,
) -> dict[str, Any]:
    rates = load_rates()
    b = rates.get("benchmarks") or {}
    paid_places = source in {"places", "places_new"}
    places = 0.0
    if paid_places:
        places = (
            int(places_searches) * float(b.get("places_textsearch_usd") or 0.032)
            + int(places_details) * float(b.get("places_details_usd") or 0.017)
        )
    line = {
        "vps_usd": 0.0,
        "runpod_usd": 0.0,
        "kokoro_usd": 0.0,
        "places_usd": round(places, 4),
        "llm_usd": 0.0,
        "other_paid_usd": 0.0,
    }
    total = round(sum(line.values()), 4)
    bench_s = float(b.get("hunt_25_elapsed_s_max") or 240)
    return {
        "when": "after",
        "at": _now(),
        "source": source,
        "leads_n": int(leads_n),
        "places_searches": int(places_searches),
        "places_details": int(places_details),
        "homepage_fetches": int(homepage_fetches),
        "elapsed_s": round(float(elapsed_s), 2),
        "elapsed_benchmark_s": bench_s,
        "elapsed_ok": float(elapsed_s) <= bench_s or int(leads_n) < 10,
        "line_items": line,
        "total_usd": total,
        "caps": rates.get("caps") or {},
        "distribution": {
            "vps": "100% CPU hunt/fetch/score/phantom",
            "runpod": "0%",
            "kokoro": "0%",
            "places": f"{places:.4f} USD" if paid_places else "0 (Nominatim/OSM)",
        },
        "bans_held": True,
    }


def remaining_month_usd() -> float:
    rates = load_rates()
    cap = float((rates.get("caps") or {}).get("usd_per_month_max") or 5.0)
    spent = 0.0
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    if SPEND_LOG.is_file():
        for line in SPEND_LOG.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            at = str(row.get("at") or "")
            if at.startswith(month):
                spent += float(row.get("total_usd") or 0)
    return round(cap - spent, 4)


def can_afford(forecast_sheet: dict[str, Any]) -> tuple[bool, str]:
    rates = load_rates()
    caps = rates.get("caps") or {}
    job_max = float(caps.get("usd_per_job_max") or 1.0)
    total = float(forecast_sheet.get("total_usd") or 0)
    if total - 1e-9 > job_max:
        return False, f"forecast ${total:.4f} > per-job cap ${job_max:.2f}"
    left = remaining_month_usd()
    if total - 1e-9 > left:
        return False, f"forecast ${total:.4f} > remaining monthly ${left:.2f}"
    return True, f"ok forecast ${total:.4f} remaining_month ${left:.2f}"


def render_sheet(sheet: dict[str, Any], *, title: str) -> str:
    lines = [f"# {title}", "", f"- when: `{sheet.get('when')}`", f"- at: `{sheet.get('at')}`"]
    if sheet.get("source") or sheet.get("source_assumed"):
        lines.append(f"- source: `{sheet.get('source') or sheet.get('source_assumed')}`")
    if sheet.get("elapsed_s") is not None and sheet.get("when") == "after":
        lines.append(
            f"- elapsed: {sheet.get('elapsed_s')}s (benchmark ≤ {sheet.get('elapsed_benchmark_s')}s)"
        )
    lines.append(f"- **total_usd: {sheet.get('total_usd')}**")
    lines.append("")
    lines.append("| resource | usd |")
    lines.append("|---|---:|")
    for k, v in (sheet.get("line_items") or {}).items():
        lines.append(f"| {k} | {v} |")
    dist = sheet.get("distribution") or {}
    if dist:
        lines.extend(["", "## Resource distribution", ""])
        for k, v in dist.items():
            lines.append(f"- **{k}:** {v}")
    bans = sheet.get("bans")
    if bans:
        lines.extend(["", f"Bans: {', '.join(str(b) for b in bans)}"])
    lines.append("")
    return "\n".join(lines)


def write_job_sheets(out_dir: Path, *, before: dict[str, Any], after: dict[str, Any] | None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "COST_SHEET.md"
    parts = [render_sheet(before, title="Lead-gen cost sheet — BEFORE")]
    if after:
        parts.append(render_sheet(after, title="Lead-gen cost sheet — AFTER"))
        delta = round(float(after.get("total_usd") or 0) - float(before.get("total_usd") or 0), 4)
        parts.append(f"\n**Delta (after − before): ${delta:.4f}**\n")
    md.write_text("\n".join(parts), encoding="utf-8")
    (out_dir / "cost_before.json").write_text(json.dumps(before, indent=2) + "\n", encoding="utf-8")
    if after:
        (out_dir / "cost_after.json").write_text(json.dumps(after, indent=2) + "\n", encoding="utf-8")
    return md


def append_spend(after: dict[str, Any], *, run_id: str) -> None:
    SPEND_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "at": after.get("at") or _now(),
        "run_id": run_id,
        "total_usd": after.get("total_usd"),
        "source": after.get("source"),
        "leads_n": after.get("leads_n"),
        "elapsed_s": after.get("elapsed_s"),
        "line_items": after.get("line_items"),
    }
    with SPEND_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
