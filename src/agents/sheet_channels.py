"""Multi-channel Google Sheet tabs (napstorian + napping_historian).

Same spreadsheet, one tab per YouTube channel. Headers, approve→produce rules,
harvest stock, and capacity gates are identical; max_concurrent=1 is global.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from src.services.settings import CONFIG_DIR, ROOT, get_settings

logger = logging.getLogger(__name__)

DEFAULT_CHANNELS = ("napstorian", "napping_historian")
LEGACY_TAB_ALIASES = ("Sheet1", "sheet1", "Sheet 1")
PICK_STATE_PATH = ROOT / "output" / "ops" / "sheet_pick_state.json"
SHORTS_TAB_SUFFIX = "_shorts"


def is_shorts_tab(name: str | None) -> bool:
    """True for sibling Shorts marketing tabs (never farm-picked)."""
    n = (name or "").strip().lower()
    return n.endswith(SHORTS_TAB_SUFFIX)


def shorts_tab_name(channel: str) -> str:
    ch = (channel or "").strip()
    if is_shorts_tab(ch):
        return ch
    return f"{ch}{SHORTS_TAB_SUFFIX}"


def longform_channel_from_shorts_tab(tab: str) -> str:
    name = (tab or "").strip()
    if is_shorts_tab(name):
        return name[: -len(SHORTS_TAB_SUFFIX)]
    return name


def empire_shorts_tab_names() -> list[str]:
    """``{channel}_shorts`` for every empire channel (production + staged)."""
    try:
        from src.agents.channel_empire import all_known_channel_names

        names = all_known_channel_names()
    except Exception:  # noqa: BLE001
        names = list(DEFAULT_CHANNELS)
    return [shorts_tab_name(n) for n in names if n]


def _agents_cfg() -> dict[str, Any]:
    path = CONFIG_DIR / "agents_settings.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def configured_sheet_channels() -> list[str]:
    """Ordered channel/tab names from env or agents_settings.

    Precedence:
    1. SHEET_CHANNELS=napstorian,napping_historian
    2. agents_settings.sheet_channels (list of names or {name: ...} objects)
    3. agents_settings.sheet_tab (single) → still dual-default if unset
    4. DEFAULT_CHANNELS

    Staged empire channels (``production_enabled: false`` in
    ``config/channel_empire.json``) are excluded unless listed explicitly in
    SHEET_CHANNELS — prevents Wave 1/2 harvest before the expansion gate.
    """
    raw = (os.getenv("SHEET_CHANNELS") or "").strip()
    if raw:
        names = [p.strip() for p in raw.split(",") if p.strip() and not is_shorts_tab(p)]
        if names:
            return _dedupe(names)

    cfg = _agents_cfg()
    channels = cfg.get("sheet_channels")
    if isinstance(channels, list) and channels:
        names: list[str] = []
        for item in channels:
            if isinstance(item, str) and item.strip():
                names.append(item.strip())
            elif isinstance(item, dict):
                if item.get("production_enabled") is False:
                    continue
                n = (item.get("name") or item.get("tab") or item.get("sheet_tab") or "").strip()
                if n:
                    names.append(n)
        names = _filter_empire_production(names)
        names = [n for n in names if not is_shorts_tab(n)]
        if names:
            return _dedupe(names)

    single = (cfg.get("sheet_tab") or "").strip()
    if single and single not in LEGACY_TAB_ALIASES:
        # Explicit single non-legacy tab — honor it alone only if no dual list.
        # Prefer dual defaults when sheet_tab is still Sheet1.
        return [single]

    return list(DEFAULT_CHANNELS)


def _filter_empire_production(names: list[str]) -> list[str]:
    """Drop staged empire channels that are not production_enabled."""
    try:
        from src.agents.channel_empire import channel_entry, load_empire

        if not load_empire():
            return names
    except Exception:  # noqa: BLE001
        return names
    out: list[str] = []
    for n in names:
        entry = channel_entry(n)
        if entry and entry.get("production_enabled") is False:
            continue
        out.append(n)
    return out


def channel_in_publish_subset(channel: str) -> bool:
    """True unless SHEET_PUBLISH_CHANNELS restricts to a named subset.

    Empty / unset → both sheets (napstorian + napping_historian) eligible.
    """
    subset = (os.getenv("SHEET_PUBLISH_CHANNELS") or "").strip()
    if not subset:
        return True
    allowed = {p.strip().lower() for p in subset.split(",") if p.strip()}
    return channel.strip().lower() in allowed


def channel_publish_enabled(channel: str) -> bool:
    """Whether idea-stock harvest may refill this channel under publish_schedule.

    Prefer ``idea_stock.idea_stock_refill_triggered`` for the real gate (publish
    schedule **or** ≥1 publish-path title). This helper remains for callers that
    only care about the env/config publish flag + optional sheet subset.
    """
    from src.agents.idea_stock import publish_schedule_enabled

    if not publish_schedule_enabled():
        return False
    return channel_in_publish_subset(channel)


def _channel_entry(channel: str) -> dict[str, Any] | None:
    """Return agents_settings.sheet_channels object for ``channel``, if any."""
    ch = (channel or "").strip().lower()
    if not ch:
        return None
    for item in _agents_cfg().get("sheet_channels") or []:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or item.get("tab") or item.get("sheet_tab") or "").strip()
        if name.lower() == ch:
            return item
    return None


def channel_youtube_id(channel: str | None) -> str | None:
    """Optional Brand Account UC… id from sheet_channels config (public id only)."""
    entry = _channel_entry((channel or "").strip())
    if not entry:
        return None
    cid = str(entry.get("youtube_channel_id") or "").strip()
    return cid or None


def channel_default_profile(channel: str) -> str:
    """Retention profile when sheet has no ``format`` column.

    Precedence:
    1. SHEET_CHANNEL_PROFILE_<CHANNEL>=retention|longform|epic
    2. agents_settings.sheet_channels[].default_format / default_profile
    3. RETENTION_PROFILE env / active_profile_name()
    4. retention
    """
    from src.services.retention_profile import DEFAULT_PROFILE, normalize_format

    ch = (channel or "").strip()
    env_key = f"SHEET_CHANNEL_PROFILE_{(ch or 'DEFAULT').upper().replace('-', '_')}"
    raw = (os.getenv(env_key) or "").strip()
    if raw:
        return normalize_format(raw)

    entry = _channel_entry(ch)
    if entry:
        prof = (
            entry.get("default_format")
            or entry.get("default_profile")
            or entry.get("format")
            or ""
        )
        if str(prof).strip():
            return normalize_format(str(prof))
    return normalize_format("", default=DEFAULT_PROFILE)


def channel_prompts_dir(channel: str | None) -> str | None:
    """Optional per-channel prompts directory (repo-relative or absolute).

    Precedence:
    1. SHEET_CHANNEL_PROMPTS_DIR_<CHANNEL>
    2. agents_settings.sheet_channels[].prompts_dir
    3. None → fall back to active retention profile prompts_dir
    """
    ch = (channel or "").strip()
    if not ch:
        return None
    env_key = f"SHEET_CHANNEL_PROMPTS_DIR_{ch.upper().replace('-', '_')}"
    raw = (os.getenv(env_key) or "").strip()
    if raw:
        return raw
    entry = _channel_entry(ch)
    if not entry:
        return None
    rel = (entry.get("prompts_dir") or "").strip()
    return rel or None


def channel_script_overrides(channel: str | None) -> dict[str, Any]:
    """Script-settings overlays from the channel entry (e.g. chapters_target).

    Does not include name / default_format / prompts_dir / note — those are
    handled separately. Used so historian can ask for 35–40 chapters while
    still riding the epic dual-pacing profile.
    """
    entry = _channel_entry(channel or "")
    if not entry:
        return {}
    out: dict[str, Any] = {}
    if entry.get("chapters_target") is not None:
        try:
            out["chapters_target"] = int(entry["chapters_target"])
        except (TypeError, ValueError):
            pass
    return out


def local_queue_path(channel: str) -> Path:
    """Per-channel local CSV fallback path."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in channel.strip()) or "default"
    return ROOT / "output" / "ops" / f"title_queue_{safe}.csv"


