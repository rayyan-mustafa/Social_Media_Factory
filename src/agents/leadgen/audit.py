"""Plain-language 1-page check-up. Not Lighthouse. Never auto-sends."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from src.agents.leadgen.phantom import slug
from src.agents.leadgen.score import classify_listing_website

_SOCIAL_LABEL = {
    "instagram_only": "Instagram",
    "youtube_only": "YouTube",
    "tiktok_only": "TikTok",
    "facebook_only": "Facebook",
    "link_in_bio_only": "a link-in-bio page",
}


def _yes_no(ok: bool) -> str:
    return "Yes" if ok else "No"


def _checks(lead: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Owner-facing questions. Avoid jargon (no CLS, Lighthouse, Core Web Vitals)."""
    flags = set(lead.get("flags") or [])
    website = str(lead.get("website") or "").strip()
    listing = classify_listing_website(website)
    social_flag = listing["flag"] if listing["kind"] == "social_only" else ""
    social_name = _SOCIAL_LABEL.get(social_flag, "")

    if listing["kind"] == "no_site":
        google_site = "No — Google has no website"
    elif listing["kind"] == "social_only":
        google_site = f"No — Google points at {social_name}"
    else:
        google_site = "Yes — a real website URL"

    book_ok = "no_booking" not in flags and listing["kind"] == "site"
    phone_ok = "no_mobile" not in flags and listing["kind"] == "site" and "site_down" not in flags
    after_ok = "no_whatsapp" not in flags and "reviews_no_chat" not in flags and listing["kind"] == "site"
    lock_ok = "no_ssl" not in flags and listing["kind"] == "site" and "site_down" not in flags
    if listing["kind"] != "site":
        book_ok = False
        phone_ok = False
        after_ok = False
        lock_ok = False

    return [
        ("Can a customer book without calling?", _yes_no(book_ok), "Book button on a real site"),
        ("Does Google list a real website?", google_site, "Not Facebook / Instagram / YouTube / TikTok"),
        ("Does the site work on a phone?", _yes_no(phone_ok), "Page is set up for a small screen"),
        ("Can they message you after 7pm?", _yes_no(after_ok), "WhatsApp or chat — not voicemail only"),
        ("Is the lock on (https)?", _yes_no(lock_ok), "Browsers show a padlock"),
    ]


def grade_label(lead: dict[str, Any]) -> str:
    return str(lead.get("grade") or "Needs work")


def render_audit_html(
    lead: dict[str, Any],
    *,
    vertical: dict[str, Any],
    region: dict[str, Any],
) -> str:
    name = html.escape(str(lead.get("name") or "this business"))
    city = html.escape(str(lead.get("city") or lead.get("address") or ""))
    track = str(lead.get("track") or "")
    title = (
        f"Google listing check-up for {name}"
        if track in {"no_site", "social_only"}
        else f"Website check-up for {name}"
    )
    grade = html.escape(grade_label(lead))
    pain = html.escape(str(vertical.get("pain") or "missed bookings after hours"))
    offer = html.escape(str(vertical.get("offer") or "a 7-day AI website + chatbot"))
    site = html.escape(str(lead.get("website") or "(none on Google)"))
    rows = "".join(
        f"<tr><td>{html.escape(q)}</td><td><strong>{html.escape(a)}</strong></td>"
        f"<td class='hint'>{html.escape(h)}</td></tr>"
        for q, a, h in _checks(lead)
    )
    regime = str((region.get("compliance") or {}).get("regime") or "")
    found = "public Google Business Profile"
    if regime == "whatsapp_opt_in_careful":
        found = "public Google Maps listing"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{title}</title>
  <style>
    body {{ margin:0; font-family:Georgia,ui-serif,serif; background:#f7f3ea; color:#1c1916; }}
    main {{ max-width:720px; margin:0 auto; padding:2rem 1.4rem 3rem; }}
    .banner {{ background:#5c3d2e; color:#f4e8d8; font-size:.85rem; text-align:center; padding:.5rem 1rem; }}
    h1 {{ font-size:1.7rem; margin:.4rem 0 .2rem; }}
    .sub {{ color:#5c564c; margin:0 0 1.2rem; }}
    .grade {{ display:inline-block; background:#e8c547; color:#1a1408; font-weight:700;
              padding:.25rem .7rem; border-radius:999px; font-family:ui-sans-serif,system-ui,sans-serif; }}
    table {{ width:100%; border-collapse:collapse; background:#fff; }}
    th, td {{ text-align:left; padding:.65rem .7rem; border-bottom:1px solid #e6dfd2; vertical-align:top; }}
    th {{ font-family:ui-sans-serif,system-ui,sans-serif; font-size:.75rem; letter-spacing:.04em; color:#5c564c; }}
    .hint {{ color:#6b645a; font-size:.85rem; }}
    h2 {{ font-size:1.1rem; margin:1.4rem 0 .4rem; }}
    footer {{ font-size:.78rem; color:#6b645a; margin-top:1.6rem; }}
  </style>
</head>
<body>
  <div class="banner">SAMPLE check-up — for the owner to read. Not a legal or security audit. Not a live Shopify store.</div>
  <main>
    <p class="grade">{grade}</p>
    <h1>{title}</h1>
    <p class="sub">{city}<br/>Google website: {site}</p>
    <table>
      <thead><tr><th>Question</th><th>Answer</th><th>In plain English</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <h2>What this can cost you</h2>
    <p>{pain}.</p>
    <h2>What we would do in about 7 days</h2>
    <p>Install {offer}. We only build a real site or Shopify store <strong>after you say yes</strong> — this page is the check-up, not that store.</p>
    <footer>
      How we found you: {html.escape(found)}. Based on your public listing
      and public homepage (if any). Reply STOP if you do not want another note.
      Generated for outreach; not affiliated with {name}.
    </footer>
  </main>
</body>
</html>
"""


def write_audit(
    lead: dict[str, Any],
    dest_dir: Path,
    *,
    vertical: dict[str, Any],
    region: dict[str, Any],
) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{slug(str(lead.get('name') or lead.get('lead_id') or 'lead'))}.html"
    path.write_text(render_audit_html(lead, vertical=vertical, region=region), encoding="utf-8")
    return path
