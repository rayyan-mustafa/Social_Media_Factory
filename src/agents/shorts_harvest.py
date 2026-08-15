"""Harvest Shorts titles from owned longform — never a second topic factory.

Each Shorts row is bound to a parent job/video (+ optional extra trailer windows
on long historian docs). Titles are short click-hooks, not full script hooks.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from src.agents.sheet_channels import shorts_tab_name
from src.agents.store import JobRecord, OpsStore
from src.agents.title_queue import TitleQueue, TitleRow, shorts_title_queue
from src.services.settings import ROOT
from src.services.shorts_packager import (
    load_scene_durations,
    probe_media_duration_s,
    resolve_final_mp4,
    trailer_window_starts,
)
from src.services.youtube_channel_auth import normalize_youtube_channel

logger = logging.getLogger(__name__)

SHORTS_TITLE_MAX_CHARS = 40
SHORTS_TITLE_MAX_WORDS = 8
_TITLE_STOP = frozenset(
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
        "for",
        "with",
        "but",
    }
)
_TWIST_START = re.compile(
    r"\b(never|died|won|fell|formed|took|conquered|changed|succeeded|"
    r"outlived|rose|crushed|established|shattered|seized|avoided|collapsed|"
    r"survived|ended|embraced|invaded|led|developed|launched|retained|"
    r"enforced|secured|broke|dominated|expanded|remained|fragmented|"
    r"escalated|declared|endured|ruled|continued|executed|reigned|"
    r"tolerated|chose|chosen|silenced|haunts|haunt|vanished|rejected|"
    r"banished|rewrote|doomed|still|had|have)\b",
    re.I,
)


def _job_channel(job: JobRecord) -> str:
    meta = job.meta or {}
    raw = meta.get("channel") or meta.get("sheet_tab") or meta.get("youtube_channel") or ""
    try:
        return normalize_youtube_channel(str(raw) if raw else None)
    except Exception:  # noqa: BLE001
        return str(raw or "").strip().lower() or "napstorian"


def load_parent_script(job_dir: Path | str | None) -> dict[str, Any]:
    if not job_dir:
        return {}
    path = Path(job_dir) / "script" / "script.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _normalize_apostrophes(text: str) -> str:
    return (
        (text or "")
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u02bc", "'")
    )


def _clip_words(text: str, n: int, max_chars: int) -> str:
    words = re.findall(r"[A-Za-z0-9']+", _normalize_apostrophes(text))
    out: list[str] = []
    for w in words:
        trial = " ".join(out + [w])
        if out and (len(out) >= n or len(trial) > max_chars):
            break
        out.append(w)
    return " ".join(out)


def _short_name(entity: str) -> str:
    words = re.findall(r"[A-Za-z0-9']+", _normalize_apostrophes(entity))
    if not words:
        return (entity or "").strip() or "history"
    if len(words) >= 3 and words[1].lower() == "of":
        return " ".join(words[:3])
    if len(words) >= 2 and words[1].upper() in {
        "I",
        "II",
        "III",
        "IV",
        "V",
        "VI",
        "VII",
        "VIII",
        "IX",
    }:
        return " ".join(words[:2])
    return " ".join(words[:2]) if len(words) >= 2 else words[0]


def _bare_entity(entity: str) -> str:
    return re.sub(r"^(the|a|an)\s+", "", entity or "", flags=re.I).strip()


def _content_words(text: str) -> list[str]:
    return [
        w
        for w in re.findall(r"[A-Za-z0-9']+", text or "")
        if w.lower() not in _TITLE_STOP
    ]


def _trim_dangling(text: str) -> str:
    dangling = {
        "into",
        "in",
        "of",
        "on",
        "at",
        "from",
        "with",
        "for",
        "to",
        "the",
        "a",
        "an",
        "and",
        "well",
    }
    words = (_normalize_apostrophes(text) or "").split()
    while words and words[-1].lower().strip(".,:?") in dangling:
        words.pop()
    return " ".join(words)


def _strip_years_hedge(text: str) -> str:
    t = _normalize_apostrophes(text)
    t = re.sub(r"\b(in|of|after|before|during|by)\s+\d{3,4}s?\b", "", t or "", flags=re.I)
    t = re.sub(r"\b[12]\d{3}s?\b", "", t)
    t = re.sub(
        r"\b(successfully|actually|really|finally|eventually)\b",
        "",
        t,
        flags=re.I,
    )
    t = re.sub(r"\bwell into\b.*$", "", t, flags=re.I)
    t = re.split(r"\s+but\b", t, maxsplit=1, flags=re.I)[0]
    return _trim_dangling(" ".join(t.split()))


def _headline(parent_title: str) -> str:
    t = " ".join(_normalize_apostrophes(parent_title).split()).strip()
    t = t.split("|")[0].strip()
    if "?" in t:
        t = t.split("?", 1)[0].strip() + "?"
    if ":" in t:
        left, right = (p.strip() for p in t.split(":", 1))
        if re.match(r"what if\b", right, re.I) or re.match(r"why\b", right, re.I):
            t = right
        elif re.match(r"what if\b", left, re.I):
            t = left
        elif re.match(r"^(the dark history of|the secret letters? of)\b", left, re.I):
            t = left
        elif re.match(r"^(the forgotten|inside)\b", left, re.I) and len(right) > 8:
            t = right
    return t


def _what_if_remainder(parent_title: str) -> str | None:
    t = _headline(parent_title)
    m = re.match(r"what if\s+(.+?)\??$", t, re.I)
    if not m:
        return None
    return _strip_years_hedge(m.group(1))


def _fit_clause(text: str, *, max_words: int, max_chars: int) -> str:
    words = re.findall(r"[A-Za-z0-9']+", _normalize_apostrophes(text))
    out: list[str] = []
    for w in words:
        trial = " ".join(out + [w])
        if out and (len(out) >= max_words or len(trial) > max_chars):
            break
        out.append(w)
    return _trim_dangling(" ".join(out))


def _entity_and_twist(parent_title: str) -> tuple[str, str]:
    """Concrete noun + stakes from a longform title (not a vague 'what if?')."""
    remainder = _what_if_remainder(parent_title)
    if remainder:
        m = _TWIST_START.search(remainder)
        if m:
            ent = remainder[: m.start()].strip()
            twist = remainder[m.start() :].strip()
            entity = _clip_words(ent, 4, 28) or _clip_words(remainder, 4, 28)
            return entity, twist
        return _clip_words(remainder, 4, 28), ""

    t = _headline(parent_title)
    m = re.match(r"why\s+(.+)$", t, re.I)
    if m:
        rest = m.group(1).strip().rstrip("?")
        if re.search(r"still\s+haunts?", rest, re.I):
            ent = re.split(r"\s+still\s+haunts?", rest, maxsplit=1, flags=re.I)[0]
            return _clip_words(ent, 4, 28), "still haunts"
        return _clip_words(rest, 4, 28), ""

    m = re.match(r"the dark history of\s+(.+)$", t, re.I)
    if m:
        return _clip_words(m.group(1), 6, 36), ""
    m = re.match(r"the secret letters? of\s+(.+)$", t, re.I)
    if m:
        return _clip_words(m.group(1), 4, 28), "secret letter"
    m = re.match(r"inside (?:the )?(.+)$", t, re.I)
    if m:
        return _clip_words(m.group(1), 4, 28), ""

    t = t.rstrip("?").strip()
    t = re.sub(r"^(the|a|an|why|how)\s+", "", t, flags=re.I)
    return _clip_words(t, 4, 26), ""


def _entity_from_parent(parent_title: str) -> str:
    entity, _twist = _entity_and_twist(parent_title)
    return entity or "history"


_PUNCH: tuple[tuple[str, str], ...] = (
    ("letter", "letter"),
    ("sealed", "letter"),
    ("secret", "secret"),
    ("fever", "fever"),
    ("corpse", "corpse"),
    ("ghost", "ghost"),
    ("haunt", "ghost"),
    ("plot", "plot"),
    ("decree", "decree"),
    ("diary", "diary"),
    ("cipher", "code"),
    ("briefcase", "file"),
    ("marriage", "marriage"),
    ("fire", "fire"),
    ("monaster", "monks"),
    ("armada", "Armada"),
    ("poison", "poison"),
)


def _punch_noun(hook: str, parent_title: str) -> str:
    blob = f"{hook} {parent_title}".lower()
    for key, noun in _PUNCH:
        if key in blob:
            return noun
    return "secret"


def _finish_title(raw: str) -> str:
    want_q = (raw or "").rstrip().endswith("?")
    t = " ".join((raw or "").split()).strip(" .?")
    t = _clip_words(t, SHORTS_TITLE_MAX_WORDS, SHORTS_TITLE_MAX_CHARS)
    if not t:
        return "The secret they buried"
    if t[0].islower():
        t = t[0].upper() + t[1:]
    if want_q and not t.endswith("?") and len(t) + 1 <= SHORTS_TITLE_MAX_CHARS:
        t += "?"
    return t


def _possessive(name: str) -> str:
    n = _normalize_apostrophes(name).strip()
    if not n:
        return n
    if "'" in n:
        return n
    if n.endswith("s") or n.endswith("S"):
        return n + "'"
    return n + "'s"


def invent_shorts_title(
    *,
    parent_title: str,
    hook: str = "",
    channel: str = "",
    variant: int = 0,
) -> str:
    """4–8 word click title: entity + stakes. Never a vague 'What if X?'."""
    entity, twist = _entity_and_twist(parent_title)
    punch = _punch_noun(hook, parent_title)
    ch = (channel or "").strip().lower()
    historian = "historian" in ch or "napping" in ch
    if len(entity) <= 36 and len(entity.split()) <= 6:
        short_ent = _trim_dangling(entity) or "history"
    else:
        short_ent = _trim_dangling(_clip_words(entity, 4, 28)) or "history"
    bare = _bare_entity(short_ent) or short_ent
    last = bare.split()[-1] if bare else "history"
    twist_short = _fit_clause(twist, max_words=4, max_chars=22)
    what_if_core = _fit_clause(
        " ".join(p for p in (short_ent, twist_short) if p),
        max_words=6,
        max_chars=SHORTS_TITLE_MAX_CHARS - len("What if ") - 1,
    )
    punch_in_ent = punch.lower() in bare.lower()
    name = _short_name(bare)
    last = re.sub(r"'s$", "", (name.split()[-1] if name else last), flags=re.I) or last

    if historian:
        haunt = bool(re.search(r"haunt", f"{parent_title} {hook} {twist}", re.I))
        if haunt:
            primary = f"Why {short_ent} still haunts"
        elif twist_short:
            primary = f"{_possessive(name)} {twist_short}"
        else:
            primary = short_ent
        cands = [
            primary,
            f"{_possessive(name)} last night",
            f"The {punch} that doomed {last}",
            f"{name} never left",
        ]
    else:
        v1 = (
            f"{_possessive(name)} last night"
            if punch_in_ent
            else (
                f"{_possessive(name)} last secret"
                if punch == "secret"
                else f"{_possessive(name)} secret {punch}"
            )
        )
        cands = [
            f"What if {what_if_core}?",
            v1,
            f"The {punch} that doomed {last}",
            f"{name} {twist_short}" if twist_short else f"{name} still haunts",
        ]
    n = len(cands)
    idx = max(0, int(variant)) % n
    ordered = cands[idx:] + cands[:idx]
    seen: set[str] = set()
    fallback = _finish_title(ordered[0])
    for c in ordered:
        t = _finish_title(c)
        key = t.lower().rstrip("?")
        if key in seen:
            continue
        seen.add(key)
        if 8 <= len(t) <= SHORTS_TITLE_MAX_CHARS and len(_content_words(t)) >= 3:
            return t
        if 8 <= len(t) <= SHORTS_TITLE_MAX_CHARS:
            fallback = t
    return fallback


def title_needs_rewrite(title: str) -> bool:
    t = (title or "").strip()
    if not t:
        return True
    if len(t) > SHORTS_TITLE_MAX_CHARS:
        return True
    if len(t.split()) > SHORTS_TITLE_MAX_WORDS:
        return True
    if t.endswith("…") or t.endswith("..."):
        return True
    if len(_content_words(t)) <= 2:
        return True
    return False


def list_owned_parents(
    channel: str,
    *,
    store: OpsStore | None = None,
) -> list[dict[str, Any]]:
    """Jobs + live_vods on this channel with a final.mp4 and/or YouTube video_id."""
    store = store or OpsStore()
    want = normalize_youtube_channel(channel)
    out: list[dict[str, Any]] = []
    seen_vid: set[str] = set()
    seen_job: set[str] = set()

    def _add(
        *,
        parent_job_id: str,
        parent_video_id: str,
        parent_title: str,
        hook: str,
        job_dir: str,
        final_path: str,
        job_status: str,
    ) -> None:
        vid = (parent_video_id or "").strip()
        jid = (parent_job_id or "").strip()
        if vid and vid in seen_vid:
            return
        if jid and jid in seen_job:
            return
        if vid:
            seen_vid.add(vid)
        if jid:
            seen_job.add(jid)
        out.append(
            {
                "parent_job_id": jid,
                "parent_video_id": vid,
                "parent_title": parent_title,
                "hook": hook,
                "job_dir": job_dir,
                "final_path": final_path,
                "job_status": job_status,
                "public": job_status == "public",
            }
        )

    for job in store.list_jobs():
        if _job_channel(job) != want:
            continue
        job_dir = Path(job.job_dir) if job.job_dir else None
        vid = (job.video_id or "").strip()
        final = resolve_final_mp4(job_dir, channel=want, video_id=vid)
        if final is None and not vid:
            continue
        script = load_parent_script(job_dir)
        parent_title = str(script.get("title") or job.title or "").strip()
        hook = str(script.get("hook") or "").strip()
        _add(
            parent_job_id=job.id,
            parent_video_id=vid,
            parent_title=parent_title,
            hook=hook,
            job_dir=str(job_dir) if job_dir else "",
            final_path=str(final) if final else "",
            job_status=str(job.status or "").strip().lower(),
        )

    live_root = ROOT / "output" / "live_vods" / want
    if live_root.is_dir():
        for d in live_root.iterdir():
            if not d.is_dir():
                continue
            final = d / "final.mp4"
            if not final.is_file() or final.stat().st_size < 1000:
                continue
            vid = d.name
            if not re.match(r"^[\w-]{11}$", vid):
                continue
            parent_title = vid
            meta_path = d / "meta.json"
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    if isinstance(meta, dict) and str(meta.get("title") or "").strip():
                        parent_title = str(meta.get("title")).strip()
                except (OSError, json.JSONDecodeError):
                    pass
            _add(
                parent_job_id="",
                parent_video_id=vid,
                parent_title=parent_title,
                hook="",
                job_dir=str(d),
                final_path=str(final),
                job_status="public",
            )

    return out


def _parent_flags(parent: dict[str, Any], lf_row: TitleRow | None) -> tuple[bool, bool]:
    """Shorts are auto-public. Approve when the parent is clip-able or queued.

    Does not write longform tabs — only the flags copied onto Shorts rows.
    """
    status = (lf_row.status if lf_row is not None else parent.get("job_status") or "").lower()
    lf_approved = bool(lf_row.approved) if lf_row is not None else False
    clipable = bool(
        parent.get("final_path") or parent.get("parent_video_id") or parent.get("job_dir")
    )
    queued_like = status in {"queued", "private", "scheduled", "public", "done"}
    approved = bool(lf_approved or clipable or queued_like)
    return approved, True


def _already_bound(
    rows: list[TitleRow],
    *,
    parent_job_id: str,
    parent_video_id: str,
    window_index: int = 0,
) -> bool:
    pj = (parent_job_id or "").strip()
    pv = (parent_video_id or "").strip()
    w = str(int(window_index))
    for r in rows:
        rw = str(r.window_index or "0")
        same_w = rw == w
        if pj and (r.parent_job_id == pj or r.job_id == pj) and same_w:
            return True
        if pv and r.parent_video_id == pv and same_w:
            return True
    return False


def _winner_boost(parent: dict[str, Any], winner_titles: set[str]) -> float:
    title = (parent.get("parent_title") or "").strip().lower()
    if title and title in winner_titles:
        return 1.0
    if parent.get("public"):
        return 0.5
    return 0.0


def _windows_for(parent: dict[str, Any], channel: str) -> list[tuple[int, float]]:
    durs: list[float] = []
    jd = parent.get("job_dir") or ""
    if jd:
        durs = load_scene_durations(jd)
    media = probe_media_duration_s(parent.get("final_path") or "")
    starts = trailer_window_starts(durs, channel=channel, media_duration_s=media)
    if not starts:
        starts = [0.0]
    return [(i, s) for i, s in enumerate(starts)]


def harvest_shorts_titles(
    *,
    channel: str,
    store: OpsStore | None = None,
    queue: TitleQueue | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Append/refresh parent-bound Shorts rows.

    Shorts are auto-public (``public_approved=TRUE``) whenever the parent is
    clip-able. ``approved`` follows clip-able / already-queued parents.
    Longform tabs are not written.
    """
    store = store or OpsStore()
    ch = normalize_youtube_channel(channel)
    lf_queue = queue or TitleQueue(channel=ch)
    shorts_q = shorts_title_queue(ch)
    tab = shorts_tab_name(ch)
    parents = list_owned_parents(ch, store=store)
    existing = shorts_q.list_rows(channel=tab)
    lf_by_job = {r.job_id: r for r in lf_queue.list_rows(channel=ch) if r.job_id}
    lf_by_vid = {r.video_id: r for r in lf_queue.list_rows(channel=ch) if r.video_id}

    winner_titles: set[str] = set()
    try:
        from src.agents.smm_harvest_bridge import load_smm_winner_signals

        sig = load_smm_winner_signals(channel=ch) or {}
        for w in sig.get("winners") or sig.get("titles") or []:
            if isinstance(w, str) and w.strip():
                winner_titles.add(w.strip().lower())
            elif isinstance(w, dict):
                t = str(w.get("title") or "").strip().lower()
                if t:
                    winner_titles.add(t)
    except Exception:  # noqa: BLE001
        pass

    parents.sort(key=lambda p: (-_winner_boost(p, winner_titles), p.get("parent_title") or ""))
    added_rows: list[dict[str, Any]] = []
    refreshed = 0
    skipped = 0
    dropped = 0
    dirty_existing = False

    window_map: dict[tuple[str, str, int], float] = {}
    parent_jobs: set[str] = set()
    parent_vids: set[str] = set()
    allowed_windows: dict[tuple[str, str], set[int]] = {}
    for parent in parents:
        pj = str(parent.get("parent_job_id") or "")
        pv = str(parent.get("parent_video_id") or "")
        if pj:
            parent_jobs.add(pj)
        if pv:
            parent_vids.add(pv)
        for window_index, start_at in _windows_for(parent, ch):
            window_map[(pj, pv, window_index)] = start_at
            allowed_windows.setdefault((pj, pv), set()).add(window_index)

    def _row_window(r: TitleRow) -> int:
        try:
            return int(str(r.window_index or "0").strip() or 0)
        except ValueError:
            return 0

    def _is_owned_row(r: TitleRow) -> bool:
        pj = (r.parent_job_id or r.job_id or "").strip()
        pv = (r.parent_video_id or "").strip()
        if pj and pj in parent_jobs:
            return True
        if pv and pv in parent_vids:
            return True
        return False

    kept: list[TitleRow] = []
    for r in existing:
        if not _is_owned_row(r):
            dropped += 1
            dirty_existing = True
            continue
        wi = _row_window(r)
        pj = (r.parent_job_id or r.job_id or "").strip()
        pv = (r.parent_video_id or "").strip()
        allowed = allowed_windows.get((pj, pv)) or allowed_windows.get((pj, "")) or set()
        if pv and not allowed:
            allowed = allowed_windows.get(("", pv)) or set()
        if allowed and wi not in allowed:
            dropped += 1
            dirty_existing = True
            continue
        start_at = window_map.get((pj, pv, wi))
        if start_at is None:
            start_at = window_map.get((pj, "", wi), 0.0)
        src_parent = next(
            (
                p
                for p in parents
                if (pj and p.get("parent_job_id") == pj)
                or (pv and p.get("parent_video_id") == pv)
            ),
            None,
        )
        if src_parent and src_parent.get("parent_title"):
            r.parent_title = str(src_parent["parent_title"])
        new_t = invent_shorts_title(
            parent_title=r.parent_title or r.title,
            hook=str((src_parent or {}).get("hook") or ""),
            channel=ch,
            variant=wi,
        )
        notes = f"parent={pj or pv}; harvest_shorts; window={wi}; start={start_at:.0f}s"
        if new_t != r.title or r.notes != notes or not r.policy_ok:
            r.title = new_t
            r.policy_ok = True
            r.notes = notes
            r.window_index = str(wi)
            dirty_existing = True
            refreshed += 1
        lf_row = lf_by_job.get(pj) or lf_by_vid.get(pv)
        approved, public_approved = _parent_flags(src_parent or {}, lf_row)
        if public_approved and not r.public_approved:
            r.public_approved = True
            dirty_existing = True
        if approved and not r.approved:
            r.approved = True
            dirty_existing = True
        if (r.status or "").lower() == "hold":
            r.status = "queued"
            dirty_existing = True
        kept.append(r)
    existing = kept

    for parent in parents:
        lf_row = lf_by_job.get(str(parent.get("parent_job_id") or "")) or lf_by_vid.get(
            str(parent.get("parent_video_id") or "")
        )
        approved, public_approved = _parent_flags(parent, lf_row)
        for window_index, start_at in _windows_for(parent, ch):
            if limit is not None and len(added_rows) >= int(limit):
                break
            pj = str(parent.get("parent_job_id") or "")
            pv = str(parent.get("parent_video_id") or "")
            if _already_bound(
                existing, parent_job_id=pj, parent_video_id=pv, window_index=window_index
            ):
                skipped += 1
                continue
            title = invent_shorts_title(
                parent_title=str(parent.get("parent_title") or ""),
                hook=str(parent.get("hook") or ""),
                channel=ch,
                variant=window_index,
            )
            row = {
                "title": title,
                "kind": "shorts",
                "parent_title": parent.get("parent_title") or "",
                "parent_job_id": pj,
                "parent_video_id": pv,
                "job_id": pj,
                "short_video_id": "",
                "video_id": "",
                "window_index": str(window_index),
                "approved": approved,
                "public_approved": public_approved,
                "policy_ok": True,
                "status": "queued",
                "notes": f"parent={pj or pv}; harvest_shorts; window={window_index}; start={start_at:.0f}s",
                "trend_score": _winner_boost(parent, winner_titles),
            }
            added_rows.append(row)
            existing.append(
                TitleRow(
                    row_index=len(existing) + 2,
                    title=title,
                    channel=tab,
                    kind="shorts",
                    parent_title=str(parent.get("parent_title") or ""),
                    parent_job_id=pj,
                    parent_video_id=pv,
                    job_id=pj,
                    approved=approved,
                    public_approved=public_approved,
                    policy_ok=True,
                    status=str(row["status"]),
                    notes=str(row["notes"]),
                    trend_score=float(row["trend_score"] or 0),
                    window_index=str(window_index),
                )
            )
        if limit is not None and len(added_rows) >= int(limit):
            break

    n_added = 0
    if not dry_run and (dirty_existing or added_rows):
        shorts_q.replace_channel_rows(tab, existing)
        n_added = len(added_rows)

    return {
        "ok": True,
        "channel": ch,
        "shorts_tab": tab,
        "parents": len(parents),
        "skipped_existing": skipped,
        "invented": len(added_rows),
        "added": n_added if not dry_run else 0,
        "refreshed_titles": refreshed,
        "dropped_junk": dropped,
        "dry_run": dry_run,
        "titles": [r["title"] for r in added_rows],
        "note": (
            "HOLD longform without final.mp4 cannot become Shorts until the "
            "parent video exists."
        ),
    }