def legacy_local_queue_path() -> Path:
    return ROOT / "output" / "ops" / "title_queue.csv"


def load_pick_state() -> dict[str, Any]:
    if not PICK_STATE_PATH.exists():
        return {}
    try:
        return json.loads(PICK_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_pick_state(last_channel: str, *, farmed_at: str | None = None) -> None:
    """Persist last picked channel + per-channel last farm timestamps.

    Round-robin uses ``last_channel``. Cold start (no last) uses
    ``last_farm_at_by_channel`` to prefer the channel with ready stock that
    was farmed least recently.
    """
    from datetime import datetime, timezone

    PICK_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    prev = load_pick_state()
    by = dict(prev.get("last_farm_at_by_channel") or {})
    # Preserve prior keys; normalize to configured channel casing when possible.
    ts = (farmed_at or "").strip() or datetime.now(timezone.utc).isoformat()
    by[last_channel] = ts
    payload = {
        "last_channel": last_channel,
        "last_farm_at_by_channel": by,
        "policy": "round_robin",
        "cold_start": "least_recent_farm_among_stock",
        "note": (
            "After a pick from channel C, next tick prefers the next channel "
            "in SHEET_CHANNELS order that has approved+policy_ok+queued+empty "
            "job_id ideas. Cold start (no last_channel): prefer the ready "
            "channel with the least recent farm start."
        ),
    }
    PICK_STATE_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def round_robin_channel_order(
    channels: list[str], *, last_channel: str | None = None
) -> list[str]:
    """Rotate so the channel after last_channel is first (fair dual-sheet pick)."""
    if not channels:
        return []
    if not last_channel:
        return list(channels)
    lower = [c.lower() for c in channels]
    try:
        idx = lower.index(last_channel.strip().lower())
    except ValueError:
        return list(channels)
    start = (idx + 1) % len(channels)
    return channels[start:] + channels[:start]


def pick_channel_order(
    channels: list[str],
    *,
    last_channel: str | None = None,
    last_farm_at_by_channel: dict[str, str] | None = None,
    channels_with_ready: set[str] | None = None,
) -> list[str]:
    """Fair dual-sheet pick order.

    * With ``last_channel``: classic round-robin (next channel after last).
    * Cold start (no last): prefer channels that have ready stock and the
      least recent farm start (never-farmed first); tie-break = configured order.

    Empty buckets are skipped by the caller; this only sets preference order.
    """
    if not channels:
        return []
    if (last_channel or "").strip():
        return round_robin_channel_order(channels, last_channel=last_channel)

    farm_at = last_farm_at_by_channel or {}
    # Case-insensitive lookup for timestamps
    farm_lower = {str(k).lower(): str(v) for k, v in farm_at.items() if k}

    def sort_key(ch: str) -> tuple[int, str, int]:
        ready_rank = 0
        if channels_with_ready is not None:
            ready_rank = 0 if ch in channels_with_ready else 1
        ts = farm_lower.get(ch.lower(), "")  # missing = oldest / never farmed
        try:
            cfg_idx = channels.index(ch)
        except ValueError:
            cfg_idx = 999
        return (ready_rank, ts, cfg_idx)

    return sorted(channels, key=sort_key)


def last_farm_at_from_jobs() -> dict[str, str]:
    """Best-effort per-channel last farm start from OpsStore jobs (created_at)."""
    try:
        from src.agents.store import OpsStore

        out: dict[str, str] = {}
        for job in OpsStore().list_jobs():
            meta = getattr(job, "meta", None) or {}
            ch = (
                (meta.get("channel") or meta.get("sheet_tab") or "")
                if isinstance(meta, dict)
                else ""
            ).strip()
            if not ch:
                continue
            created = (getattr(job, "created_at", None) or "").strip()
            if not created:
                continue
            prev = out.get(ch)
            if prev is None or created > prev:
                out[ch] = created
        return out
    except Exception:  # noqa: BLE001
        return {}


def ensure_sheet_channels(
    *,
    channels: list[str] | None = None,
    columns: list[str] | None = None,
    rename_legacy: bool = True,
) -> dict[str, Any]:
    """Rename Sheet1→first channel (if needed) and create sibling tabs with headers.

    Never deletes row data. Creates empty tabs with header row only.
    """
    from src.agents.title_queue import DEFAULT_COLUMNS

    s = get_settings()
    sheet_id = (getattr(s, "google_sheet_id", None) or "").strip()
    creds = (getattr(s, "google_sheets_credentials", None) or "").strip()
    chans = list(channels or configured_sheet_channels())
    cols = list(columns or DEFAULT_COLUMNS)
    out: dict[str, Any] = {
        "ok": False,
        "sheet_id": sheet_id or None,
        "channels": chans,
        "actions": [],
        "titles_before": [],
        "titles_after": [],
    }
    if not sheet_id or not creds:
        out["error"] = "GOOGLE_SHEET_ID / credentials not configured"
        out["manual"] = (
            f"In Google Sheets UI: rename topics tab → {chans[0]!r}; "
            f"add tab {chans[1]!r} with the same header row."
            if len(chans) >= 2
            else f"Rename topics tab → {chans[0]!r}."
        )
        return out

    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        credentials = Credentials.from_service_account_file(creds, scopes=scopes)
        svc = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"sheets client failed: {exc}"
        return out

    try:
        meta = (
            svc.spreadsheets()
            .get(
                spreadsheetId=sheet_id,
                fields="sheets.properties(sheetId,title,index)",
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"spreadsheet get failed: {exc}"
        return out

    sheets = meta.get("sheets") or []
    props = [(s.get("properties") or {}) for s in sheets]
    titles = [p.get("title") for p in props if p.get("title")]
    out["titles_before"] = list(titles)
    title_to_id = {p.get("title"): p.get("sheetId") for p in props if p.get("title")}

    requests: list[dict[str, Any]] = []

    # Rename legacy Sheet1 → primary channel when primary missing
    primary = chans[0] if chans else "napstorian"
    if rename_legacy and primary not in title_to_id:
        legacy_title = next((t for t in titles if t in LEGACY_TAB_ALIASES), None)
        if legacy_title is not None:
            sid = title_to_id.get(legacy_title)
            if sid is not None:
                requests.append(
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": sid, "title": primary},
                            "fields": "title",
                        }
                    }
                )
                out["actions"].append(
                    {"op": "rename", "from": legacy_title, "to": primary}
                )
                # Update local maps for subsequent create checks
                title_to_id[primary] = sid
                del title_to_id[legacy_title]
                titles = [primary if t == legacy_title else t for t in titles]

    # Create missing channel tabs
    for name in chans:
        if name in title_to_id:
            continue
        requests.append({"addSheet": {"properties": {"title": name}}})
        out["actions"].append({"op": "add_sheet", "title": name})

    if requests:
        try:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id, body={"requests": requests}
            ).execute()
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"batchUpdate failed: {exc}"
            out["manual"] = (
                f"Rename Sheet1→{primary} and create missing tabs in the UI if needed."
            )
            return out

    # Refresh sheet ids/titles after mutations
    try:
        meta2 = (
            svc.spreadsheets()
            .get(
                spreadsheetId=sheet_id,
                fields="sheets.properties(sheetId,title)",
            )
            .execute()
        )
        props2 = [(s.get("properties") or {}) for s in (meta2.get("sheets") or [])]
        titles_after = [p.get("title") for p in props2 if p.get("title")]
        title_to_id = {
            p.get("title"): p.get("sheetId") for p in props2 if p.get("title")
        }
        out["titles_after"] = titles_after
    except Exception as exc:  # noqa: BLE001
        out["titles_after"] = titles
        out["warn"] = f"re-list failed: {exc}"

    # Drop legacy ``format`` column + ensure identical headers (no data wipe)
    for name in chans:
        try:
            sid = title_to_id.get(name)
            _ensure_channel_headers(
                svc, sheet_id, name, sheet_gid=sid, columns=cols, out=out
            )
        except Exception as exc:  # noqa: BLE001
            out["actions"].append(
                {"op": "header_failed", "title": name, "error": str(exc)[:200]}
            )

    missing = [c for c in chans if c not in (out.get("titles_after") or titles)]
    out["ok"] = not missing and "error" not in out
    out["headers"] = cols
    if missing:
        out["missing"] = missing
        out["manual"] = (
            "Create missing tabs in Google Sheets UI with the same header row: "
            + ", ".join(missing)
        )
    return out


