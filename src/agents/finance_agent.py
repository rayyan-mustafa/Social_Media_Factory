"""Finance / market-valuation agent — open-market price ticket for the factory.

Extends CostGuardian (unit $ + monthly pack) with a CTO-realistic sale ticket:
floor / easy-ask / stretch, scored from live ops params. Not a SaaS ARR model —
productized agency tool / indie ops bundle.

Writes:
  output/ops/price_ticket.json   (latest)
  output/ops/price_ticket.jsonl  (history)
  CTO_STATUS.md marked snippet   (optional)
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.cost_guardian import CostGuardian
from src.agents.ledger import OpsLedger
from src.agents.store import OPS_DIR, OpsStore
from src.services.settings import CONFIG_DIR, get_settings

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

PRICE_TICKET_PATH = OPS_DIR / "price_ticket.json"
PRICE_TICKET_JSONL = OPS_DIR / "price_ticket.jsonl"
CTO_STATUS_PATH = OPS_DIR / "CTO_STATUS.md"

TICKET_BEGIN = "<!-- PRICE_TICKET:BEGIN -->"
TICKET_END = "<!-- PRICE_TICKET:END -->"

# Honest comps (2026): templates $49–$997; Fiverr setups $0.5k–$5k;
# Flippa indie ops tools $3k–$15k; productized agency handoff $8k–$25k.
# This is NOT a multi-tenant SaaS with ARR — do not stretch to $50k–$100k.
DEFAULT_BASE = {
    "floor_usd": 3500,
    "easy_ask_usd": 9500,
    "stretch_usd": 22000,
}

COMPARABLES = [
    {
        "name": "n8n / Make AI YouTube templates",
        "range_usd": [49, 497],
        "note": "Workflow-only; no live farm / GPU ops",
    },
    {
        "name": "Gumroad faceless YT automation kits",
        "range_usd": [97, 997],
        "note": "Docs + prompts; buyer builds ops",
    },
    {
        "name": "Fiverr/Upwork AI YouTube factory setup gigs",
        "range_usd": [500, 5000],
        "note": "One-shot setup; rarely includes capacity traffic-light",
    },
    {
        "name": "Flippa / indie sale: single-tenant automation + ops",
        "range_usd": [3000, 15000],
        "note": "Closest peer class for this repo",
    },
    {
        "name": "Productized agency tool with proven production",
        "range_usd": [8000, 25000],
        "note": "Handoff + docs + short warranty; stretch ceiling",
    },
    {
        "name": "Early multi-tenant SaaS with ARR",
        "range_usd": [50000, 150000],
        "note": "NOT this system — no SaaS UI / billing / tenants",
    },
]


class FinanceAgent:
    """CostGuardian + open-market price ticket; regular recheck for evolving params."""

    def __init__(
        self,
        store: OpsStore | None = None,
        ledger: OpsLedger | None = None,
        cost: CostGuardian | None = None,
        *,
        ops_dir: Path | None = None,
    ):
        self.store = store or OpsStore()
        self.ledger = ledger or OpsLedger(self.store)
        self.cost = cost or CostGuardian(self.store, self.ledger)
        self.cfg = _load_agents_settings()
        self.fin = dict(self.cfg.get("finance_agent") or {})
        self.s = get_settings()
        self.ops_dir = Path(ops_dir) if ops_dir else OPS_DIR
        self.ticket_path = self.ops_dir / "price_ticket.json"
        self.ticket_jsonl = self.ops_dir / "price_ticket.jsonl"
        self.cto_path = self.ops_dir / "CTO_STATUS.md"

    # ------------------------------------------------------------------ collect

    def collect_live_params(self) -> dict[str, Any]:
        """Snapshot live knobs that move valuation as the system evolves."""
        channels = _sheet_channels()
        br = self.cost.estimate_video_breakdown()
        pack = self.cost.estimate_monthly_pack(channels=len(channels) or None)
        per_video = float(br.get("total_usd_rounded") or br.get("total_usd") or 0.0)
        rates = self.cost.rate_card()

        jobs = self.store.list_jobs()
        terminal_ok = {
            "private",
            "scheduled",
            "public",
            "done",
        }
        proven = sum(1 for j in jobs if j.status in terminal_ok)
        failed = sum(1 for j in jobs if j.status in {"failed", "hold"})

        tts = (
            str(rates.get("production_pack_tts") or "").strip()
            or str(getattr(self.s, "tts_backend", None) or os.getenv("TTS_BACKEND") or "kokoro")
        ).lower()
        visuals_mode = str(rates.get("runpod_visuals_mode") or "pod").lower()
        max_gpu = max(1, _env_int("MAX_GPU_CONCURRENT", 1))
        max_prep = max(0, _env_int("MAX_PREP_CONCURRENT", 1))
        prep_while_gpu = _env_truthy("RUNPOD_PREP_WHILE_GPU", "1")
        prep_voice = _env_truthy("RUNPOD_PREP_VOICE", "1")
        watchdog = _env_truthy("RUNPOD_WATCHDOG", "1")
        auto_start = _env_truthy("RUNPOD_AUTO_START", "1")
        kill_on_error = _env_truthy("RUNPOD_KILL_ON_ERROR", "1")
        learn_days = _env_int("RUNPOD_LEARN_LOOKBACK_DAYS", 365)
        idea_stock = _env_int("IDEA_STOCK_TARGET", 15)
        sleep_farm = bool((self.cfg.get("sleep_beat") or {}).get("enqueue_pipeline"))

        return {
            "channels": channels,
            "channel_count": len(channels),
            "per_video_usd": per_video,
            "per_video_usd_raw": float(br.get("total_usd") or per_video),
            "per_video_line_items": br.get("line_items") or {},
            "monthly_pack_usd": float(pack.get("total_usd") or 0.0),
            "monthly_videos": int(pack.get("videos_per_month") or 0),
            "profile": br.get("profile") or "retention",
            "tts_backend": tts,
            "stills_backend": visuals_mode,
            "runpod_gpu_class": (br.get("quantities") or {}).get("runpod_gpu_class"),
            "stills_per_video": (br.get("quantities") or {}).get("stills"),
            "seedream_required": (br.get("quantities") or {}).get(
                "seedream_required", True
            ),
            "max_gpu_concurrent": max_gpu,
            "max_prep_concurrent": max_prep,
            "prep_while_gpu": prep_while_gpu,
            "prep_voice": prep_voice,
            "watchdog_armed": watchdog,
            "auto_start": auto_start,
            "kill_on_error": kill_on_error,
            "learn_lookback_days": learn_days,
            "idea_stock_target": idea_stock,
            "blind_farm_armed": sleep_farm,
            "cost_guardian_on": True,
            "proven_terminal_jobs": proven,
            "failed_or_hold_jobs": failed,
            "jobs_total": len(jobs),
            "host_profile": "4vCPU/8GB VPS · max 1 GPU job",
            "category": "productized_agency_tool_indie_ops_bundle",
            "not_saas_yet": True,
        }

    # ------------------------------------------------------------------ score

    def score_factors(self, params: dict[str, Any]) -> dict[str, Any]:
        """0–100 factor scores + weighted overall (CTO-honest weights)."""
        ch = int(params.get("channel_count") or 0)
        per = float(params.get("per_video_usd") or 99.0)
        proven = int(params.get("proven_terminal_jobs") or 0)
        max_gpu = int(params.get("max_gpu_concurrent") or 1)

        # Autonomy: cron gate + auto-start + prep bank + kill-on-error + CostGuardian
        auto_pts = 20.0  # CostGuardian always counted
        if params.get("watchdog_armed"):
            auto_pts += 25.0
        if params.get("auto_start"):
            auto_pts += 20.0
        if params.get("prep_voice"):
            auto_pts += 15.0
        if params.get("kill_on_error"):
            auto_pts += 10.0
        if params.get("prep_while_gpu"):
            auto_pts += 10.0
        if params.get("blind_farm_armed"):
            # Blind farm armed without GREEN gate is riskier for buyers → slight drag
            auto_pts -= 5.0
        autonomy = _clamp(auto_pts)

        # Multi-channel sheet ops
        if ch <= 0:
            multi = 20.0
        elif ch == 1:
            multi = 45.0
        elif ch == 2:
            multi = 78.0
        else:
            multi = min(95.0, 78.0 + (ch - 2) * 6.0)
        if int(params.get("idea_stock_target") or 0) >= 10:
            multi = min(100.0, multi + 8.0)

        # Unit economics — retention target ~$0.31–0.38 is excellent
        if per <= 0.25:
            unit = 95.0
        elif per <= 0.40:
            unit = 88.0
        elif per <= 0.60:
            unit = 72.0
        elif per <= 1.00:
            unit = 55.0
        elif per <= 2.00:
            unit = 35.0
        else:
            unit = 15.0
        if str(params.get("tts_backend") or "").startswith("kokoro"):
            unit = min(100.0, unit + 5.0)

        # GPU traffic-light / ops maturity
        gpu = 35.0
        if params.get("stills_backend") == "pod":
            gpu += 15.0
        if params.get("kill_on_error"):
            gpu += 10.0
        if int(params.get("learn_lookback_days") or 0) >= 90:
            gpu += 15.0
        if params.get("prep_while_gpu"):
            gpu += 10.0
        if max_gpu == 1:
            gpu += 10.0  # disciplined 0-orphan / single-pod — buyer-safe
        elif max_gpu > 2:
            gpu -= 5.0  # more blast radius without SaaS ops polish
        gpu = _clamp(gpu)

        # Proven output
        if proven <= 0:
            proven_s = 25.0
        elif proven == 1:
            proven_s = 55.0
        elif proven < 5:
            proven_s = 70.0
        elif proven < 15:
            proven_s = 85.0
        else:
            proven_s = 95.0

        # Scale headroom (honest: 1 GPU + small VPS caps enterprise ask)
        if max_gpu == 1 and ch <= 2:
            scale = 40.0  # solid indie; not enterprise
        elif max_gpu >= 2 and ch >= 3:
            scale = 70.0
        else:
            scale = 50.0
        if params.get("not_saas_yet"):
            scale = min(scale, 45.0)

        weights = {
            "autonomy": 0.22,
            "multi_channel": 0.15,
            "unit_economics": 0.22,
            "gpu_ops": 0.18,
            "proven_output": 0.15,
            "scale_headroom": 0.08,
        }
        factors = {
            "autonomy": round(autonomy, 1),
            "multi_channel": round(multi, 1),
            "unit_economics": round(unit, 1),
            "gpu_ops": round(gpu, 1),
            "proven_output": round(proven_s, 1),
            "scale_headroom": round(scale, 1),
        }
        overall = sum(factors[k] * weights[k] for k in weights)
        return {
            "factors": factors,
            "weights": weights,
            "overall_score": round(overall, 1),
            # Map score 0–100 → multiplier ~0.72–1.18 around base anchors
            "price_multiplier": round(0.72 + (overall / 100.0) * 0.46, 4),
        }

    def compute_ticket(
        self, *, force_params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        params = force_params or self.collect_live_params()
        scored = self.score_factors(params)
        mult = float(scored["price_multiplier"])
        base = {
            "floor_usd": float(
                self.fin.get("base_floor_usd") or DEFAULT_BASE["floor_usd"]
            ),
            "easy_ask_usd": float(
                self.fin.get("base_easy_ask_usd") or DEFAULT_BASE["easy_ask_usd"]
            ),
            "stretch_usd": float(
                self.fin.get("base_stretch_usd") or DEFAULT_BASE["stretch_usd"]
            ),
        }
        pkr = float(
            self.fin.get("pkr_per_usd")
            or (self.cost.rate_card().get("pkr_per_usd_approx"))
            or 280.0
        )

        def _band(v: float) -> dict[str, float]:
            usd = round(v * mult / 100.0) * 100  # nearest $100
            usd = max(500.0, float(usd))
            return {"usd": usd, "pkr_approx": round(usd * pkr, 0)}

        floor = _band(base["floor_usd"])
        ask = _band(base["easy_ask_usd"])
        stretch = _band(base["stretch_usd"])

        drivers = _ticket_drivers(params, scored)
        now = datetime.now(timezone.utc).isoformat()
        ticket = {
            "at": now,
            "label": "open_market_price_ticket",
            "category": params.get("category"),
            "verdict": (
                "Productized agency / indie ops bundle — not a $100k SaaS. "
                "Easy-ask is what it would catch/sell for without hard negotiation "
                "given proven production + GREEN GPU farm + sub-$0.40/video."
            ),
            "ticket": {
                "floor": floor,
                "easy_ask": ask,
                "stretch": stretch,
                "currency": "USD",
                "pkr_per_usd": pkr,
            },
            "headline": {
                "floor_usd": floor["usd"],
                "easy_ask_usd": ask["usd"],
                "stretch_usd": stretch["usd"],
                "floor_pkr": floor["pkr_approx"],
                "easy_ask_pkr": ask["pkr_approx"],
                "stretch_pkr": stretch["pkr_approx"],
            },
            "score": scored,
            "live_params": params,
            "drivers": drivers,
            "comparables": COMPARABLES,
            "base_anchors_usd": base,
            "recheck_hours": float(self.fin.get("recheck_hours") or 24),
        }
        return ticket

    # ------------------------------------------------------------------ persist

    def write_ticket(self, ticket: dict[str, Any] | None = None) -> dict[str, Any]:
        ticket = ticket or self.compute_ticket()
        self.ops_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(ticket, indent=2, ensure_ascii=False) + "\n"
        with _LOCK:
            self.ticket_path.write_text(text, encoding="utf-8")
            with self.ticket_jsonl.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ticket, ensure_ascii=False) + "\n")
        try:
            h = ticket.get("headline") or {}
            self.ledger.write(
                agent="finance_agent",
                problem=(
                    f"price ticket floor=${h.get('floor_usd')} "
                    f"ask=${h.get('easy_ask_usd')} stretch=${h.get('stretch_usd')} "
                    f"score={((ticket.get('score') or {}).get('overall_score'))}"
                ),
                action="recomputed market valuation",
                severity="info",
                extra={
                    "headline": h,
                    "overall_score": (ticket.get("score") or {}).get("overall_score"),
                    "per_video_usd": (ticket.get("live_params") or {}).get(
                        "per_video_usd"
                    ),
                    "channels": (ticket.get("live_params") or {}).get("channels"),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("finance ledger write failed: %s", exc)
        return ticket

    def update_cto_status(self, ticket: dict[str, Any] | None = None) -> bool:
        """Replace/insert marked PRICE_TICKET section in CTO_STATUS.md."""
        if self.fin.get("update_cto_status") is False:
            return False
        ticket = ticket or self.load_ticket() or self.compute_ticket()
        snippet = render_cto_snippet(ticket)
        path = self.cto_path
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"# CTO status\n\n{TICKET_BEGIN}\n{snippet}\n{TICKET_END}\n",
                encoding="utf-8",
            )
            return True
        body = path.read_text(encoding="utf-8")
        block = f"{TICKET_BEGIN}\n{snippet}\n{TICKET_END}"
        if TICKET_BEGIN in body and TICKET_END in body:
            body = re.sub(
                re.escape(TICKET_BEGIN) + r".*?" + re.escape(TICKET_END),
                block,
                body,
                count=1,
                flags=re.DOTALL,
            )
        else:
            # Insert prominently after title / first heading block
            lines = body.splitlines(keepends=True)
            insert_at = 0
            for i, line in enumerate(lines[:30]):
                if line.startswith("# "):
                    insert_at = i + 1
                    # skip blank + Updated line if present
                    j = insert_at
                    while j < len(lines) and (
                        lines[j].strip() == ""
                        or lines[j].startswith("**Updated")
                        or lines[j].startswith("**Repo")
                        or lines[j].startswith("**Owner")
                    ):
                        j += 1
                    insert_at = j
                    break
            lines.insert(insert_at, "\n" + block + "\n\n")
            body = "".join(lines)
        with _LOCK:
            path.write_text(body, encoding="utf-8")
        return True

    def load_ticket(self) -> dict[str, Any] | None:
        if not self.ticket_path.exists():
            return None
        try:
            data = json.loads(self.ticket_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def due_for_recheck(self, *, now: datetime | None = None) -> tuple[bool, str]:
        if self.fin.get("enabled") is False:
            return False, "finance_agent disabled"
        hours = float(self.fin.get("recheck_hours") or 24)
        prev = self.load_ticket()
        if not prev or not prev.get("at"):
            return True, "no prior ticket"
        try:
            last = datetime.fromisoformat(str(prev["at"]).replace("Z", "+00:00"))
        except ValueError:
            return True, "prior ticket timestamp invalid"
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        age_h = (now - last.astimezone(timezone.utc)).total_seconds() / 3600.0
        if age_h >= hours:
            return True, f"age {age_h:.1f}h >= {hours}h"
        return False, f"fresh age {age_h:.1f}h < {hours}h"

    def maybe_recompute(
        self,
        *,
        force: bool = False,
        dry_run: bool = False,
        update_cto: bool = True,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Watchdog/daily hook: recompute when due (or force)."""
        due, reason = self.due_for_recheck(now=now)
        if not force and not due:
            prev = self.load_ticket() or {}
            return {
                "skipped": True,
                "reason": reason,
                "headline": prev.get("headline"),
                "at": prev.get("at"),
            }
        ticket = self.compute_ticket()
        if dry_run:
            return {
                "skipped": False,
                "dry_run": True,
                "reason": reason if not force else "force",
                "headline": ticket.get("headline"),
                "ticket": ticket,
            }
        self.write_ticket(ticket)
        cto_ok = False
        if update_cto and self.fin.get("update_cto_status") is not False:
            try:
                cto_ok = self.update_cto_status(ticket)
            except Exception as exc:  # noqa: BLE001
                logger.warning("CTO_STATUS price ticket update failed: %s", exc)
        return {
            "skipped": False,
            "reason": reason if not force else "force",
            "headline": ticket.get("headline"),
            "overall_score": (ticket.get("score") or {}).get("overall_score"),
            "wrote": str(self.ticket_path),
            "cto_updated": cto_ok,
            "at": ticket.get("at"),
        }


