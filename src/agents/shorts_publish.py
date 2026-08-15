"""Pack + Gate S + private YouTube Shorts upload; always promote public."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from src.agents.sheet_channels import shorts_tab_name
from src.agents.store import OpsStore
from src.agents.title_queue import TitleRow, shorts_title_queue
from src.content.derivatives import shorts_clip_allowed
from src.services.gate_shorts import run_gate_s
from src.services.settings import ROOT
from src.services.shorts_packager import (
    pack_short_clip,
    resolve_final_mp4,
    shorts_output_path,
)
from src.services.youtube_channel_auth import normalize_youtube_channel

logger = logging.getLogger(__name__)

MODULE_ID = "youtube_shorts_clip"

_YT_ID_RE = re.compile(r"^[\w-]{11}$")
_STOP = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "of",
        "in",
        "on",
        "to",
        "if",
        "what",
        "why",
        "how",
        "had",
        "have",
        "for",
        "with",
    }
)


def shorts_description(
    *,
    title: str,
    hook: str = "",
) -> str:
    """Shorts description: title + optional hook + #Shorts (no longform URL)."""
    lines = [title.strip()]
    hook_s = (hook or "").strip()
    if hook_s and hook_s.lower() != title.strip().lower():
        lines.append(hook_s)
    lines.append("")
    lines.append("#Shorts")
    return "\n".join(lines)


def _job_channel(job: Any) -> str:
    meta = job.meta or {}
    raw = meta.get("channel") or meta.get("sheet_tab") or meta.get("youtube_channel") or ""
    try:
        return normalize_youtube_channel(str(raw) if raw else None)
    except Exception:  # noqa: BLE001
        return str(raw or "").strip().lower() or ""


def _is_shortish_title(title: str) -> bool:
    t = (title or "").lower()
    return "#short" in t or t.startswith("shorts:")


def _tokens(text: str) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-z0-9']+", (text or "").lower())
        if w not in _STOP and len(w) > 1
    }


def list_public_longform_videos(
    channel: str,
    *,
    store: OpsStore | None = None,
) -> list[dict[str, str]]:
    """Public longform videos on this channel (ops jobs + live_vods meta)."""
    store = store or OpsStore()
    want = normalize_youtube_channel(channel)
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(vid: str, title: str, sort_key: str) -> None:
        vid = (vid or "").strip()
        if not vid or vid in seen or not _YT_ID_RE.match(vid):
            return
        if _is_shortish_title(title):
            return
        seen.add(vid)
        out.append({"video_id": vid, "title": (title or "").strip(), "sort_key": sort_key})

    for job in store.list_jobs():
        if _job_channel(job) != want:
            continue
        if (job.status or "").strip().lower() != "public":
            continue
        vid = (job.video_id or "").strip()
        _add(vid, job.title or "", str(job.updated_at or job.created_at or ""))

    live_root = ROOT / "output" / "live_vods" / want
    if live_root.is_dir():
        for d in live_root.iterdir():
            if not d.is_dir():
                continue
            meta_path = d / "meta.json"
            title = ""
            vid = d.name if _YT_ID_RE.match(d.name) else ""
            updated = ""
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    meta = {}
                if isinstance(meta, dict):
                    title = str(meta.get("title") or "").strip()
                    vid = str(meta.get("video_id") or vid).strip()
                    updated = str(meta.get("updated_at") or "")
            _add(vid, title or vid, updated)

    out.sort(key=lambda r: r.get("sort_key") or "", reverse=True)
    return out


def _best_title_match(
    query: str, catalog: list[dict[str, str]]
) -> dict[str, str] | None:
    qtok = _tokens(query)
    if not qtok or not catalog:
        return None
    qlow = (query or "").strip().lower()
    best: dict[str, str] | None = None
    best_score = 0.0
    for item in catalog:
        title = item.get("title") or ""
        tlow = title.lower()
        ttok = _tokens(title)
        if not ttok:
            continue
        overlap = len(qtok & ttok) / max(1, len(qtok))
        if qlow and (qlow in tlow or tlow in qlow):
            overlap = max(overlap, 0.9)
        if overlap > best_score:
            best_score = overlap
            best = item
    if best is None or best_score < 0.34:
        return None
    return best


def resolve_shorts_cta(
    row: TitleRow,
    *,
    channel: str,
    store: OpsStore | None = None,
) -> dict[str, str]:
    """Pick the longform YouTube video for this Short's Related Video link.

    Prefer the bound parent id, then a title match against public longforms,
    else the latest public long video on the channel.
    """
    store = store or OpsStore()
    catalog = list_public_longform_videos(channel, store=store)
    parent_vid = (row.parent_video_id or "").strip()
    parent_title = (row.parent_title or "").strip()

    if parent_vid and _YT_ID_RE.match(parent_vid):
        title = parent_title
        for item in catalog:
            if item["video_id"] == parent_vid:
                title = item.get("title") or title
                break
        return {
            "video_id": parent_vid,
            "title": title,
            "kind": "matched",
        }

    query = parent_title or (row.title or "")
    hit = _best_title_match(query, catalog)
    if hit:
        return {
            "video_id": hit["video_id"],
            "title": hit.get("title") or query,
            "kind": "matched",
        }
    if catalog:
        latest = catalog[0]
        return {
            "video_id": latest["video_id"],
            "title": latest.get("title") or "",
            "kind": "latest",
        }
    return {"video_id": "", "title": parent_title, "kind": "none"}


