"""Sheet hygiene — keep channel idea tabs stocked with policy_ok titles only.

napping_historian
-----------------
- Delete queued **What If** titles (wrong invent style; legacy pollution).
- Delete clearly blocked (``policy_ok=FALSE``) queued rows with no job/video —
  Rayyan wants only policy_ok visible/stocked on the sheet.

napstorian
----------
- What-If titles are correct; leave them.
- Still strip blocked queued junk with no job/video when asked.

Scheduled consume-drop (both channels)
--------------------------------------
When a title is consumed into a **scheduled** publish (sheet ``status=scheduled``
and/or ops job ``status=scheduled``), archive + remove that row from the idea
queue tab. ``job_id`` / ``video_id`` are allowed — OpsStore owns the armed job.

HOLD note hygiene (both channels)
---------------------------------
Clear **obsolete auto-HOLD notes** that no longer apply (e.g. Gate R length /
duration HOLDs after length gate removal). Release **script HOLD-exhausted**
rows that never produced ``script.json`` back to approved+queued+empty job_id
so the farm can re-pick them under the current new-format SOP.

Never wipes Rayyan inspiration notes or Live/private/farming rows.

Junk hygiene still never removes private/public/farming/… rows.

Archived rows land under ``output/ops/sheet_hygiene_archive/``.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.store import OPS_DIR
from src.agents.title_queue import TitleQueue, TitleRow, shorts_title_queue

logger = logging.getLogger(__name__)

ARCHIVE_DIR_NAME = "sheet_hygiene_archive"
HISTORIAN = "napping_historian"
NAPSTORIAN = "napstorian"

# Released script-HOLD rows get this note (auto; safe to clear later).
READY_AFTER_SCRIPT_HOLD_NOTE = (
    "ready — prior script HOLD cleared for new-format farm"
)

# Obsolete auto notes that must not keep blocking / confusing farm pickup.
_OBSOLETE_GATE_R_LENGTH_NOTE = re.compile(
    r"gate\s*r.*("
    r"length|duration|runtime|too\s*(?:short|long)|"
    r"retention[_\s-]?band|min_duration|max_duration|"
    r"below\s*min|above\s*max|target\s*band"
    r")",
    re.IGNORECASE,
)
_SCRIPT_HOLD_EXHAUSTED_NOTE = re.compile(
    r"hold\s*exhausted.*class\s*=\s*script|class\s*=\s*script.*hold\s*exhausted",
    re.IGNORECASE,
)

_PROTECTED_STATUSES = frozenset(
    {
        "private",
        "scheduled",
        "public",
        "uploading",
        "farming",
        "rendering",
        "hold",
        "failed",
    }
)


def _is_what_if_title(title: str) -> bool:
    t = re.sub(r"\s+", " ", (title or "").strip()).lower()
    return t.startswith("what if ") or t == "what if" or t.startswith("what if?")


def _safe_to_delete(row: TitleRow) -> bool:
    """True when removing the row cannot orphan a farmed / published video."""
    if (row.job_id or "").strip() or (row.video_id or "").strip():
        return False
    status = (row.status or "queued").strip().lower()
    if status in _PROTECTED_STATUSES:
        return False
    return status in ("queued", "", "rejected", "archived")


def is_obsolete_auto_hold_note(notes: str) -> bool:
    """True for auto notes that should not block pickup under current gates."""
    text = (notes or "").strip()
    if not text:
        return False
    # Preserve human / invent notes.
    low = text.lower()
    if low.startswith("original") or "smm_winner_bias" in low:
        return False
    if "refarm" in low and "hold" not in low:
        return False
    return bool(_OBSOLETE_GATE_R_LENGTH_NOTE.search(text))


def is_script_hold_exhausted_note(notes: str) -> bool:
    return bool(_SCRIPT_HOLD_EXHAUSTED_NOTE.search(notes or ""))


def _job_has_script_json(job: Any) -> bool:
    if job is None:
        return False
    job_dir = getattr(job, "job_dir", None) or ""
    if not job_dir:
        return False
    return (Path(job_dir) / "script" / "script.json").is_file()


def should_release_script_hold_exhausted(
    row: TitleRow,
    *,
    job: Any | None = None,
) -> tuple[bool, str]:
    """Release approved script-HOLD-exhausted rows with no script.json for refarm.

    Keeps ``approved=TRUE``. Clears ``job_id`` + HOLD note → ``status=queued`` so
    TitleQueue ``_ready_rows`` can pick again under new-format SOP.
    """
    status = (row.status or "").strip().lower()
    if status not in ("hold", "failed"):
        return False, ""
    if not row.approved or not row.policy_ok:
        return False, ""
    if (row.video_id or "").strip():
        return False, ""
    if not is_script_hold_exhausted_note(row.notes or ""):
        return False, ""
    # Never steal an actively farming / private / scheduled job.
    if job is not None:
        jstatus = str(getattr(job, "status", "") or "").strip().lower()
        if jstatus in ("farming", "private", "scheduled", "public", "uploading"):
            return False, "job_still_active"
        meta = dict(getattr(job, "meta", None) or {})
        if meta.get("live_featured") or meta.get("vod_loop_featured"):
            return False, "live_featured"
    if _job_has_script_json(job):
        # Has script — prefer resume / human decision, not blind refarm.
        return False, "has_script_json"
    return True, "script_hold_exhausted_no_script"


def should_clear_obsolete_hold_note(
    row: TitleRow,
    *,
    job: Any | None = None,
) -> tuple[bool, str]:
    """Clear obsolete Gate R length HOLDs so rows can return to queued."""
    if not is_obsolete_auto_hold_note(row.notes or ""):
        return False, ""
    status = (row.status or "").strip().lower()
    if status not in ("hold", "failed", "queued", ""):
        return False, ""
    if job is not None:
        jstatus = str(getattr(job, "status", "") or "").strip().lower()
        if jstatus in ("farming", "private", "scheduled", "public", "uploading"):
            return False, "job_still_active"
    return True, "obsolete_gate_r_length_note"


def should_remove_row(row: TitleRow, *, channel: str) -> tuple[bool, str]:
    """Return (remove?, reason) for one sheet row (junk hygiene only)."""
    ch = (channel or row.channel or "").strip().lower()
    if not _safe_to_delete(row):
        return False, ""

    if ch == HISTORIAN and _is_what_if_title(row.title):
        return True, "historian_what_if"

    if not bool(row.policy_ok):
        return True, "policy_blocked"

    return False, ""


def scheduled_job_ids_from_store(store: Any | None = None) -> set[str]:
    """Ops job ids currently ``status=scheduled`` (publishAt armed)."""
    if store is None:
        try:
            from src.agents.store import OpsStore

            store = OpsStore()
        except Exception:  # noqa: BLE001
            return set()
    out: set[str] = set()
    try:
        jobs = store.list_jobs(status="scheduled")
    except TypeError:
        jobs = [j for j in store.list_jobs() if (j.status or "") == "scheduled"]
    except Exception:  # noqa: BLE001
        return set()
    for j in jobs or []:
        jid = str(getattr(j, "id", "") or "").strip()
        if jid:
            out.add(jid)
    return out


def should_drop_scheduled_consumed(
    row: TitleRow,
    *,
    scheduled_job_ids: set[str] | None = None,
) -> tuple[bool, str]:
    """Drop rule: title consumed into a scheduled publish (both channels).

    Remove when:
    - sheet ``status == scheduled``, or
    - ``job_id`` matches an ops job with ``status == scheduled``.

    Safe with ``job_id`` / ``video_id`` — OpsStore retains the armed job.
    Does **not** drop ``public`` / in-flight farm rows unless the ops job is
    scheduled (desync repair).
    """
    status = (row.status or "").strip().lower()
    if status == "scheduled":
        return True, "sheet_status_scheduled"
    jid = (row.job_id or "").strip()
    if jid and scheduled_job_ids and jid in scheduled_job_ids:
        return True, "ops_job_scheduled"
    return False, ""


def drop_scheduled_consumed_rows(
    *,
    queue: TitleQueue | None = None,
    store: Any | None = None,
    dry_run: bool = False,
    channels: list[str] | None = None,
    archive_root: Path | None = None,
    credit_refills: bool = True,
    credits_root: Path | None = None,
    scheduled_job_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Archive + remove scheduled-consumed idea rows; optionally credit invent.

    Used on */10 (via hygiene → refill) and after successful schedule-arm.
    When ``credit_refills`` is True, each dropped row credits +1 consume-replace
    invent for that channel (orphan catch-up). Arm path passes False because it
    already credited via ``credit_consume_refills``.
    """
    q = queue or TitleQueue()
    chans = list(channels) if channels else [NAPSTORIAN, HISTORIAN]
    sched_ids = (
        scheduled_job_ids
        if scheduled_job_ids is not None
        else scheduled_job_ids_from_store(store)
    )

    out: dict[str, Any] = {
        "channels": {},
        "removed": 0,
        "by_channel": {},
        "dry_run": dry_run,
        "credit_refills": bool(credit_refills),
        "rule": (
            "drop sheet rows when status=scheduled or ops job status=scheduled; "
            "archive under sheet_hygiene_archive; refill via consume credits"
        ),
    }
    credit_map: dict[str, int] = {}

    for ch in chans:
        rows = q.list_rows(channel=ch)
        keep: list[TitleRow] = []
        removed: list[dict[str, Any]] = []
        reasons: dict[str, int] = {}
        for r in rows:
            drop, reason = should_drop_scheduled_consumed(
                r, scheduled_job_ids=sched_ids
            )
            if drop:
                payload = r.as_dict()
                payload["hygiene_reason"] = reason
                removed.append(payload)
                reasons[reason] = int(reasons.get(reason) or 0) + 1
            else:
                keep.append(r)

        ch_out: dict[str, Any] = {
            "channel": ch,
            "before": len(rows),
            "after": len(rows) - len(removed),
            "removed": len(removed),
            "reasons": reasons,
            "dry_run": dry_run,
            "titles_removed": [x.get("title") for x in removed[:40]],
        }
        if not removed:
            ch_out["skipped"] = True
            ch_out["reason"] = "no scheduled-consumed rows"
            out["channels"][ch] = ch_out
            continue

        if dry_run:
            ch_out["skipped"] = True
            ch_out["reason"] = f"dry_run — would drop {len(removed)}"
            out["channels"][ch] = ch_out
            out["removed"] += len(removed)
            out["by_channel"][ch] = len(removed)
            continue

        q.replace_channel_rows(ch, keep)
        arch = _write_archive(channel=ch, removed=removed, root=archive_root)
        if arch is not None:
            ch_out["archive"] = str(arch)
        out["channels"][ch] = ch_out
        out["removed"] += len(removed)
        out["by_channel"][ch] = len(removed)
        credit_map[ch] = int(credit_map.get(ch) or 0) + len(removed)
        logger.info(
            "scheduled drop %s: removed %d kept %d reasons=%s",
            ch,
            len(removed),
            len(keep),
            reasons,
        )

    if credit_refills and credit_map and not dry_run:
        try:
            from src.agents.idea_stock import credit_consume_refills

            out["refill_credits"] = credit_consume_refills(
                credit_map, root=credits_root, dry_run=False
            )
            out["credits_added"] = dict(credit_map)
        except Exception as exc:  # noqa: BLE001
            logger.warning("scheduled-drop refill credit failed: %s", exc)
            out["refill_credits_error"] = str(exc)[:200]
    elif credit_refills and credit_map and dry_run:
        out["credits_added_note"] = (
            "dry_run — would credit "
            + ", ".join(f"{k}+{v}" for k, v in sorted(credit_map.items()))
        )

    return out


