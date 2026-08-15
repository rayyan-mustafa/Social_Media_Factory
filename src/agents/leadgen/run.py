"""Hunt → score → draft. No SMTP to leads."""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.leadgen.audit import write_audit
from src.agents.leadgen.config import is_chain, load_chains_skip, load_region, load_vertical
from src.agents.leadgen.crm import append_ops_crm, try_write_sheet, write_csv
from src.agents.leadgen.draft import draft_for_region
from src.agents.leadgen.phantom import slug, write_phantom
from src.agents.leadgen.places import (
    PlacesError,
    place_details,
    places_api_key,
    search_nominatim,
    search_places,
)
from src.agents.leadgen.score import fetch_homepage, score_lead
from src.services.settings import ROOT

logger = logging.getLogger(__name__)

LEADGEN_OUT = ROOT / "output" / "leadgen"


def _lead_id(place_id: str, name: str) -> str:
    raw = f"{place_id}|{name}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def _normalize_place(raw: dict[str, Any], *, city: str, details: dict[str, Any] | None) -> dict[str, Any]:
    d = details or {}
    website = str(d.get("website") or raw.get("website") or "").strip()
    phone = str(
        d.get("international_phone_number")
        or d.get("formatted_phone_number")
        or raw.get("international_phone_number")
        or ""
    ).strip()
    rating = d.get("rating", raw.get("rating"))
    try:
        rating_f = float(rating) if rating is not None and rating != "" else None
    except (TypeError, ValueError):
        rating_f = None
    try:
        reviews = int(d.get("user_ratings_total") or raw.get("user_ratings_total") or 0)
    except (TypeError, ValueError):
        reviews = 0
    return {
        "place_id": str(d.get("place_id") or raw.get("place_id") or ""),
        "name": str(d.get("name") or raw.get("name") or "").strip(),
        "address": str(d.get("formatted_address") or raw.get("formatted_address") or "").strip(),
        "city": city,
        "phone": phone,
        "website": website,
        "maps_url": str(d.get("url") or raw.get("url") or "").strip(),
        "rating": rating_f,
        "reviews": reviews,
        "types": d.get("types") or raw.get("types") or [],
        "business_status": str(d.get("business_status") or raw.get("business_status") or ""),
        "source": str(raw.get("source") or "places"),
    }


