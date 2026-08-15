"""Title Queue — multi-channel Google Sheets or local CSV fallback.

Channels (tabs in one spreadsheet): napstorian, napping_historian (SHEET_CHANNELS).
Identical headers on every tab — no ``format`` column (profile from channel/config
default → RETENTION_PROFILE → retention). Pick alternates across channels
(round-robin after last pick; cold start prefers least-recent farm among stock);
max_concurrent=1 remains global at the farm/watchdog layer.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.agents.sheet_channels import (
    channel_default_profile,
    configured_sheet_channels,
    is_shorts_tab,
    last_farm_at_from_jobs,
    legacy_local_queue_path,
    load_pick_state,
    local_queue_path,
    pick_channel_order,
    save_pick_state,
)
from src.services.settings import CONFIG_DIR, ROOT, get_settings

logger = logging.getLogger(__name__)

# Legacy single-file path (migrated → title_queue_<primary>.csv on first write)
LOCAL_QUEUE = ROOT / "output" / "ops" / "title_queue.csv"

# Identical headers on both channel tabs (format removed — profile via channel default).
DEFAULT_COLUMNS = [
    "title",
    "competitor_source",
    "source_subs",
    "source_avg_recent_views",
    "source_eligible",
    "source_video_views",
    "source_video_likes",
    "source_video_comments",
    "source_video_uploaded_at",
    "trend_score",
    "policy_ok",
    "approved",
    "public_approved",
    "status",
    "job_id",
    "video_id",
    "scheduled_at",
    "notes",
]

SHORTS_EXTRA_COLUMNS = [
    "kind",
    "parent_title",
    "parent_job_id",
    "parent_video_id",
    "short_video_id",
    "window_index",
]

SHORTS_COLUMNS = DEFAULT_COLUMNS + [
    c for c in SHORTS_EXTRA_COLUMNS if c not in DEFAULT_COLUMNS
]


@dataclass
class TitleRow:
    row_index: int
    title: str
    channel: str = ""
    format: str = ""  # in-memory only; not a sheet column (channel/config default)
    competitor_source: str = ""
    source_subs: str = ""
    source_avg_recent_views: str = ""
    source_eligible: str = ""
    source_video_views: str = ""
    source_video_likes: str = ""
    source_video_comments: str = ""
    source_video_uploaded_at: str = ""
    trend_score: float = 0.0
    policy_ok: bool = False
    approved: bool = False
    public_approved: bool = False
    status: str = "queued"
    job_id: str = ""
    video_id: str = ""
    scheduled_at: str = ""
    notes: str = ""
    kind: str = ""
    parent_title: str = ""
    parent_job_id: str = ""
    parent_video_id: str = ""
    short_video_id: str = ""
    window_index: str = "0"

    def as_dict(self) -> dict[str, Any]:
        return {
            "row_index": self.row_index,
            "channel": self.channel,
            "title": self.title,
            "format": self.format,
            "competitor_source": self.competitor_source,
            "source_subs": self.source_subs,
            "source_avg_recent_views": self.source_avg_recent_views,
            "source_eligible": self.source_eligible,
            "source_video_views": self.source_video_views,
            "source_video_likes": self.source_video_likes,
            "source_video_comments": self.source_video_comments,
            "source_video_uploaded_at": self.source_video_uploaded_at,
            "trend_score": self.trend_score,
            "policy_ok": self.policy_ok,
            "approved": self.approved,
            "public_approved": self.public_approved,
            "status": self.status,
            "job_id": self.job_id,
            "video_id": self.video_id,
            "scheduled_at": self.scheduled_at,
            "notes": self.notes,
            "kind": self.kind,
            "parent_title": self.parent_title,
            "parent_job_id": self.parent_job_id,
            "parent_video_id": self.parent_video_id,
            "short_video_id": self.short_video_id,
            "window_index": self.window_index,
        }

    @property
    def sheet_tab(self) -> str:
        return self.channel


class TitleQueue:
    """Human Approve gate across all configured sheet channels."""

    def __init__(self, *, channel: str | None = None):
        self.s = get_settings()
        self.cfg = _load_agents_settings()
        self.columns = list(self.cfg.get("columns") or DEFAULT_COLUMNS)
        # Drop legacy format / publish_hour_approved from config columns if present
        self.columns = [
            c
            for c in self.columns
            if c
            not in ("format", "publish_hour_approved", "publish_hour_override")
        ]
        if "title" not in self.columns:
            self.columns = list(DEFAULT_COLUMNS)
        self.channels = [c for c in configured_sheet_channels() if not is_shorts_tab(c)]
        self._focus_channel = (channel or "").strip() or None
        if is_shorts_tab(self._focus_channel or ""):
            self.columns = list(SHORTS_COLUMNS)
        for ch in self.channels:
            path = local_queue_path(ch)
            path.parent.mkdir(parents=True, exist_ok=True)
        if self._focus_channel:
            local_queue_path(self._focus_channel).parent.mkdir(parents=True, exist_ok=True)

    def columns_for(self, channel: str | None = None) -> list[str]:
        ch = (channel or self._focus_channel or "").strip()
        if is_shorts_tab(ch):
            return list(SHORTS_COLUMNS)
        return list(self.columns)

    @property
    def default_channel(self) -> str:
        return self.channels[0] if self.channels else "napstorian"

    def list_rows(self, *, channel: str | None = None) -> list[TitleRow]:
        chans = self._channels_for(channel)
        out: list[TitleRow] = []
        for ch in chans:
            out.extend(self._list_channel(ch))
        return out

    def append_titles(
        self, rows: list[dict[str, Any]], *, channel: str | None = None
    ) -> int:
        ch = (channel or self._focus_channel or self.default_channel).strip()
        existing = self._list_channel(ch)
        existing_titles = {r.title.strip().lower() for r in existing}
        added = 0
        for raw in rows:
            title = (raw.get("title") or "").strip()
            if not title or title.lower() in existing_titles:
                continue
            existing.append(
                _row_from_dict(len(existing) + 2, raw, title=title, channel=ch)
            )
            existing_titles.add(title.lower())
            added += 1
        if added:
            self._persist_channel(ch, existing)
        return added

    def update_row(self, row_index: int, **updates: Any) -> TitleRow | None:
        channel = (updates.pop("channel", None) or updates.pop("sheet_tab", None) or "").strip()
        channel = channel or (self._focus_channel or "")
        if channel:
            rows = self._list_channel(channel)
            for r in rows:
                if r.row_index == row_index:
                    for k, v in updates.items():
                        if hasattr(r, k):
                            setattr(r, k, v)
                    self._persist_channel(channel, rows)
                    return r
            return None

        # Ambiguous without channel: scan all; require unique row_index match
        matches: list[tuple[str, list[TitleRow], TitleRow]] = []
        for ch in self.channels:
            rows = self._list_channel(ch)
            for r in rows:
                if r.row_index == row_index:
                    matches.append((ch, rows, r))
        if not matches:
            return None
        if len(matches) > 1:
            # Prefer job_id match if present on the row being updated
            job_id = str(updates.get("job_id") or "")
            if job_id:
                for ch, rows, r in matches:
                    if r.job_id == job_id:
                        for k, v in updates.items():
                            if hasattr(r, k):
                                setattr(r, k, v)
                        self._persist_channel(ch, rows)
                        return r
            logger.warning(
                "update_row row_index=%s matches %d channels; pass channel=",
                row_index,
                len(matches),
            )
            return None
        ch, rows, r = matches[0]
        for k, v in updates.items():
            if hasattr(r, k):
                setattr(r, k, v)
        self._persist_channel(ch, rows)
        return r

    def find_by_job_id(self, job_id: str) -> TitleRow | None:
        for r in self.list_rows():
            if r.job_id == job_id:
                return r
        return None

    def pick_approved(
        self,
        *,
        limit: int = 1,
        channel: str | None = None,
        mutate_state: bool = True,
    ) -> list[TitleRow]:
        """Pick ready ideas. Multi-channel: fair alternate across tabs.

        Order:
        - After a real pick: round-robin (next channel after ``last_channel``).
        - Cold start (no last): channel with ready stock + least recent farm start.
        Within a channel: highest trend_score, then lowest row_index.
        Global concurrency is enforced by the worker (max_concurrent=1).

        ``mutate_state=False`` previews the next pick without writing
        ``sheet_pick_state.json`` (dry-pick / status / boards).
        """
        if channel or self._focus_channel:
            ch = (channel or self._focus_channel or "").strip()
            if is_shorts_tab(ch):
                return []
            ready = self._ready_rows(self._list_channel(ch))
            return ready[:limit]

        state = load_pick_state()
        last = (state.get("last_channel") or "").strip() or None
        farm_at = dict(state.get("last_farm_at_by_channel") or {})
        # Fill missing channels from job history so cold start is meaningful
        # even before the first dual-channel pick under the new state shape.
        if not last or not farm_at:
            for ch, ts in last_farm_at_from_jobs().items():
                farm_at.setdefault(ch, ts)
        buckets: dict[str, list[TitleRow]] = {
            ch: self._ready_rows(self._list_channel(ch)) for ch in self.channels
        }
        ready_set = {ch for ch, rows in buckets.items() if rows}
        order = pick_channel_order(
            self.channels,
            last_channel=last,
            last_farm_at_by_channel=farm_at,
            channels_with_ready=ready_set,
        )
        picked: list[TitleRow] = []
        while len(picked) < limit:
            progressed = False
            for ch in order:
                bucket = buckets.get(ch) or []
                if not bucket:
                    continue
                picked.append(bucket.pop(0))
                progressed = True
                if len(picked) >= limit:
                    break
            if not progressed:
                break
        if picked and mutate_state:
            save_pick_state(picked[-1].channel)
        return picked

    def count_pending_ready(self, *, channel: str | None = None) -> int:
        """Count ready ideas without mutating round-robin pick state."""
        if channel or self._focus_channel:
            ch = (channel or self._focus_channel or "").strip()
            return len(self._ready_rows(self._list_channel(ch)))
        return sum(
            len(self._ready_rows(self._list_channel(ch))) for ch in self.channels
        )

    def count_idea_stock(self, *, channel: str | None = None) -> int:
        """Available idea stock: policy_ok + queued + empty job_id + non-empty title.

        Rows with a job_id are already farmed / in-flight and do not count toward
        the per-channel floor (IDEA_STOCK_TARGET, default 15).
        """
        rows = self.list_rows(channel=channel)
        return sum(
            1
            for r in rows
            if r.policy_ok
            and (r.status or "queued").lower() in ("queued", "")
            and r.title.strip()
            and not (r.job_id or "").strip()
        )

    def replace_channel_rows(
        self, channel: str, rows: list[TitleRow]
    ) -> list[TitleRow]:
        """Rewrite one channel tab/CSV with ``rows`` (re-index from sheet row 2)."""
        ch = (channel or "").strip() or self.default_channel
        cleaned: list[TitleRow] = []
        for i, r in enumerate(rows):
            r.channel = ch
            r.row_index = i + 2
            cleaned.append(r)
        self._persist_channel(ch, cleaned)
        return cleaned

    def count_buffer(self, *, channel: str | None = None) -> int:
        rows = self.list_rows(channel=channel)
        return sum(
            1
            for r in rows
            if (r.status or "").lower() in ("private", "scheduled")
        )

    def resolve_profile(self, row: TitleRow) -> str:
        """Profile for a row: legacy in-memory format → channel default → env."""
        from src.services.retention_profile import normalize_format

        if (row.format or "").strip():
            return normalize_format(row.format)
        return channel_default_profile(row.channel or self.default_channel)

    # --- internals ---------------------------------------------------------

    def _channels_for(self, channel: str | None) -> list[str]:
        if channel:
            return [channel.strip()]
        if self._focus_channel:
            return [self._focus_channel]
        return list(self.channels)

    def _ready_rows(self, rows: list[TitleRow]) -> list[TitleRow]:
        """Eligible for farm: approved + policy_ok + queued + empty job_id.

        Non-empty ``job_id`` means already farmed or in-flight — never remake.
        """
        ready = [
            r
            for r in rows
            if r.approved
            and r.policy_ok
            and (r.status or "queued").lower() in ("queued", "")
            and r.title.strip()
            and not (r.job_id or "").strip()
            and not is_shorts_tab(r.channel)
            and (r.kind or "").strip().lower() != "shorts"
        ]
        ready.sort(key=lambda x: (-x.trend_score, x.row_index))
        return ready

    def inventory_approved(self, *, channel: str | None = None) -> dict[str, list[TitleRow]]:
        """Split approved+policy_ok+queued titles into ready vs skip-have-job_id."""
        chans = self._channels_for(channel)
        ready: list[TitleRow] = []
        skipped: list[TitleRow] = []
        for ch in chans:
            for r in self._list_channel(ch):
                if not (
                    r.approved
                    and r.policy_ok
                    and (r.status or "queued").lower() in ("queued", "")
                    and r.title.strip()
                ):
                    continue
                if (r.job_id or "").strip():
                    skipped.append(r)
                else:
                    ready.append(r)
        ready.sort(key=lambda x: (-x.trend_score, x.row_index))
        skipped.sort(key=lambda x: (-x.trend_score, x.row_index))
        return {"ready": ready, "skipped_have_job_id": skipped}

    def _list_channel(self, channel: str) -> list[TitleRow]:
        sheet_id = (getattr(self.s, "google_sheet_id", None) or "").strip()
        if sheet_id and (getattr(self.s, "google_sheets_credentials", None) or "").strip():
            try:
                return self._list_from_sheets(sheet_id, channel)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Sheets read failed for %r, using local CSV: %s", channel, exc
                )
        return self._list_from_csv(channel)

    def _persist(self, rows: list[TitleRow]) -> None:
        """Compat: partition aggregated rows by channel and write each tab."""
        by_ch: dict[str, list[TitleRow]] = {}
        for r in rows:
            ch = (r.channel or self.default_channel).strip()
            by_ch.setdefault(ch, []).append(r)
        for ch, ch_rows in by_ch.items():
            # Preserve row_index order within channel
            ch_rows.sort(key=lambda x: x.row_index)
            self._persist_channel(ch, ch_rows)

    def _persist_channel(self, channel: str, rows: list[TitleRow]) -> None:
        for r in rows:
            r.channel = channel
        sheet_id = (getattr(self.s, "google_sheet_id", None) or "").strip()
        if sheet_id and (getattr(self.s, "google_sheets_credentials", None) or "").strip():
            try:
                self._write_sheets(sheet_id, channel, rows)
                self._write_csv(channel, rows)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Sheets write failed for %r, local CSV only: %s", channel, exc
                )
        self._write_csv(channel, rows)

    def _list_from_csv(self, channel: str) -> list[TitleRow]:
        path = local_queue_path(channel)
        if not path.exists():
            # Migrate legacy title_queue.csv → primary channel only
            legacy = legacy_local_queue_path()
            if channel == self.default_channel and legacy.exists():
                path = legacy
            else:
                return []
        out: list[TitleRow] = []
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, start=2):
                out.append(_row_from_dict(i, row, channel=channel))
        return out

    def _write_csv(self, channel: str, rows: list[TitleRow]) -> None:
        path = local_queue_path(channel)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            cols = self.columns_for(channel)
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(_row_to_sheet_dict(r, columns=cols))
        # Keep legacy path in sync for primary (compat with old tools)
        if channel == self.default_channel:
            legacy = legacy_local_queue_path()
            try:
                legacy.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass

    def _sheets_service(self):
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        creds_path = Path(getattr(self.s, "google_sheets_credentials"))
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(str(creds_path), scopes=scopes)
        return build("sheets", "v4", credentials=creds, cache_discovery=False)

    def _resolve_sheet_tab(self, svc, sheet_id: str, preferred: str) -> str:
        """Resolve preferred tab; fall back to legacy Sheet1 only for primary."""
        preferred = (preferred or "").strip() or self.default_channel
        try:
            meta = (
                svc.spreadsheets()
                .get(spreadsheetId=sheet_id, fields="sheets.properties(title)")
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("sheet metadata failed, using tab %r: %s", preferred, exc)
            return preferred
        titles = [
            (s.get("properties") or {}).get("title")
            for s in (meta.get("sheets") or [])
            if (s.get("properties") or {}).get("title")
        ]
        if not titles:
            return preferred
        if preferred in titles:
            return preferred
        lower_map = {t.lower(): t for t in titles}
        if preferred.lower() in lower_map:
            return lower_map[preferred.lower()]
        # Primary channel may still be named Sheet1 before ensure runs
        if preferred == self.default_channel:
            for legacy in ("Sheet1", "sheet1", "Sheet 1"):
                if legacy in titles:
                    return legacy
                if legacy.lower() in lower_map:
                    return lower_map[legacy.lower()]
        logger.warning(
            "sheet_tab %r not found; using first tab %r (available=%s)",
            preferred,
            titles[0],
            titles,
        )
        return titles[0]

    def _sheet_range(self, tab: str, *, anchor: str = "A1", channel: str | None = None) -> str:
        cols = self.columns_for(channel or tab)
        end_col = _col_letters(max(len(cols), 26))
        safe_tab = tab.replace("'", "''")
        if anchor.upper() == "A1":
            return f"'{safe_tab}'!A1"
        return f"'{safe_tab}'!A:{end_col}"

    def _list_from_sheets(self, sheet_id: str, channel: str) -> list[TitleRow]:
        svc = self._sheets_service()
        tab = self._resolve_sheet_tab(svc, sheet_id, channel)
        rng = self._sheet_range(tab, anchor="A", channel=channel)
        try:
            result = (
                svc.spreadsheets()
                .values()
                .get(spreadsheetId=sheet_id, range=rng)
                .execute()
            )
        except Exception as first_exc:  # noqa: BLE001
            logger.warning("range %s failed (%s); retrying bare A:Z", rng, first_exc)
            result = (
                svc.spreadsheets()
                .values()
                .get(spreadsheetId=sheet_id, range="A:Z")
                .execute()
            )
        values = result.get("values") or []
        if not values:
            return []
        header = [str(h).strip().lower() for h in values[0]]
        out: list[TitleRow] = []
        for i, row in enumerate(values[1:], start=2):
            data = {
                header[j]: (row[j] if j < len(row) else "")
                for j in range(len(header))
            }
            out.append(_row_from_dict(i, data, channel=channel))
        return out

    def _write_sheets(self, sheet_id: str, channel: str, rows: list[TitleRow]) -> None:
        svc = self._sheets_service()
        tab = self._resolve_sheet_tab(svc, sheet_id, channel)
        cols = self.columns_for(channel)
        values = [cols]
        for r in rows:
            d = _row_to_sheet_dict(r, columns=cols)
            values.append([str(d.get(c, "")) for c in cols])
        # Clear first so deleted/shrunk queues do not leave stale rows below.
        clear_rng = self._sheet_range(tab, anchor="A", channel=channel)
        try:
            svc.spreadsheets().values().clear(
                spreadsheetId=sheet_id, range=clear_rng, body={}
            ).execute()
        except Exception as clear_exc:  # noqa: BLE001
            logger.warning(
                "sheet clear %s failed (%s); writing anyway", clear_rng, clear_exc
            )
        rng = self._sheet_range(tab, anchor="A1", channel=channel)
        try:
            svc.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=rng,
                valueInputOption="RAW",
                body={"values": values},
            ).execute()
        except Exception as first_exc:  # noqa: BLE001
            logger.warning("write range %s failed (%s); retrying A1", rng, first_exc)
            svc.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range="A1",
                valueInputOption="RAW",
                body={"values": values},
            ).execute()


def _row_from_dict(
    row_index: int,
    raw: dict[str, Any],
    *,
    title: str | None = None,
    channel: str = "",
) -> TitleRow:
    ch = (channel or str(raw.get("channel") or "")).strip()
    fmt = str(raw.get("format") or "").strip()  # optional legacy CSV only
    if not fmt and ch:
        fmt = ""  # leave blank; resolve_profile uses channel default
    return TitleRow(
        row_index=row_index,
        channel=ch,
        title=(title if title is not None else (raw.get("title") or "")).strip(),
        format=fmt,
        competitor_source=str(raw.get("competitor_source") or ""),
        source_subs=str(raw.get("source_subs") or ""),
        source_avg_recent_views=str(raw.get("source_avg_recent_views") or ""),
        source_eligible=str(raw.get("source_eligible") or ""),
        source_video_views=str(raw.get("source_video_views") or ""),
        source_video_likes=str(raw.get("source_video_likes") or ""),
        source_video_comments=str(raw.get("source_video_comments") or ""),
        source_video_uploaded_at=str(raw.get("source_video_uploaded_at") or ""),
        trend_score=float(raw.get("trend_score") or 0),
        policy_ok=_truthy(raw.get("policy_ok")),
        approved=_truthy(raw.get("approved")),
        public_approved=_truthy(raw.get("public_approved")),
        status=str(raw.get("status") or "queued").strip(),
        job_id=str(raw.get("job_id") or ""),
        video_id=str(raw.get("video_id") or ""),
        scheduled_at=str(raw.get("scheduled_at") or ""),
        notes=str(raw.get("notes") or ""),
        kind=str(raw.get("kind") or "").strip(),
        parent_title=str(raw.get("parent_title") or "").strip(),
        parent_job_id=str(raw.get("parent_job_id") or "").strip(),
        parent_video_id=str(raw.get("parent_video_id") or "").strip(),
        short_video_id=str(raw.get("short_video_id") or "").strip(),
        window_index=str(raw.get("window_index") or "0").strip() or "0",
    )


def _row_to_sheet_dict(
    r: TitleRow, *, columns: list[str] | None = None
) -> dict[str, Any]:
    full = {
        "title": r.title,
        "format": r.format,  # only written if still in columns (legacy)
        "competitor_source": r.competitor_source,
        "source_subs": r.source_subs,
        "source_avg_recent_views": r.source_avg_recent_views,
        "source_eligible": _optional_bool_cell(r.source_eligible),
        "source_video_views": r.source_video_views,
        "source_video_likes": r.source_video_likes,
        "source_video_comments": r.source_video_comments,
        "source_video_uploaded_at": r.source_video_uploaded_at,
        "trend_score": r.trend_score,
        "policy_ok": "TRUE" if r.policy_ok else "FALSE",
        "approved": "TRUE" if r.approved else "FALSE",
        "public_approved": "TRUE" if r.public_approved else "FALSE",
        "status": r.status,
        "job_id": r.job_id,
        "video_id": r.video_id,
        "scheduled_at": r.scheduled_at,
        "notes": r.notes,
        "kind": r.kind or ("shorts" if is_shorts_tab(r.channel) else ""),
        "parent_title": r.parent_title,
        "parent_job_id": r.parent_job_id,
        "parent_video_id": r.parent_video_id,
        "short_video_id": r.short_video_id or r.video_id,
        "window_index": r.window_index or "0",
    }
    if columns is None:
        return full
    return {c: full.get(c, "") for c in columns}


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("1", "true", "yes", "y", "t")


def _optional_bool_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    s = str(v).strip()
    if not s:
        return ""
    return "TRUE" if _truthy(s) else "FALSE"


def _col_letters(n: int) -> str:
    n = max(int(n), 1)
    letters = []
    while n:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(65 + rem))
    return "".join(reversed(letters))


def _load_agents_settings() -> dict[str, Any]:
    path = CONFIG_DIR / "agents_settings.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def shorts_title_queue(channel: str) -> TitleQueue:
    """Queue bound to ``{channel}_shorts`` (never used for farm pick)."""
    from src.agents.sheet_channels import shorts_tab_name

    return TitleQueue(channel=shorts_tab_name(channel))
