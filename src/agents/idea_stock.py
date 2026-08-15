"""Maintain idea titles **per sheet/channel** (harvest refill).

Locked refill rule (*/10 watchdog + */15 sleep_factory backup)
=============================================================

**A — Schedule (separate):** ``maybe_arm_public_approved_schedule`` arms
``public_approved=TRUE`` private jobs via ``publishAt`` (never sets the flag).

**B — Consume replace (this module):**

1. On each **successful** schedule-arm for a job on channel C, credit
   ``refill_credits[C] += 1`` (persisted in ``output/ops/idea_refill_credits.json``)
   and **archive+drop** that title from the idea sheet (OpsStore keeps the
   scheduled job). */10 hygiene also sweeps leftover ``status=scheduled`` /
   ops-scheduled rows and credits orphan drops.
2. On each harvest tick, run sheet hygiene first (scheduled drop + historian
   What-If + blocked junk), then for each channel C::

       stock_need    = max(0, IDEA_STOCK_TARGET - idea_stock[C])   # default target 15
       consume_need  = max(0, refill_credits[C])                  # lag-safe
       need          = max(stock_need, consume_need)

   ``idea_stock`` = ``policy_ok`` + queued + empty ``job_id``.
   Harvest invents ``need`` ORIGINAL titles with ``approved=FALSE`` /
   ``public_approved=FALSE`` (human still approves farm + public).
   Only ``policy_ok=TRUE`` titles are appended to the sheet.
3. After harvesting H titles, debit ``min(H, consume_need)`` from credits
   so failed harvests retry on the next */10 (refill lag catch-up).
4. Channel invent style stays in TrendsAgent: napstorian ``what_if``;
   napping_historian ``documentary_mystery`` + ``competitors_napping_historian.json``.

CostGuardian / GREEN farm stay separate — this path never starts GPU.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from src.agents.ledger import OpsLedger
from src.agents.sheet_channels import (
    channel_in_publish_subset,
    configured_sheet_channels,
)
from src.agents.store import OPS_DIR, OpsStore
from src.agents.title_queue import TitleQueue, TitleRow
from src.agents.trends_agent import TrendsAgent
from src.services.settings import CONFIG_DIR

logger = logging.getLogger(__name__)

DEFAULT_IDEA_STOCK_TARGET = 15
REFILL_CREDITS_NAME = "idea_refill_credits.json"

# Statuses / flags that mean a title is on the public schedule / publish path.
_PUBLISH_PATH_STATUSES = frozenset({"scheduled", "public"})


def _env_truthy(name: str, default: str = "0") -> bool:
    raw = (os.getenv(name) or default).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _agents_cfg() -> dict[str, Any]:
    path = CONFIG_DIR / "agents_settings.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _schedule_cfg() -> dict[str, Any]:
    path = CONFIG_DIR / "publish_schedule.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def publish_schedule_enabled() -> bool:
    """True when autonomous publish / idea-refill loop should run.

    Sources (any true wins):
    - env PUBLISH_SCHEDULE / PUBLISH_SCHEDULE_ENABLED
    - agents_settings.sleep_beat.publish (default true in repo)
    """
    if "PUBLISH_SCHEDULE" in os.environ:
        return _env_truthy("PUBLISH_SCHEDULE", "1")
    if "PUBLISH_SCHEDULE_ENABLED" in os.environ:
        return _env_truthy("PUBLISH_SCHEDULE_ENABLED", "1")
    beat = (_agents_cfg().get("sleep_beat") or {})
    if "publish" in beat:
        return bool(beat.get("publish"))
    return bool(_schedule_cfg())


def row_on_publish_path(row: TitleRow) -> bool:
    """True if this title is on the public schedule / publish path.

    Counts even a single row: public_approved, status scheduled/public, or
    scheduled_at set. Used so harvest does not wait for many public videos.
    """
    if bool(getattr(row, "public_approved", False)):
        return True
    status = (getattr(row, "status", None) or "").strip().lower()
    if status in _PUBLISH_PATH_STATUSES:
        return True
    if (getattr(row, "scheduled_at", None) or "").strip():
        return True
    return False


def count_publish_path_titles(
    queue: TitleQueue, *, channel: str | None = None
) -> int:
    """How many titles are on the public schedule / publish path."""
    return sum(1 for r in queue.list_rows(channel=channel) if row_on_publish_path(r))


def count_public_approved_pending(
    queue: TitleQueue, *, channel: str | None = None
) -> int:
    """Sheet rows with public_approved=TRUE not yet scheduled/public."""
    n = 0
    for r in queue.list_rows(channel=channel):
        if not bool(getattr(r, "public_approved", False)):
            continue
        status = (getattr(r, "status", None) or "").strip().lower()
        if status in _PUBLISH_PATH_STATUSES:
            continue
        n += 1
    return n


def refill_credits_path(root: Path | None = None) -> Path:
    return (root or OPS_DIR) / REFILL_CREDITS_NAME


def load_refill_credits(*, root: Path | None = None) -> dict[str, int]:
    path = refill_credits_path(root)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    credits = raw.get("credits") if isinstance(raw, dict) else None
    if not isinstance(credits, dict):
        credits = raw if isinstance(raw, dict) else {}
    out: dict[str, int] = {}
    for k, v in credits.items():
        if str(k).startswith("_"):
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out[str(k)] = n
    return out


def save_refill_credits(
    credits: dict[str, int], *, root: Path | None = None
) -> Path:
    path = refill_credits_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: int(v) for k, v in credits.items() if int(v) > 0}
    path.write_text(
        json.dumps(
            {
                "credits": clean,
                "rule": (
                    "On successful public_approved schedule-arm → +1/channel + "
                    "archive/drop sheet row; orphan scheduled sweep also credits; "
                    "harvest need=max(stock_deficit, credits); debit min(H, credits)."
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def credit_consume_refills(
    by_channel: dict[str, int],
    *,
    root: Path | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Add consume-replace credits after successful schedule arms. Returns new balances."""
    from src.services.youtube_channel_auth import normalize_youtube_channel

    current = load_refill_credits(root=root)
    for raw_ch, n in (by_channel or {}).items():
        try:
            add = int(n)
        except (TypeError, ValueError):
            continue
        if add <= 0:
            continue
        ch = normalize_youtube_channel(str(raw_ch))
        current[ch] = int(current.get(ch) or 0) + add
    if not dry_run:
        save_refill_credits(current, root=root)
    return dict(current)