def hunt_leads(
    *,
    region_id: str,
    vertical_id: str,
    city: str,
    limit: int = 25,
    fetch_sites: bool = True,
    allow_nominatim_fallback: bool = True,
) -> dict[str, Any]:
    region = load_region(region_id)
    vertical = load_vertical(vertical_id)
    skip_names = load_chains_skip()
    query = str(vertical.get("query") or vertical_id)
    lang = str(region.get("places_language") or "en")
    source = "places"
    raw_rows: list[dict[str, Any]] = []
    places_error = ""
    try:
        raw_rows = search_places(
            query=query,
            city=city,
            language=lang,
            limit=max(int(limit) * 2, int(limit)),
        )
    except PlacesError as exc:
        places_error = str(exc)[:400]
        logger.warning("Places hunt failed: %s", places_error)
        if not allow_nominatim_fallback:
            raise
        source = "nominatim"
        raw_rows = search_nominatim(query=query, city=city, limit=max(int(limit) * 2, int(limit)))

    weights = vertical.get("gap_weights") if isinstance(vertical.get("gap_weights"), dict) else {}
    channel = str(region.get("primary_channel") or "email")
    kept: list[dict[str, Any]] = []
    skipped_chain = 0
    skipped_chat = 0
    homepage_fetches = 0
    places_details_n = 0
    t0 = time.monotonic()

    for raw in raw_rows:
        pid = str(raw.get("place_id") or "")
        details: dict[str, Any] = {}
        if (
            source == "places"
            and pid
            and places_api_key()
            and not str(raw.get("website") or "").strip()
            and str(raw.get("source") or "") != "places_new"
        ):
            try:
                details = place_details(pid, language=lang)
                places_details_n += 1
                time.sleep(0.08)
            except PlacesError:
                details = {}
        lead = _normalize_place(raw, city=city, details=details or None)
        if not lead["name"]:
            continue
        if is_chain(lead["name"], skip=skip_names):
            skipped_chain += 1
            continue
        html = ""
        fetch_ok: bool | None = None
        final_url = ""
        if fetch_sites and lead["website"]:
            page = fetch_homepage(lead["website"])
            homepage_fetches += 1
            fetch_ok = bool(page.get("ok"))
            html = str(page.get("html") or "")
            final_url = str(page.get("final_url") or "")
        scored = score_lead(
            name=lead["name"],
            website=lead["website"],
            rating=lead["rating"],
            reviews=lead["reviews"],
            html=html,
            fetch_ok=fetch_ok,
            final_url=final_url,
            region_channel=channel,
            weights={k: int(v) for k, v in weights.items() if str(v).lstrip("-").isdigit()},
        )
        if scored.get("skip"):
            skipped_chat += 1
            continue
        track = str(scored.get("track") or "")
        if track not in {"no_site", "social_only", "poor_site"}:
            continue
        lead.update(scored)
        lead["vertical"] = vertical["id"]
        lead["region"] = region["id"]
        lead["lead_id"] = _lead_id(lead["place_id"], lead["name"])
        drafts = draft_for_region(lead, region=region, vertical=vertical)
        lead["channel"] = drafts["channel"]
        lead["subject"] = drafts["subject"]
        lead["draft_primary"] = drafts["primary"]
        lead["draft_email"] = drafts["email"]
        lead["draft_whatsapp"] = drafts["whatsapp"]
        kept.append(lead)
        if len(kept) >= int(limit):
            break

    kept.sort(key=lambda r: (-int(r.get("score") or 0), str(r.get("name") or "")))
    elapsed_s = time.monotonic() - t0
    return {
        "ok": True,
        "region": region["id"],
        "vertical": vertical["id"],
        "city": city,
        "source": source,
        "places_error": places_error,
        "n": len(kept),
        "skipped_chain": skipped_chain,
        "skipped_chat_stack": skipped_chat,
        "leads": kept,
        "region_pack": region,
        "vertical_pack": vertical,
        "elapsed_s": round(elapsed_s, 2),
        "homepage_fetches": homepage_fetches,
        "places_details": places_details_n,
        "places_searches": 0 if source == "nominatim" else 1,
    }