def _parent_job_dir(row: TitleRow, store: OpsStore) -> Path | None:
    jid = (row.parent_job_id or row.job_id or "").strip()
    if not jid:
        return None
    job = store.get_job(jid) if hasattr(store, "get_job") else None
    if job is None:
        for j in store.list_jobs():
            if j.id == jid:
                job = j
                break
    if job is None or not job.job_dir:
        return None
    p = Path(job.job_dir)
    return p if p.is_dir() else None


def _window_from_row(row: TitleRow) -> tuple[int, float]:
    try:
        wi = int(str(row.window_index or "0").strip() or 0)
    except ValueError:
        wi = 0
    start = 0.0
    m = re.search(r"start=(\d+(?:\.\d+)?)", row.notes or "")
    if m:
        start = float(m.group(1))
    return wi, start


def pack_and_gate(
    job_dir: Path | str,
    *,
    layout: str = "blur_pillar",
    window_index: int = 0,
    start_s: float | None = None,
    final_path: Path | str | None = None,
    channel: str | None = None,
    video_id: str | None = None,
) -> dict[str, Any]:
    src = final_path or resolve_final_mp4(job_dir, channel=channel, video_id=video_id)
    out = shorts_output_path(job_dir, window_index=int(window_index))
    packed = pack_short_clip(
        job_dir,
        layout=layout,  # type: ignore[arg-type]
        out_path=out,
        final_path=src,
        start_s=start_s,
    )
    gate = run_gate_s(packed["out_path"])
    packed["gate_s"] = gate.model_dump()
    packed["gate_s_ok"] = gate.ok
    packed["window_index"] = int(window_index)
    if not gate.ok:
        packed["ok"] = False
        packed["errors"] = list(gate.errors)
    return packed


def publish_short_for_row(
    row: TitleRow,
    *,
    channel: str,
    store: OpsStore | None = None,
    dry_run: bool = True,
    allow_video_reuse: bool = False,
    layout: str = "blur_pillar",
) -> dict[str, Any]:
    """Pack if needed, Gate S, private upload; always promote public."""
    if not shorts_clip_allowed(allow_video_reuse=allow_video_reuse):
        return {
            "ok": False,
            "skipped": True,
            "reason": "youtube_shorts_clip frozen (pass --allow-video-reuse or smm.allow_youtube_shorts_clip)",
        }
    store = store or OpsStore()
    ch = normalize_youtube_channel(channel)
    wi, start_s = _window_from_row(row)
    parent_vid = (row.parent_video_id or "").strip()
    job_dir = _parent_job_dir(row, store)
    src = resolve_final_mp4(job_dir, channel=ch, video_id=parent_vid)
    if job_dir is None and src is not None:
        job_dir = src.parent
    if job_dir is None:
        return {"ok": False, "error": "parent job_dir missing", "parent_job_id": row.parent_job_id}
    if src is None:
        return {"ok": False, "error": "parent final.mp4 missing", "job_dir": str(job_dir)}

    out = shorts_output_path(job_dir, window_index=wi)
    if not out.is_file() or out.stat().st_size < 1000:
        packed = pack_and_gate(
            job_dir,
            layout=layout,
            window_index=wi,
            start_s=start_s,
            final_path=src,
            channel=ch,
            video_id=parent_vid,
        )
        if not packed.get("gate_s_ok"):
            return {"ok": False, "stage": "gate_s", **packed}
        out = Path(packed["out_path"])
    else:
        gate = run_gate_s(out)
        if not gate.ok:
            return {"ok": False, "stage": "gate_s", "errors": list(gate.errors)}

    hook = ""
    script_path = job_dir / "script" / "script.json"
    title = (row.title or "").strip() or "Short"
    cta = resolve_shorts_cta(row, channel=ch, store=store)
    related_vid = (cta.get("video_id") or "").strip()
    desc = shorts_description(title=title, hook=hook)

    from src.services.publish_youtube import PublishModule, PublishModuleError

    pub = PublishModule(channel=ch)
    try:
        result = pub.publish_private(
            final_path=out,
            title=title if "#shorts" in title.lower() else f"{title} #Shorts",
            description=desc,
            script_path=script_path if script_path.is_file() else None,
            job_dir=job_dir,
            channel=ch,
            dry_run=dry_run,
            allow_short=True,
            shorts_mode=True,
            related_video_id=related_vid or None,
        )
    except PublishModuleError as exc:
        return {"ok": False, "stage": "upload", "error": str(exc)[:500]}

    payload: dict[str, Any] = {
        "ok": bool(result.ok),
        "dry_run": dry_run,
        "short_video_id": result.video_id,
        "watch_url": result.watch_url,
        "privacy": result.privacy_status,
        "title": result.title,
        "out_path": str(out),
        "parent_video_id": parent_vid or related_vid or "",
        "related_video_id": related_vid or "",
        "parent_job_id": row.parent_job_id or row.job_id,
        "cta": cta,
        "manifest": result.publish_manifest_path,
    }

    want_public = True

    if want_public and result.video_id and not dry_run:
        try:
            pub.promote_public(video_id=result.video_id, force=True)
            payload["privacy"] = "public"
            payload["promoted_public"] = True
        except Exception as exc:  # noqa: BLE001
            payload["promote_error"] = str(exc)[:300]
            payload["promoted_public"] = False
    elif want_public and dry_run:
        payload["promoted_public"] = False
        payload["promote_dry_run"] = True

    return payload