def debit_consume_refills(
    by_channel: dict[str, int],
    *,
    root: Path | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Subtract credits after a successful harvest. Returns new balances."""
    from src.services.youtube_channel_auth import normalize_youtube_channel

    current = load_refill_credits(root=root)
    for raw_ch, n in (by_channel or {}).items():
        try:
            sub = int(n)
        except (TypeError, ValueError):
            continue
        if sub <= 0:
            continue
        ch = normalize_youtube_channel(str(raw_ch))
        left = max(0, int(current.get(ch) or 0) - sub)
        if left:
            current[ch] = left
        else:
            current.pop(ch, None)
    if not dry_run:
        save_refill_credits(current, root=root)
    return dict(current)


def refill_need_for_channel(
    *,
    stock: int,
    target: int,
    consume_credits: int,
) -> dict[str, int]:
    """Pure refill-count logic (unit-tested).

    ``need = max(stock_need, consume_need)`` so consume-replace still runs when
    idea stock is already at target, and buffer top-up still runs when no arms.
    """
    stock_need = max(0, int(target) - int(stock))
    consume_need = max(0, int(consume_credits))
    return {
        "stock_need": stock_need,
        "consume_need": consume_need,
        "need": max(stock_need, consume_need),
    }


def idea_stock_refill_triggered(
    *,
    queue: TitleQueue | None = None,
    force: bool = False,
    credits: dict[str, int] | None = None,
    credits_root: Path | None = None,
) -> tuple[bool, str]:
    """Whether this tick should attempt per-sheet stock refill.

    True when:
    - ``force``, or
    - publish_schedule is on, or
    - **≥1** title anywhere is on the public schedule / publish path, or
    - consume-replace credits remain (refill lag after a successful arm)
    """
    if force:
        return True, "force"
    if publish_schedule_enabled():
        return True, "publish_schedule"
    bal = credits if credits is not None else load_refill_credits(root=credits_root)
    if any(int(v) > 0 for v in (bal or {}).values()):
        return True, "consume_refill_credits"
    q = queue or TitleQueue()
    n = count_publish_path_titles(q)
    if n >= 1:
        return True, f"publish_path_title>={n}"
    return False, "no publish_schedule and no publish-path titles"


def idea_stock_target() -> int:
    """Target count of queued policy_ok ideas to keep **per channel** (~15)."""
    raw = (os.getenv("IDEA_STOCK_TARGET") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    sched = _schedule_cfg()
    for key in ("idea_stock_target", "human_approve_first_n", "videos_per_month_target"):
        if key in sched and sched[key] is not None:
            try:
                return max(1, int(sched[key]))
            except (TypeError, ValueError):
                continue
    beat = (_agents_cfg().get("sleep_beat") or {})
    if beat.get("min_queued_titles") is not None:
        try:
            return max(1, int(beat["min_queued_titles"]))
        except (TypeError, ValueError):
            pass
    return DEFAULT_IDEA_STOCK_TARGET


def maybe_refill_idea_stock(
    *,
    queue: TitleQueue | None = None,
    store: OpsStore | None = None,
    ledger: OpsLedger | None = None,
    dry_run: bool = False,
    force: bool = False,
    channel: str | None = None,
    credits_root: Path | None = None,
    skip_hygiene: bool = False,
) -> dict[str, Any]:
    """Harvest toward target stock **and** replace consumed public_approved arms.

    See module docstring for the locked refill rule.

    Before counting stock, runs sheet hygiene (historian What-If + blocked junk)
    so the floor (≥15 ``policy_ok`` queued empty-``job_id``) reflects clean tabs.
    """
    target = idea_stock_target()
    q = queue or TitleQueue()
    channels = [channel] if channel else list(configured_sheet_channels())

    hygiene_payload: dict[str, Any] | None = None
    if not skip_hygiene:
        try:
            from src.agents.sheet_hygiene import maybe_hygiene_idea_sheets

            hygiene_payload = maybe_hygiene_idea_sheets(
                queue=q, dry_run=dry_run, channels=list(channels)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("sheet hygiene before refill failed: %s", exc)
            hygiene_payload = {"ok": False, "error": str(exc)[:300]}

    credits = load_refill_credits(root=credits_root)
    triggered, trigger_reason = idea_stock_refill_triggered(
        queue=q, force=force, credits=credits, credits_root=credits_root
    )
    out: dict[str, Any] = {
        "publish_schedule": publish_schedule_enabled(),
        "publish_path_titles": count_publish_path_titles(q),
        "trigger": trigger_reason,
        "triggered": triggered,
        "target_per_channel": target,
        "refill_credits_before": dict(credits),
        "channels": {},
        "harvested": 0,
        "skipped": False,
        "hygiene": hygiene_payload,
        "rule": (
            "need=max(stock_deficit, consume_credits); "
            "credit +1 per successful schedule-arm; drop scheduled sheet rows; "
            "approved=FALSE; sheet stocks policy_ok only "
            "(hygiene strips scheduled-consumed + historian What-If + blocked)"
        ),
    }

    store = store or OpsStore()
    ledger = ledger or OpsLedger(store)
    any_work = False
    debit_map: dict[str, int] = {}

    if not triggered:
        out["skipped"] = True
        out["reason"] = trigger_reason
        for ch in channels:
            bal = int(credits.get(ch) or 0)
            stock = q.count_idea_stock(channel=ch)
            needs = refill_need_for_channel(
                stock=stock, target=target, consume_credits=bal
            )
            out["channels"][ch] = {
                "channel": ch,
                "stock": stock,
                "pending_ready": q.count_pending_ready(channel=ch),
                "public_approved_pending": count_public_approved_pending(q, channel=ch),
                "consume_credits": bal,
                "target": target,
                "harvested": 0,
                "skipped": True,
                "reason": trigger_reason,
                **needs,
            }
        return out

    for ch in channels:
        bal = int(credits.get(ch) or 0)
        stock = q.count_idea_stock(channel=ch)
        needs = refill_need_for_channel(
            stock=stock, target=target, consume_credits=bal
        )
        ch_out: dict[str, Any] = {
            "channel": ch,
            "stock": stock,
            "pending_ready": q.count_pending_ready(channel=ch),
            "public_approved_pending": count_public_approved_pending(q, channel=ch),
            "consume_credits": bal,
            "target": target,
            "harvested": 0,
            "skipped": False,
            "trigger": trigger_reason,
            **needs,
        }
        # Optional subset filter (SHEET_PUBLISH_CHANNELS); default = both sheets.
        if not force and not channel_in_publish_subset(ch):
            ch_out["skipped"] = True
            ch_out["reason"] = "channel not in SHEET_PUBLISH_CHANNELS"
            out["channels"][ch] = ch_out
            continue
        need = int(needs["need"])
        if need <= 0:
            ch_out["skipped"] = True
            ch_out["reason"] = (
                f"idea stock full ({stock}>={target}) and no consume credits"
            )
            out["channels"][ch] = ch_out
            continue

        any_work = True
        if dry_run:
            ch_out["skipped"] = True
            ch_out["reason"] = (
                f"dry_run — would harvest up to {need} "
                f"(stock_need={needs['stock_need']}, consume_need={needs['consume_need']})"
            )
            out["channels"][ch] = ch_out
            continue

        try:
            harvested = TrendsAgent(store, ledger, queue=q).harvest_titles(
                channel=ch, limit=need
            )
        except TypeError:
            # Older TrendsAgent without channel/limit kw — focused queue fallback
            try:
                harvested = TrendsAgent(
                    store, ledger, queue=TitleQueue(channel=ch)
                ).harvest_titles(limit=need)
            except TypeError:
                harvested = TrendsAgent(
                    store, ledger, queue=TitleQueue(channel=ch)
                ).harvest_titles()
        except Exception as exc:  # noqa: BLE001
            logger.warning("idea stock harvest failed for %s: %s", ch, exc)
            ch_out["error"] = str(exc)[:300]
            out["channels"][ch] = ch_out
            continue

        got = len(harvested)
        ch_out["harvested"] = got
        ch_out["titles"] = [h.get("title") for h in harvested[:8] if h.get("title")]
        ch_out["stock_after"] = q.count_idea_stock(channel=ch)
        debit = min(got, int(needs["consume_need"]))
        if debit:
            debit_map[ch] = debit
            ch_out["credits_debited"] = debit
        ch_out["note"] = (
            f"original titles appended to {ch} — set approved=TRUE to produce; "
            f"public_approved stays human-only"
        )
        out["harvested"] += got
        out["channels"][ch] = ch_out

    if debit_map:
        out["refill_credits_after"] = debit_consume_refills(
            debit_map, root=credits_root, dry_run=dry_run
        )
    else:
        out["refill_credits_after"] = dict(credits)

    if not any_work and all(
        (c.get("skipped") for c in out["channels"].values())
    ):
        out["skipped"] = True
        reasons = {
            ch: c.get("reason") for ch, c in out["channels"].items() if c.get("reason")
        }
        out["reason"] = reasons or "all channels skipped"
    return out
