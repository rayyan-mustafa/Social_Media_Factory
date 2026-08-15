"""Competitors metrics Sheet tab + CSV fallback."""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any

from src.services.settings import CONFIG_DIR, ROOT, get_settings

logger = logging.getLogger(__name__)

LOCAL_CSV = ROOT / "output" / "ops" / "competitors_metrics.csv"
DEFAULT_TAB = "Competitors"
COLUMNS = [
    "label",
    "handle",
    "channel_id",
    "url",
    "subscribers",
    "avg_recent_views",
    "median_recent_views",
    "upload_freq_per_30d",
    "last_upload_at",
    "active_last_7_days",
    "views_last_7d_uploads",
    "eligible_for_titles",
    "metrics_updated_at",
    "notes",
]


class CompetitorsSheet:
    """One row per competitor channel with power metrics."""

    def __init__(
        self,
        tab: str | None = None,
        csv_path: Path | None = None,
    ):
        self.s = get_settings()
        self.cfg = _load_agents_settings()
        self.tab = (tab or self.cfg.get("competitors_sheet_tab") or DEFAULT_TAB).strip()
        self.columns = list(COLUMNS)
        self.csv_path = Path(csv_path) if csv_path else LOCAL_CSV
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)

    def sync_from_competitors(self, competitors: list[dict[str, Any]]) -> dict[str, Any]:
        rows = [_competitor_to_row(c) for c in competitors]
        sheet_ok = False
        sheet_id = (getattr(self.s, "google_sheet_id", None) or "").strip()
        if sheet_id and (getattr(self.s, "google_sheets_credentials", None) or "").strip():
            try:
                self._write_sheets(sheet_id, rows)
                sheet_ok = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("Competitors sheet write failed: %s", exc)
        self._write_csv(rows)
        return {
            "rows": len(rows),
            "sheet_ok": sheet_ok,
            "tab": self.tab,
            "csv": str(self.csv_path),
            "eligible": sum(1 for r in rows if _truthy(r.get("eligible_for_titles"))),
        }

    def _write_csv(self, rows: list[dict[str, Any]]) -> None:
        with self.csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.columns, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in self.columns})

    def _sheets_service(self):
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        creds_path = Path(getattr(self.s, "google_sheets_credentials"))
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(str(creds_path), scopes=scopes)
        return build("sheets", "v4", credentials=creds, cache_discovery=False)

    def _ensure_tab(self, svc, sheet_id: str) -> str:
        meta = (
            svc.spreadsheets()
            .get(spreadsheetId=sheet_id, fields="sheets.properties(sheetId,title)")
            .execute()
        )
        titles = {
            (s.get("properties") or {}).get("title"): (s.get("properties") or {}).get(
                "sheetId"
            )
            for s in (meta.get("sheets") or [])
            if (s.get("properties") or {}).get("title")
        }
        if self.tab in titles:
            return self.tab
        lower = {t.lower(): t for t in titles}
        if self.tab.lower() in lower:
            self.tab = lower[self.tab.lower()]
            return self.tab
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": self.tab}}}]},
        ).execute()
        return self.tab

    def _write_sheets(self, sheet_id: str, rows: list[dict[str, Any]]) -> None:
        svc = self._sheets_service()
        tab = self._ensure_tab(svc, sheet_id)
        values = [self.columns]
        for r in rows:
            values.append([str(r.get(c, "")) for c in self.columns])
        safe = tab.replace("'", "''")
        rng = f"'{safe}'!A1"
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=rng,
            valueInputOption="RAW",
            body={"values": values},
        ).execute()


def _competitor_to_row(c: dict[str, Any]) -> dict[str, Any]:
    handle = (c.get("handle") or "").strip()
    cid = (c.get("channel_id") or "").strip()
    url = (c.get("url") or "").strip()
    if not url:
        if handle:
            h = handle if handle.startswith("@") else f"@{handle.lstrip('@')}"
            url = f"https://www.youtube.com/{h}"
        elif cid:
            url = f"https://www.youtube.com/channel/{cid}"
    return {
        "label": c.get("label") or "",
        "handle": handle,
        "channel_id": cid,
        "url": url,
        "subscribers": c.get("subscribers", ""),
        "avg_recent_views": c.get("avg_recent_views", ""),
        "median_recent_views": c.get("median_recent_views", ""),
        "upload_freq_per_30d": c.get("upload_freq_per_30d", ""),
        "last_upload_at": c.get("last_upload_at") or "",
        "active_last_7_days": "TRUE" if c.get("active_last_7_days") else "FALSE",
        "views_last_7d_uploads": c.get("views_last_7d_uploads", ""),
        "eligible_for_titles": "TRUE" if c.get("eligible_for_titles") else "FALSE",
        "metrics_updated_at": c.get("metrics_updated_at") or "",
        "notes": c.get("notes") or "",
    }


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("1", "true", "yes", "y", "t")


def _load_agents_settings() -> dict[str, Any]:
    path = CONFIG_DIR / "agents_settings.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))