def archive_drop_sheet_row(
    queue: TitleQueue,
    row: TitleRow,
    *,
    channel: str | None = None,
    extra: dict[str, Any] | None = None,
    archive_root: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Archive + remove one sheet row (schedule-arm consume path)."""
    ch = (channel or row.channel or "").strip()
    if not ch:
        return {"ok": False, "reason": "missing channel"}
    payload = row.as_dict()
    if extra:
        payload.update(extra)
    payload.setdefault("hygiene_reason", "schedule_arm")
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "channel": ch,
            "removed": 1,
            "title": row.title,
        }
    keep = [
        r
        for r in queue.list_rows(channel=ch)
        if not (
            r.row_index == row.row_index
            or (
                (row.job_id or "").strip()
                and (r.job_id or "").strip() == (row.job_id or "").strip()
            )
        )
    ]
    queue.replace_channel_rows(ch, keep)
    arch = _write_archive(channel=ch, removed=[payload], root=archive_root)
    return {
        "ok": True,
        "channel": ch,
        "removed": 1,
        "title": row.title,
        "archive": str(arch) if arch else None,
        "kept": len(keep),
    }


def archive_path(*, root: Path | None = None) -> Path:
    return (root or OPS_DIR) / ARCHIVE_DIR_NAME


def _write_archive(
    *,
    channel: str,
    removed: list[dict[str, Any]],
    root: Path | None = None,
) -> Path | None:
    if not removed:
        return None
    dest_dir = archive_path(root=root)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = dest_dir / f"{channel}_{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "channel": channel,
                "archived_at": datetime.now(timezone.utc).isoformat(),
                "count": len(removed),
                "rows": removed,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def hygiene_channel_sheet(
    channel: str,
    *,
    queue: TitleQueue | None = None,
    dry_run: bool = False,
    archive_root: Path | None = None,
) -> dict[str, Any]:
    """Strip unsafe/wrong-style junk from one channel tab (+ CSV mirror)."""
    ch = (channel or "").strip()
    q = queue or TitleQueue()
    rows = q.list_rows(channel=ch)
    keep: list[TitleRow] = []
    removed: list[dict[str, Any]] = []
    reasons: dict[str, int] = {}

    for r in rows:
        drop, reason = should_remove_row(r, channel=ch)
        if drop:
            payload = r.as_dict()
            payload["hygiene_reason"] = reason
            removed.append(payload)
            reasons[reason] = int(reasons.get(reason) or 0) + 1
        else:
            keep.append(r)

    out: dict[str, Any] = {
        "channel": ch,
        "before": len(rows),
        "after": len(keep),
        "removed": len(removed),
        "reasons": reasons,
        "what_if_removed": int(reasons.get("historian_what_if") or 0),
        "policy_blocked_removed": int(reasons.get("policy_blocked") or 0),
        "dry_run": dry_run,
        "titles_removed": [x.get("title") for x in removed[:40]],
    }

    if not removed:
        out["skipped"] = True
        out["reason"] = "nothing to remove"
        return out

    if dry_run:
        out["skipped"] = True
        out["reason"] = f"dry_run — would remove {len(removed)}"
        return out

    q.replace_channel_rows(ch, keep)
    arch = _write_archive(channel=ch, removed=removed, root=archive_root)
    if arch is not None:
        out["archive"] = str(arch)
    logger.info(
        "sheet hygiene %s: removed %d (what_if=%s blocked=%s) kept %d",
        ch,
        len(removed),
        out["what_if_removed"],
        out["policy_blocked_removed"],
        len(keep),
    )
    return out


def hygiene_hold_notes_channel(
    channel: str,
    *,
    queue: TitleQueue | None = None,
    store: Any | None = None,
    dry_run: bool = False,
    archive_root: Path | None = None,
) -> dict[str, Any]:
    """Clear obsolete auto-HOLD notes; release script-HOLD-exhausted for refarm.

    Does **not** wipe inspiration notes or touch Live/private/farming rows.
    Supersedes old ops jobs (meta only) so resume will not reclaim released titles.
    """
    ch = (channel or "").strip()
    q = queue or TitleQueue()
    if store is None:
        try:
            from src.agents.store import OpsStore

            store = OpsStore()
        except Exception:  # noqa: BLE001
            store = None

    rows = q.list_rows(channel=ch)
    actions: list[dict[str, Any]] = []
    mutated = False
    reasons: dict[str, int] = {}

    for r in rows:
        job = None
        jid = (r.job_id or "").strip()
        if jid and store is not None:
            try:
                job = store.get_job(jid)
            except Exception:  # noqa: BLE001
                job = None

        release, rel_reason = should_release_script_hold_exhausted(r, job=job)
        clear, clear_reason = should_clear_obsolete_hold_note(r, job=job)
        if not release and not clear:
            continue

        action = {
            "row_index": r.row_index,
            "title": r.title,
            "job_id": jid,
            "old_status": r.status,
            "old_notes": (r.notes or "")[:240],
            "reason": rel_reason or clear_reason,
        }
        if release:
            action["action"] = "release_script_hold_exhausted"
            if not dry_run:
                if jid and store is not None and job is not None:
                    meta = dict(job.meta or {})
                    meta["superseded_by_refarm"] = True
                    meta["superseded_at"] = datetime.now(timezone.utc).isoformat()
                    meta["resumable"] = False
                    meta["repair_blocked"] = True
                    try:
                        store.update_job(
                            jid,
                            status="hold",
                            stage="hold",
                            error=(
                                "superseded — sheet title released for new-format "
                                "refarm after script HOLD exhausted"
                            ),
                            meta=meta,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "hold hygiene: could not supersede job %s: %s", jid, exc
                        )
                r.job_id = ""
                r.status = "queued"
                r.notes = READY_AFTER_SCRIPT_HOLD_NOTE
                mutated = True
        elif clear:
            action["action"] = "clear_obsolete_gate_r_note"
            if not dry_run:
                r.notes = ""
                if (r.status or "").strip().lower() in ("hold", "failed") and not jid:
                    r.status = "queued"
                elif (r.status or "").strip().lower() in ("hold", "failed") and jid:
                    # Job still linked — only clear the obsolete note text.
                    pass
                mutated = True
        actions.append(action)
        reasons[action["reason"]] = int(reasons.get(action["reason"]) or 0) + 1

    out: dict[str, Any] = {
        "channel": ch,
        "actions": len(actions),
        "reasons": reasons,
        "released": sum(
            1 for a in actions if a.get("action") == "release_script_hold_exhausted"
        ),
        "cleared_notes": sum(
            1 for a in actions if a.get("action") == "clear_obsolete_gate_r_note"
        ),
        "dry_run": dry_run,
        "titles": [a.get("title") for a in actions[:40]],
        "detail": actions[:40],
    }

    if not actions:
        out["skipped"] = True
        out["reason"] = "no obsolete HOLD notes"
        return out

    if dry_run:
        out["skipped"] = True
        out["reason"] = f"dry_run — would act on {len(actions)}"
        return out

    if mutated:
        q.replace_channel_rows(ch, rows)
        arch = _write_archive(
            channel=f"{ch}_hold_hygiene",
            removed=actions,
            root=archive_root,
        )
        if arch:
            out["archive"] = str(arch)
        logger.info(
            "sheet hold hygiene %s: released=%s cleared_notes=%s",
            ch,
            out["released"],
            out["cleared_notes"],
        )
    return out


def maybe_hygiene_idea_sheets(
    *,
    queue: TitleQueue | None = None,
    store: Any | None = None,
    dry_run: bool = False,
    channels: list[str] | None = None,
    archive_root: Path | None = None,
    credit_scheduled_refills: bool = True,
    credits_root: Path | None = None,
) -> dict[str, Any]:
    """Run scheduled consume-drop then junk + HOLD-note hygiene (both channels)."""
    q = queue or TitleQueue()
    chans = list(channels) if channels else [HISTORIAN, NAPSTORIAN]
    # Historian first — that is where legacy What-If pollution lives.
    ordered: list[str] = []
    for prefer in (HISTORIAN, NAPSTORIAN):
        if prefer in chans and prefer not in ordered:
            ordered.append(prefer)
    for ch in chans:
        if ch not in ordered:
            ordered.append(ch)

    scheduled_drop = drop_scheduled_consumed_rows(
        queue=q,
        store=store,
        dry_run=dry_run,
        channels=list(ordered),
        archive_root=archive_root,
        credit_refills=credit_scheduled_refills,
        credits_root=credits_root,
    )

    out: dict[str, Any] = {
        "channels": {},
        "what_if_removed": 0,
        "policy_blocked_removed": 0,
        "scheduled_removed": int(scheduled_drop.get("removed") or 0),
        "removed": int(scheduled_drop.get("removed") or 0),
        "hold_released": 0,
        "hold_notes_cleared": 0,
        "dry_run": dry_run,
        "scheduled_drop": scheduled_drop,
        "hold_hygiene": {},
    }
    for ch in ordered:
        ch_out = hygiene_channel_sheet(
            ch, queue=q, dry_run=dry_run, archive_root=archive_root
        )
        hold_out = hygiene_hold_notes_channel(
            ch, queue=q, store=store, dry_run=dry_run, archive_root=archive_root
        )
        out["hold_hygiene"][ch] = hold_out
        out["hold_released"] += int(hold_out.get("released") or 0)
        out["hold_notes_cleared"] += int(hold_out.get("cleared_notes") or 0)
        # Merge scheduled-drop counts into per-channel view when present.
        sched_ch = (scheduled_drop.get("channels") or {}).get(ch) or {}
        if sched_ch.get("removed"):
            ch_out = {
                **ch_out,
                "scheduled_removed": int(sched_ch.get("removed") or 0),
                "scheduled_reasons": sched_ch.get("reasons") or {},
            }
        ch_out["hold_hygiene"] = {
            "released": hold_out.get("released") or 0,
            "cleared_notes": hold_out.get("cleared_notes") or 0,
            "reasons": hold_out.get("reasons") or {},
        }
        out["channels"][ch] = ch_out
        out["what_if_removed"] += int(ch_out.get("what_if_removed") or 0)
        out["policy_blocked_removed"] += int(ch_out.get("policy_blocked_removed") or 0)
        out["removed"] += int(ch_out.get("removed") or 0)
    try:
        out["shorts_cascade"] = cascade_shorts_from_longform(queue=q, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        out["shorts_cascade"] = {"ok": False, "error": str(exc)[:300]}
    return out


def _match_shorts_rows(lf: TitleRow, shorts_rows: list[TitleRow]) -> list[TitleRow]:
    pj = (lf.job_id or "").strip()
    pv = (lf.video_id or "").strip()
    out: list[TitleRow] = []
    for r in shorts_rows:
        if pj and (r.parent_job_id == pj or r.job_id == pj):
            out.append(r)
        elif pv and (r.parent_video_id == pv):
            out.append(r)
    return out


def _match_shorts_row(lf: TitleRow, shorts_rows: list[TitleRow]) -> TitleRow | None:
    rows = _match_shorts_rows(lf, shorts_rows)
    return rows[0] if rows else None


def cascade_shorts_from_longform(
    *,
    queue: TitleQueue | None = None,
    channels: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Escalate longform approved onto sibling Shorts rows.

    Shorts are auto-public: ``public_approved`` is always TRUE and is never
    demoted to FALSE. Longform TRUE still copies onto Shorts ``approved``.
    Longform tabs are not written.
    """
    from src.agents.sheet_channels import configured_sheet_channels, shorts_tab_name

    q = queue or TitleQueue()
    chans = list(channels) if channels else list(q.channels or configured_sheet_channels())
    updated = 0
    held = 0
    per: dict[str, Any] = {}
    for ch in chans:
        lf_rows = q.list_rows(channel=ch)
        tab = shorts_tab_name(ch)
        sq = shorts_title_queue(ch)
        s_rows = sq.list_rows(channel=tab)
        if not s_rows:
            per[ch] = {"updated": 0, "shorts_rows": 0}
            continue
        n = 0
        n_hold = 0
        dirty = False
        for lf in lf_rows:
            matches = _match_shorts_rows(lf, s_rows)
            if not matches:
                continue
            lf_status = (lf.status or "").strip().lower()
            lf_approved = bool(lf.approved) or lf_status in {
                "private",
                "scheduled",
                "public",
            }
            for sr in matches:
                # One-way escalate from longform; never strip a Shorts TRUE.
                # Shorts are auto-public — never demote public_approved.
                want_approved = bool(sr.approved) or lf_approved
                want_public = True
                new_status = sr.status
                st = (sr.status or "").strip().lower()
                if want_approved and st in {"hold", ""}:
                    new_status = "queued"
                elif want_approved:
                    new_status = sr.status
                elif not want_approved and st in {"", "queued"}:
                    new_status = "queued"
                if not want_approved:
                    n_hold += 1
                changed = (
                    sr.approved != want_approved
                    or sr.public_approved != want_public
                    or sr.status != new_status
                )
                if not (sr.parent_job_id or "").strip() and lf.job_id:
                    sr.parent_job_id = lf.job_id
                    sr.job_id = sr.job_id or lf.job_id
                    changed = True
                if not (sr.parent_video_id or "").strip() and lf.video_id:
                    sr.parent_video_id = lf.video_id
                    changed = True
                if not (sr.parent_title or "").strip():
                    sr.parent_title = lf.title
                    changed = True
                if not changed:
                    continue
                sr.approved = want_approved
                sr.public_approved = want_public
                sr.status = new_status
                dirty = True
                n += 1
        if dirty and not dry_run:
            sq.replace_channel_rows(tab, s_rows)
        updated += n
        held += n_hold
        per[ch] = {"updated": n, "held": n_hold, "shorts_rows": len(s_rows)}
    return {"ok": True, "updated": updated, "held": held, "dry_run": dry_run, "channels": per}