def write_run(
    hunt: dict[str, Any],
    *,
    phantom_top: bool = True,
    write_sheet: bool = True,
    run_id: str | None = None,
    cost_before: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rid = run_id or f"{ts}_{hunt.get('region')}_{hunt.get('vertical')}_{slug(str(hunt.get('city') or 'city'), max_len=24)}"
    out_dir = LEADGEN_OUT / rid
    out_dir.mkdir(parents=True, exist_ok=True)
    leads = list(hunt.get("leads") or [])
    for lead in leads:
        lead["run_id"] = rid
    audits_dir = out_dir / "audits"
    audit_paths: list[str] = []
    for lead in leads:
        ap = write_audit(
            lead,
            audits_dir,
            vertical=hunt["vertical_pack"],
            region=hunt["region_pack"],
        )
        lead["audit_path"] = str(ap)
        lead["audit_url"] = f"file://{ap.resolve()}"
        audit_paths.append(str(ap))

    phantom_path = ""
    phantom_src = next(
        (L for L in leads if str(L.get("track") or "") in {"no_site", "social_only"}),
        leads[0] if leads else None,
    )
    if phantom_top and phantom_src is not None:
        top = phantom_src
        pdir = out_dir / "phantom" / slug(str(top.get("name") or "lead"))
        path = write_phantom(
            top,
            pdir,
            vertical=hunt["vertical_pack"],
            region=hunt["region_pack"],
        )
        phantom_path = str(path)
        top["phantom_url"] = f"file://{path.resolve()}"

    for lead in leads:
        drafts = draft_for_region(lead, region=hunt["region_pack"], vertical=hunt["vertical_pack"])
        lead["channel"] = drafts["channel"]
        lead["draft_primary"] = drafts["primary"]
        lead["draft_email"] = drafts["email"]
        lead["draft_whatsapp"] = drafts["whatsapp"]
        lead["subject"] = drafts["subject"]

    csv_path = write_csv(out_dir / "leads.csv", leads)
    append_ops_crm(leads)
    drafts_md = out_dir / "drafts.md"
    lines = [
        f"# Leadgen drafts — {rid}",
        "",
        f"Region `{hunt.get('region')}` · vertical `{hunt.get('vertical')}` · {hunt.get('city')}",
        f"Source: {hunt.get('source')} · n={len(leads)}",
        "",
        "Do **not** auto-send. Copy the ones you like. Check-ups are in `audits/` (HTML, not Shopify stores).",
        "",
    ]
    for i, lead in enumerate(leads, start=1):
        lines.append(f"## {i}. {lead.get('name')}  (score {lead.get('score')} · {lead.get('track')} · {lead.get('grade')})")
        lines.append(f"- id: `{lead.get('lead_id')}`")
        lines.append(f"- gap: {', '.join(lead.get('reasons') or [])}")
        lines.append(f"- site: {lead.get('website') or '(none)'}")
        lines.append(f"- maps: {lead.get('maps_url') or ''}")
        lines.append(f"- audit: {lead.get('audit_path') or ''}")
        lines.append(f"- subject: {lead.get('subject')}")
        lines.append("")
        lines.append("```")
        lines.append(str(lead.get("draft_primary") or "").strip())
        lines.append("```")
        lines.append("")
    drafts_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sheet = try_write_sheet(leads) if write_sheet else {"ok": False, "skipped": True}
    from src.agents.leadgen.cost import actual, append_spend, forecast, write_job_sheets
    from src.agents.leadgen.watchdog import log_hunt

    before = cost_before or forecast(
        limit=max(len(leads), 1),
        source=str(hunt.get("source") or "nominatim"),
    )
    after = actual(
        source=str(hunt.get("source") or "nominatim"),
        leads_n=len(leads),
        places_searches=int(hunt.get("places_searches") or 0),
        places_details=int(hunt.get("places_details") or 0),
        elapsed_s=float(hunt.get("elapsed_s") or 0),
        homepage_fetches=int(hunt.get("homepage_fetches") or 0),
    )
    cost_md = write_job_sheets(out_dir, before=before, after=after)
    append_spend(after, run_id=rid)
    log_hunt(run_id=rid, leads_n=len(leads))
    summary = {
        "ok": True,
        "run_id": rid,
        "out_dir": str(out_dir),
        "csv": str(csv_path),
        "drafts": str(drafts_md),
        "phantom": phantom_path,
        "audits_dir": str(audits_dir),
        "audits_n": len(audit_paths),
        "cost_sheet": str(cost_md),
        "cost_before": before,
        "cost_after": after,
        "n": len(leads),
        "source": hunt.get("source"),
        "places_error": hunt.get("places_error") or "",
        "skipped_chain": hunt.get("skipped_chain"),
        "skipped_chat_stack": hunt.get("skipped_chat_stack"),
        "elapsed_s": hunt.get("elapsed_s"),
        "sheet": sheet,
        "top": [
            {
                "lead_id": L.get("lead_id"),
                "name": L.get("name"),
                "score": L.get("score"),
                "reasons": L.get("reasons"),
                "track": L.get("track"),
                "grade": L.get("grade"),
            }
            for L in leads[:10]
        ],
    }
    (out_dir / "summary.json").write_text(
        __import__("json").dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return summary