def render_cto_snippet(ticket: dict[str, Any]) -> str:
    h = ticket.get("headline") or {}
    params = ticket.get("live_params") or {}
    score = ticket.get("score") or {}
    drivers = ticket.get("drivers") or []
    floor = h.get("floor_usd")
    ask = h.get("easy_ask_usd")
    stretch = h.get("stretch_usd")
    pkr = (ticket.get("ticket") or {}).get("pkr_per_usd") or 280
    driver_lines = "\n".join(f"- {d}" for d in drivers[:6]) or "- (none)"
    return f"""## Price ticket (open-market valuation)

| Band | USD | PKR (×{int(pkr)}) |
|------|-----|-------------------|
| **Floor** (quick close / code+ops dump) | **${floor:,.0f}** | ~{int(h.get('floor_pkr') or 0):,} |
| **Easy-ask** (what it catches/sells for) | **${ask:,.0f}** | ~{int(h.get('easy_ask_pkr') or 0):,} |
| **Stretch** (full handoff + warranty) | **${stretch:,.0f}** | ~{int(h.get('stretch_pkr') or 0):,} |

**Current ticket:** floor **${floor:,.0f}** · easy-ask **${ask:,.0f}** · stretch **${stretch:,.0f}**  
**Score:** {score.get('overall_score')}/100 · category: productized agency / indie ops bundle (**not** $100k SaaS)  
**Live unit cost:** ~${float(params.get('per_video_usd') or 0):.2f}/video · channels: `{', '.join(params.get('channels') or [])}` · TTS `{params.get('tts_backend')}` · stills `{params.get('stills_backend')}` · max GPU `{params.get('max_gpu_concurrent')}`  
**Updated:** {ticket.get('at')} · source `output/ops/price_ticket.json` (FinanceAgent recheck)

**What drives it**
{driver_lines}
"""