def ensure_shorts_tabs(
    *,
    channels: list[str] | None = None,
    columns: list[str] | None = None,
) -> dict[str, Any]:
    """Create ``{channel}_shorts`` sibling tabs for empire channels (no farm pick)."""
    from src.agents.title_queue import SHORTS_COLUMNS

    tabs = list(channels or empire_shorts_tab_names())
    cols = list(columns or SHORTS_COLUMNS)
    return ensure_sheet_channels(channels=tabs, columns=cols, rename_legacy=False)


def _ensure_channel_headers(
    svc: Any,
    sheet_id: str,
    tab: str,
    *,
    sheet_gid: int | None,
    columns: list[str],
    out: dict[str, Any],
) -> None:
    """Write header if empty; drop legacy columns if present; never wipe other cols.

    Legacy drops (both channel tabs): ``format``, ``publish_hour_approved``,
    ``publish_hour_override``. Hours come from preferred_hours / CLI only.
    """
    safe = tab.replace("'", "''")
    rng = f"'{safe}'!A1:AZ"
    result = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=rng)
        .execute()
    )
    values = result.get("values") or []
    if not values:
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{safe}'!A1",
            valueInputOption="RAW",
            body={"values": [columns]},
        ).execute()
        out["actions"].append({"op": "write_header", "title": tab})
        return

    header = [str(h).strip() for h in values[0]]
    header_l = [h.lower() for h in header]
    want_l = [c.lower() for c in columns]

    # Drop legacy columns on BOTH channel sheets
    for legacy in ("format", "publish_hour_approved", "publish_hour_override"):
        header = [str(h).strip() for h in (values[0] if values else [])]
        header_l = [h.lower() for h in header]
        if legacy not in header_l or sheet_gid is None:
            continue
        idx = header_l.index(legacy)
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "requests": [
                    {
                        "deleteDimension": {
                            "range": {
                                "sheetId": sheet_gid,
                                "dimension": "COLUMNS",
                                "startIndex": idx,
                                "endIndex": idx + 1,
                            }
                        }
                    }
                ]
            },
        ).execute()
        out["actions"].append(
            {
                "op": f"drop_{legacy}_column",
                "title": tab,
                "col_index": idx,
            }
        )
        result = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=rng)
            .execute()
        )
        values = result.get("values") or []

    header = [str(h).strip() for h in (values[0] if values else [])]
    header_l = [h.lower() for h in header]

    if header_l == want_l:
        out["actions"].append({"op": "header_ok", "title": tab})
        return

    if not header_l or header_l == [""]:
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{safe}'!A1",
            valueInputOption="RAW",
            body={"values": [columns]},
        ).execute()
        out["actions"].append({"op": "write_header", "title": tab})
        return

    # Existing non-empty header that isn't exact match: rewrite header cells only
    # for known columns we care about — do not delete unknown extra columns.
    if header_l and header_l[0] == "title":
        # Align first N cells to canonical columns without touching data rows
        # only if format already gone and we're missing nothing critical.
        missing_cols = [c for c in columns if c.lower() not in header_l]
        if (
            not missing_cols
            and "format" not in header_l
            and "publish_hour_approved" not in header_l
            and "publish_hour_override" not in header_l
        ):
            out["actions"].append(
                {
                    "op": "header_present",
                    "title": tab,
                    "note": "title header present; left columns as-is",
                    "header": header,
                }
            )
            return
        if missing_cols:
            # Append missing columns to the right of existing header (no wipe)
            new_header = header + missing_cols
            svc.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"'{safe}'!A1",
                valueInputOption="RAW",
                body={"values": [new_header]},
            ).execute()
            out["actions"].append(
                {
                    "op": "append_missing_headers",
                    "title": tab,
                    "added": missing_cols,
                }
            )
            return

    out["actions"].append(
        {
            "op": "header_present",
            "title": tab,
            "note": "non-empty header left unchanged",
            "header": header,
        }
    )


def _dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        key = n.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out
