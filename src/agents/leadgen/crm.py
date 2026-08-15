"""Local CSV CRM (+ optional Google Sheet tab `leadgen`). Never emails leads."""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

from src.services.settings import ROOT

logger = logging.getLogger(__name__)

CRM_COLUMNS = [
    "lead_id",
    "run_id",
    "name",
    "vertical",
    "region",
    "city",
    "address",
    "phone",
    "website",
    "maps_url",
    "rating",
    "reviews",
    "score",
    "flags",
    "reasons",
    "gap_count",
    "track",
    "grade",
    "audit_path",
    "channel",
    "subject",
    "draft_primary",
    "sent",
    "reply",
    "source",
    "place_id",
]

OPS_CRM = ROOT / "output" / "ops" / "leadgen_crm.csv"


def _row(lead: dict[str, Any]) -> dict[str, Any]:
    flags = lead.get("flags") or []
    reasons = lead.get("reasons") or []
    return {
        "lead_id": lead.get("lead_id") or "",
        "run_id": lead.get("run_id") or "",
        "name": lead.get("name") or "",
        "vertical": lead.get("vertical") or "",
        "region": lead.get("region") or "",
        "city": lead.get("city") or "",
        "address": lead.get("address") or "",
        "phone": lead.get("phone") or "",
        "website": lead.get("website") or "",
        "maps_url": lead.get("maps_url") or "",
        "rating": lead.get("rating") if lead.get("rating") is not None else "",
        "reviews": lead.get("reviews") or 0,
        "score": lead.get("score") or 0,
        "flags": ";".join(str(f) for f in flags),
        "reasons": "; ".join(str(r) for r in reasons),
        "gap_count": lead.get("gap_count") or 0,
        "track": lead.get("track") or "",
        "grade": lead.get("grade") or "",
        "audit_path": lead.get("audit_path") or "",
        "channel": lead.get("channel") or "",
        "subject": lead.get("subject") or "",
        "draft_primary": (lead.get("draft_primary") or "").replace("\n", " / "),
        "sent": lead.get("sent") or "FALSE",
        "reply": lead.get("reply") or "",
        "source": lead.get("source") or "places",
        "place_id": lead.get("place_id") or "",
    }


def write_csv(path: Path, leads: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CRM_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for lead in leads:
            w.writerow(_row(lead))
    return path


def append_ops_crm(leads: list[dict[str, Any]]) -> Path:
    OPS_CRM.parent.mkdir(parents=True, exist_ok=True)
    exists = OPS_CRM.is_file()
    with OPS_CRM.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CRM_COLUMNS, extrasaction="ignore")
        if not exists:
            w.writeheader()
        for lead in leads:
            w.writerow(_row(lead))
    return OPS_CRM


def try_write_sheet(leads: list[dict[str, Any]]) -> dict[str, Any]:
    """Best-effort Google Sheet tab `leadgen`. CSV is source of truth."""
    try:
        from src.services.settings import get_settings

        s = get_settings()
        sheet_id = (getattr(s, "google_sheet_id", None) or "").strip()
        creds = (getattr(s, "google_sheets_credentials", None) or "").strip()
        if not sheet_id or not creds:
            return {"ok": False, "skipped": True, "reason": "no sheets creds"}
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        creds_obj = Credentials.from_service_account_file(
            creds, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        svc = build("sheets", "v4", credentials=creds_obj, cache_discovery=False)
        meta = svc.spreadsheets().get(spreadsheetId=sheet_id, fields="sheets.properties(title)").execute()
        titles = [
            (sh.get("properties") or {}).get("title")
            for sh in (meta.get("sheets") or [])
        ]
        if "leadgen" not in titles:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": "leadgen"}}}]},
            ).execute()
        values = [CRM_COLUMNS]
        for lead in leads:
            d = _row(lead)
            values.append([str(d.get(c, "")) for c in CRM_COLUMNS])
        # Append rather than wipe — keep history
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range="'leadgen'!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": values if "leadgen" not in titles else values[1:] if titles else values},
        ).execute()
        return {"ok": True, "sheet_id": sheet_id, "tab": "leadgen", "n": len(leads)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("leadgen sheet write failed: %s", exc)
        return {"ok": False, "error": str(exc)[:300]}