def _ticket_drivers(params: dict[str, Any], scored: dict[str, Any]) -> list[str]:
    factors = scored.get("factors") or {}
    out: list[str] = []
    out.append(
        f"Unit economics ~${float(params.get('per_video_usd') or 0):.2f}/retention video "
        f"(Kokoro $0 + Seedream + A5000/3090 Community) → score {factors.get('unit_economics')}"
    )
    out.append(
        f"Autonomy: watchdog={'ON' if params.get('watchdog_armed') else 'OFF'} "
        f"auto_start={'ON' if params.get('auto_start') else 'OFF'} "
        f"prep∥GPU={'ON' if params.get('prep_while_gpu') else 'OFF'} "
        f"kill-on-error={'ON' if params.get('kill_on_error') else 'OFF'} "
        f"→ {factors.get('autonomy')}"
    )
    out.append(
        f"Multi-channel Sheets ({params.get('channel_count')}: "
        f"{', '.join(params.get('channels') or [])}) + idea stock "
        f"{params.get('idea_stock_target')} → {factors.get('multi_channel')}"
    )
    out.append(
        f"GPU ops: GREEN-only farm, {params.get('learn_lookback_days')}d learning, "
        f"stills={params.get('stills_backend')} → {factors.get('gpu_ops')}"
    )
    out.append(
        f"Proven terminal jobs={params.get('proven_terminal_jobs')} "
        f"(failed/hold={params.get('failed_or_hold_jobs')}) → {factors.get('proven_output')}"
    )
    out.append(
        f"Scale honesty: {params.get('host_profile')} — not multi-tenant SaaS "
        f"→ headroom {factors.get('scale_headroom')}"
    )
    return out


def _sheet_channels() -> list[str]:
    try:
        from src.agents.sheet_channels import configured_sheet_channels

        return list(configured_sheet_channels())
    except Exception:  # noqa: BLE001
        raw = (os.getenv("SHEET_CHANNELS") or "napstorian,napping_historian").strip()
        return [p.strip() for p in raw.split(",") if p.strip()]


def _load_agents_settings() -> dict[str, Any]:
    path = CONFIG_DIR / "agents_settings.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_truthy(name: str, default: str = "0") -> bool:
    raw = (os.getenv(name) if name in os.environ else default) or default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}