def _ensure_related_video_on_existing_short(
    row: TitleRow,
    *,
    channel: str,
    store: OpsStore | None = None,
    dry_run: bool = True,
    pub: Any | None = None,
) -> dict[str, Any] | None:
    """Patch Related Video on an already-uploaded Short when CTA resolves."""
    short_vid = (row.short_video_id or row.video_id or "").strip()
    if not short_vid:
        return None
    cta = resolve_shorts_cta(row, channel=channel, store=store)
    related_vid = (cta.get("video_id") or "").strip()
    if not related_vid:
        return {"ok": True, "skipped": True, "reason": "no related video target"}
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "short_video_id": short_vid,
            "related_video_id": related_vid,
        }
    from src.services.publish_youtube import PublishModule

    publisher = pub or PublishModule(channel=channel)
    try:
        return publisher.set_shorts_related_video(short_vid, related_vid)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300], "short_video_id": short_vid}


def publish_ready_shorts(
    *,
    channel: str | None = None,
    store: OpsStore | None = None,
    dry_run: bool = True,
    allow_video_reuse: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Pack/upload Shorts rows that inherited longform approved."""
    from src.agents.channel_empire import production_channel_names
    from src.agents.sheet_hygiene import cascade_shorts_from_longform

    store = store or OpsStore()
    cascade_shorts_from_longform(dry_run=False)
    chans = [normalize_youtube_channel(channel)] if channel else list(production_channel_names())
    results: list[dict[str, Any]] = []
    n = 0
    for ch in chans:
        tab = shorts_tab_name(ch)
        sq = shorts_title_queue(ch)
        rows = sq.list_rows(channel=tab)
        dirty = False
        for row in rows:
            if limit is not None and n >= int(limit):
                break
            if not row.approved or not row.policy_ok:
                continue
            if (row.status or "").lower() in {"public", "failed"}:
                continue
            has_id = bool((row.short_video_id or row.video_id or "").strip())
            if has_id and (row.status or "").lower() != "public":
                vid = (row.short_video_id or row.video_id).strip()
                if not dry_run:
                    try:
                        from src.services.publish_youtube import PublishModule

                        pub = PublishModule(channel=ch)
                        pub.promote_public(video_id=vid, force=True)
                        _ensure_related_video_on_existing_short(
                            row, channel=ch, store=store, dry_run=False, pub=pub
                        )
                        row.status = "public"
                        row.public_approved = True
                        dirty = True
                        n += 1
                        results.append(
                            {"ok": True, "channel": ch, "promoted_existing": vid}
                        )
                    except Exception as exc:  # noqa: BLE001
                        results.append({"ok": False, "channel": ch, "error": str(exc)[:200]})
                else:
                    results.append({"ok": True, "channel": ch, "promote_dry_run": vid})
                    n += 1
                continue
            if has_id:
                _ensure_related_video_on_existing_short(
                    row, channel=ch, store=store, dry_run=dry_run
                )
                continue
            out = publish_short_for_row(
                row,
                channel=ch,
                store=store,
                dry_run=dry_run,
                allow_video_reuse=allow_video_reuse,
            )
            out["channel"] = ch
            results.append(out)
            n += 1
            if out.get("ok") and not dry_run:
                vid = str(out.get("short_video_id") or "")
                if vid:
                    row.short_video_id = vid
                    row.video_id = vid
                row.public_approved = True
                row.status = "public" if out.get("privacy") == "public" else "private"
                dirty = True
        if dirty and not dry_run:
            sq.replace_channel_rows(tab, rows)
        if limit is not None and n >= int(limit):
            break
    return {
        "ok": True,
        "dry_run": dry_run,
        "n": n,
        "results": results,
        "allowed": shorts_clip_allowed(allow_video_reuse=allow_video_reuse),
        "module": MODULE_ID,
    }
