"""Lead-gen SMM — agency growth scorecard. Separate from YouTube ceo_smm / smm_agent."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.leadgen.config import list_regions, list_verticals
from src.services.settings import ROOT

OPS = ROOT / "output" / "ops"
CRM = OPS / "leadgen_crm.csv"
DIGEST_MD = OPS / "leadgen_smm_digest.md"
DIGEST_JSON = OPS / "leadgen_smm_digest.json"
HUNT_LOG = OPS / "leadgen_hunts.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _crm_rows() -> list[dict[str, str]]:
    if not CRM.is_file():
        return []
    with CRM.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def scorecard() -> dict[str, Any]:
    rows = _crm_rows()
    n = len(rows)
    sent = sum(1 for r in rows if str(r.get("sent") or "").upper() in {"TRUE", "1", "YES"})
    replies = sum(1 for r in rows if str(r.get("reply") or "").strip())
    by_v: dict[str, int] = {}
    by_r: dict[str, int] = {}
    scores: list[float] = []
    for r in rows:
        by_v[r.get("vertical") or "?"] = by_v.get(r.get("vertical") or "?", 0) + 1
        by_r[r.get("region") or "?"] = by_r.get(r.get("region") or "?", 0) + 1
        try:
            scores.append(float(r.get("score") or 0))
        except (TypeError, ValueError):
            pass
    hunts = 0
    if HUNT_LOG.is_file():
        hunts = sum(1 for line in HUNT_LOG.read_text(encoding="utf-8").splitlines() if line.strip())
    avg = round(sum(scores) / len(scores), 1) if scores else 0.0
    next_hint = "Run salon in a second US city if Austin gap density stays high."
    if by_v.get("salon", 0) >= 20 and by_v.get("estate", 0) < 5:
        next_hint = "Next hunt: --vertical estate (same region) — do not spray a new continent yet."
    return {
        "at": _now(),
        "agent": "leadgen_smm",
        "not": ["ceo_smm", "smm_agent", "youtube"],
        "crm_rows": n,
        "hunts_logged": hunts,
        "sent": sent,
        "replies": replies,
        "reply_rate": round(replies / sent, 3) if sent else 0.0,
        "avg_gap_score": avg,
        "by_vertical": by_v,
        "by_region": by_r,
        "regions_available": list_regions(),
        "verticals_available": list_verticals(),
        "next_move": next_hint,
        "rules": [
            "No auto-send",
            "No YouTube metrics mixed in",
            "Max 10 human sends/day until a sending domain exists",
        ],
    }


def write_digest(card: dict[str, Any] | None = None) -> dict[str, Any]:
    card = card or scorecard()
    md = [
        "# Lead-gen SMM digest (not YouTube SMM)",
        "",
        f"- at: `{card.get('at')}`",
        f"- CRM rows: **{card.get('crm_rows')}** · hunts: {card.get('hunts_logged')}",
        f"- sent (human): {card.get('sent')} · replies: {card.get('replies')} · reply_rate: {card.get('reply_rate')}",
        f"- avg gap score: {card.get('avg_gap_score')}",
        f"- next: {card.get('next_move')}",
        "",
        "## By vertical",
        "",
    ]
    for k, v in sorted((card.get("by_vertical") or {}).items(), key=lambda kv: -kv[1]):
        md.append(f"- {k}: {v}")
    md.extend(["", "## By region", ""])
    for k, v in sorted((card.get("by_region") or {}).items(), key=lambda kv: -kv[1]):
        md.append(f"- {k}: {v}")
    md.extend(["", "This beat never starts RunPod, Kokoro, or YouTube uploads.", ""])
    OPS.mkdir(parents=True, exist_ok=True)
    DIGEST_MD.write_text("\n".join(md), encoding="utf-8")
    DIGEST_JSON.write_text(json.dumps(card, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "digest_md": str(DIGEST_MD), "digest_json": str(DIGEST_JSON), **card}


def run_beat(*, dry_run: bool = False) -> dict[str, Any]:
    card = scorecard()
    if dry_run:
        return {"ok": True, "dry_run": True, **card}
    return write_digest(card)
