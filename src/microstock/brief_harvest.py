"""Brief harvest — keeps the engine supplied with things worth drawing.

The microstock counterpart to ``src/agents/idea_stock.py``. Same consume-replace
contract: production debits the stock, and the harvester refills it back to target
so the beat never stalls waiting on an LLM. What differs is the unit of work —
asset briefs weighted by commercial niche, not YouTube titles — which is why this
is a sibling of ``trends_agent`` rather than a reuse of it (that module is bound
to the YouTube Data API and competitor channels).

The feedback loop closes here: ``stock_smm`` writes observed per-niche sales into
``sales_benchmarks.json``, and this module biases future briefs toward whatever
is actually earning. That is the same shape as
``smm_harvest_bridge.winner_bias_for_title``.
"""

from __future__ import annotations

import json
import logging
import random
import threading
from datetime import datetime, timezone
from typing import Any

from src.microstock import config, paths

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

# How strongly observed sales are allowed to distort the configured niche weights.
# Capped so one lucky asset cannot collapse the catalogue into a single niche.
MAX_SALES_BIAS = 2.0
MIN_SALES_BIAS = 0.5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- stock file
def load_stock() -> list[dict[str, Any]]:
    data = _read_json(paths.BRIEF_STOCK_PATH, {"briefs": []})
    briefs = data.get("briefs")
    return list(briefs) if isinstance(briefs, list) else []


def save_stock(briefs: list[dict[str, Any]]) -> None:
    _write_json(paths.BRIEF_STOCK_PATH, {"updated_at": _now(), "briefs": briefs})


def stock_count() -> int:
    return len(load_stock())


def consume(count: int) -> list[dict[str, Any]]:
    """Take up to ``count`` briefs off the stock. This is the 'consume' half."""
    with _LOCK:
        briefs = load_stock()
        taken, remaining = briefs[:count], briefs[count:]
        if taken:
            save_stock(remaining)
    logger.info("brief_harvest: consumed %d, %d left in stock", len(taken), len(remaining) if taken else stock_count())
    return taken


def add(briefs: list[dict[str, Any]]) -> int:
    """Append newly invented briefs. This is the 'replace' half."""
    if not briefs:
        return 0
    with _LOCK:
        existing = load_stock()
        seen = {b.get("subject", "").strip().lower() for b in existing}
        fresh = [
            b for b in briefs
            if b.get("subject", "").strip().lower() and b["subject"].strip().lower() not in seen
        ]
        save_stock(existing + fresh)
    logger.info("brief_harvest: added %d new brief(s)", len(fresh))
    return len(fresh)


# ------------------------------------------------------------- niche weighting
def load_sales_bias() -> dict[str, float]:
    """Per-niche multiplier from observed sales, written by ``stock_smm``."""
    data = _read_json(paths.SALES_BENCHMARKS_PATH, {})
    niches = data.get("niches") if isinstance(data, dict) else None
    if not isinstance(niches, dict):
        return {}
    scores = {
        name: float(row.get("score", 0.0))
        for name, row in niches.items()
        if isinstance(row, dict)
    }
    positive = [s for s in scores.values() if s > 0]
    if not positive:
        return {}
    mean = sum(positive) / len(positive)
    if mean <= 0:
        return {}
    return {
        name: max(MIN_SALES_BIAS, min(MAX_SALES_BIAS, score / mean))
        for name, score in scores.items()
    }


def weighted_niches() -> list[tuple[dict[str, Any], float]]:
    """Enabled niches with their configured weight times any sales bias."""
    bias = load_sales_bias()
    rows: list[tuple[dict[str, Any], float]] = []
    for niche in config.load_niches():
        weight = float(niche.get("weight") or 1.0) * bias.get(niche["name"], 1.0)
        if weight > 0:
            rows.append((niche, weight))
    return rows


def pick_niche(rng: random.Random | None = None) -> dict[str, Any] | None:
    rows = weighted_niches()
    if not rows:
        return None
    rng = rng or random
    total = sum(weight for _, weight in rows)
    target = rng.uniform(0, total)
    cumulative = 0.0
    for niche, weight in rows:
        cumulative += weight
        if target <= cumulative:
            return niche
    return rows[-1][0]


# ------------------------------------------------------------ brief invention
def _winner_bias_text(niche_name: str) -> str:
    bias = load_sales_bias()
    score = bias.get(niche_name)
    if score is None:
        return "No sales history yet — explore broadly across the deliverables above."
    if score >= 1.2:
        return (
            "SALES SIGNAL: this niche is outperforming. Stay close to what already "
            "sells here — iterate on proven subjects rather than exploring."
        )
    if score <= 0.8:
        return (
            "SALES SIGNAL: this niche is underperforming. Try noticeably different "
            "subjects and compositions than before."
        )
    return "SALES SIGNAL: this niche performs around average. Balance iteration with exploration."


def _template_briefs(niche: dict[str, Any], count: int, rng: random.Random) -> list[dict[str, Any]]:
    """Deterministic offline fallback so the beat never stalls on LLM availability."""
    deliverables = list(niche.get("deliverables") or ["icon set"])
    style = niche.get("style_hint", "flat vector illustration")
    briefs: list[dict[str, Any]] = []
    for index in range(count):
        subject = deliverables[index % len(deliverables)]
        variant = rng.choice(["single centred", "three-item set", "isometric", "outline", "duotone"])
        briefs.append({
            "subject": f"{variant} {subject}",
            "prompt": (
                f"{variant} {subject}, {style}, solid flat colours, centred on a plain "
                "white background with generous margin, no text, no logos, no shadows, "
                "square composition, commercial stock vector artwork"
            ),
            "niche": niche["name"],
            "source": "template",
            "created_at": _now(),
        })
    return briefs


def invent_briefs(
    niche: dict[str, Any], count: int, *, use_llm: bool = True,
    rng: random.Random | None = None,
) -> list[dict[str, Any]]:
    """Invent briefs for a niche, falling back to templates when the LLM is unavailable."""
    rng = rng or random.Random()
    if not use_llm:
        return _template_briefs(niche, count, rng)

    try:
        from src.services.openrouter import OpenRouterClient, OpenRouterError
        from src.services.settings import get_settings, load_prompt, render_prompt

        get_settings.cache_clear()
        settings = get_settings()
        if not (settings.openrouter_api_key or "").strip():
            raise OpenRouterError("OPENROUTER_API_KEY not set")

        template = load_prompt("brief_invent", prompts_dir=paths.PROMPTS_DIR)
        prompt = render_prompt(template, {
            "COUNT": count,
            "NICHE_DISPLAY": niche.get("display", niche["name"]),
            "DELIVERABLES": ", ".join(niche.get("deliverables") or []),
            "BUYERS": ", ".join(niche.get("buyers") or []),
            "STYLE_HINT": niche.get("style_hint", ""),
            "WINNER_BIAS": _winner_bias_text(niche["name"]),
        })
        client = OpenRouterClient(settings)
        payload = client.chat_json(prompt)
        rows = payload.get("briefs") if isinstance(payload, dict) else None
        briefs = [
            {
                "subject": str(r.get("subject", "")).strip(),
                "prompt": str(r.get("prompt", "")).strip(),
                "niche": niche["name"],
                "source": "llm",
                "created_at": _now(),
            }
            for r in (rows or [])
            if isinstance(r, dict) and r.get("subject") and r.get("prompt")
        ]
        if briefs:
            return briefs[:count]
        logger.warning("brief_harvest: LLM returned no usable briefs, using templates")
    except Exception as exc:  # noqa: BLE001 - any LLM failure must fall back, not crash the beat
        logger.warning("brief_harvest: LLM unavailable (%s), using templates", exc)

    return _template_briefs(niche, count, rng)


def maybe_refill(
    *, target: int | None = None, batch_per_niche: int = 8,
    use_llm: bool = True, dry_run: bool = False,
) -> dict[str, Any]:
    """Top the brief stock back up to target. The beat's step 1.

    Idempotent and cheap when the stock is already healthy — it returns without
    calling anything.
    """
    settings = config.load_settings()
    target = int(target if target is not None else settings.get("min_brief_stock") or 60)
    have = stock_count()
    if have >= target:
        return {"refilled": False, "reason": "stock_healthy", "have": have, "target": target}

    needed = target - have
    rng = random.Random()
    invented: list[dict[str, Any]] = []
    niches_used: list[str] = []

    while len(invented) < needed:
        niche = pick_niche(rng)
        if niche is None:
            return {"refilled": False, "reason": "no_enabled_niches", "have": have, "target": target}
        chunk = min(batch_per_niche, needed - len(invented))
        invented.extend(invent_briefs(niche, chunk, use_llm=use_llm, rng=rng))
        niches_used.append(niche["name"])
        if len(niches_used) > 40:  # safety valve against a pathological loop
            break

    if dry_run:
        return {
            "refilled": False, "reason": "dry_run", "have": have, "target": target,
            "would_add": len(invented), "niches": niches_used,
        }

    added = add(invented)
    return {
        "refilled": True, "have_before": have, "added": added,
        "have_after": stock_count(), "target": target, "niches": niches_used,
    }
