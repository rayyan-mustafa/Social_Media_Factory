"""Fetch a public homepage and score AI-website / chatbot gaps."""

from __future__ import annotations

import re
import ssl
import urllib.error
import urllib.request
from typing import Any

_UA = (
    "Mozilla/5.0 (compatible; LeadgenGapBot/1.0; +https://example.invalid/agency)"
)

CHAT_MARKERS = (
    "intercom",
    "tidio",
    "manychat",
    "crisp.chat",
    "crisp.im",
    "drift.com",
    "js.driftt.com",
    "zendesk",
    "hubspot",
    "tawk.to",
    "livechat",
    "gorgias",
    "ada.support",
)
BOOKING_MARKERS = (
    "book now",
    "book online",
    "appointment",
    "calendly",
    "cal.com",
    "vagaro",
    "fresha",
    "square.site",
    "squareup",
    "acuityscheduling",
    "setmore",
    "styleseat",
    "mindbodyonline",
    "booksy",
)
WHATSAPP_MARKERS = ("wa.me/", "api.whatsapp.com", "whatsapp.com/send", "whatsapp")

# GBP "website" that is actually a social profile — not a real shop site.
# Classify from the URL Google already shows. Do not scrape IG/FB/YT.
_SOCIAL_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("instagram_only", r"instagram\.com|instagr\.am", "Google website is Instagram only"),
    ("youtube_only", r"youtube\.com|youtu\.be", "Google website is a YouTube channel"),
    ("tiktok_only", r"tiktok\.com", "Google website is TikTok only"),
    ("facebook_only", r"facebook\.com|\bfb\.com\b|fb\.me", "Google website is Facebook only"),
    ("link_in_bio_only", r"linktr\.ee|beacons\.ai|bio\.site|carrd\.co", "Google website is a link-in-bio page"),
)

_SOCIAL_FLAGS = {row[0] for row in _SOCIAL_PATTERNS}
_GAP_FLAGS = {
    "no_website",
    "no_ssl",
    "no_booking",
    "no_whatsapp",
    "site_down",
    "no_mobile",
    *_SOCIAL_FLAGS,
}


def classify_listing_website(website: str) -> dict[str, str]:
    """Map a GBP website URL to no_site / social_only / site. No social-network crawl."""
    site = (website or "").strip()
    if not site:
        return {
            "kind": "no_site",
            "flag": "no_website",
            "reason": "no website on Google Business Profile",
        }
    blob = site.lower()
    for flag, pat, reason in _SOCIAL_PATTERNS:
        if re.search(pat, blob):
            return {"kind": "social_only", "flag": flag, "reason": reason}
    return {"kind": "site", "flag": "", "reason": ""}


def _has_mobile_viewport(html: str) -> bool:
    return bool(re.search(r"<meta[^>]+name=['\"]viewport['\"]", html or "", re.I))


def fetch_homepage(url: str, *, timeout: int = 10) -> dict[str, Any]:
    site = (url or "").strip()
    if not site:
        return {"ok": False, "url": "", "html": "", "final_url": "", "error": "no_url"}
    if not site.startswith("http"):
        site = "https://" + site
    ctx = ssl.create_default_context()
    req = urllib.request.Request(site, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            final = str(resp.geturl() or site)
            raw = resp.read(400_000)
        html = raw.decode("utf-8", errors="replace")
        return {"ok": True, "url": site, "html": html, "final_url": final, "error": ""}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ssl.SSLError, OSError) as exc:
        err = str(exc)[:180]
        if site.startswith("https://"):
            http = "http://" + site.split("://", 1)[-1]
            try:
                req2 = urllib.request.Request(http, headers={"User-Agent": _UA})
                with urllib.request.urlopen(req2, timeout=timeout, context=ctx) as resp:
                    final = str(resp.geturl() or http)
                    raw = resp.read(400_000)
                html = raw.decode("utf-8", errors="replace")
                return {
                    "ok": True,
                    "url": http,
                    "html": html,
                    "final_url": final,
                    "error": "",
                    "http_fallback": True,
                }
            except Exception:  # noqa: BLE001
                pass
        return {"ok": False, "url": site, "html": "", "final_url": "", "error": err}


def _has_any(blob: str, needles: tuple[str, ...]) -> bool:
    return any(n in blob for n in needles)


def score_lead(
    *,
    name: str,
    website: str,
    rating: float | None,
    reviews: int,
    html: str = "",
    fetch_ok: bool | None = None,
    final_url: str = "",
    region_channel: str = "email",
    weights: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Return gap score 0–100 plus flags. High = better outreach target."""
    w = {
        "no_website": 30,
        "facebook_only": 20,
        "instagram_only": 20,
        "youtube_only": 20,
        "tiktok_only": 20,
        "link_in_bio_only": 18,
        "no_mobile": 10,
        "no_ssl": 10,
        "no_booking": 15,
        "no_whatsapp": 10,
        "reviews_no_chat": 15,
        "newish": 8,
        **(weights or {}),
    }
    site = (website or "").strip()
    low = (html or "").lower()
    final = (final_url or site).lower()
    flags: list[str] = []
    reasons: list[str] = []
    score = 0
    skip = False

    if _has_any(low, CHAT_MARKERS):
        skip = True
        flags.append("has_chat_stack")
        reasons.append("already has Intercom/Tidio/ManyChat-class chat")

    listing = classify_listing_website(site)
    kind = listing["kind"]
    no_site = kind == "no_site"
    social_only = kind == "social_only"
    if no_site:
        score += int(w["no_website"])
        flags.append("no_website")
        reasons.append(listing["reason"])
    elif social_only:
        flag = listing["flag"]
        score += int(w.get(flag) or w["facebook_only"])
        flags.append(flag)
        reasons.append(listing["reason"])

    if site and fetch_ok is False:
        score += 12
        flags.append("site_down")
        reasons.append("listed site did not load")

    if site and not social_only and final.startswith("http://") and not final.startswith("https://"):
        score += int(w["no_ssl"])
        flags.append("no_ssl")
        reasons.append("site is not on HTTPS")

    if site and not no_site and not social_only:
        if fetch_ok and low and not _has_mobile_viewport(html or ""):
            score += int(w["no_mobile"])
            flags.append("no_mobile")
            reasons.append("site does not look set up for phones")
        if not _has_any(low, BOOKING_MARKERS):
            score += int(w["no_booking"])
            flags.append("no_booking")
            reasons.append("no obvious online booking")
        if not _has_any(low, WHATSAPP_MARKERS):
            wa = int(w["no_whatsapp"])
            if region_channel != "whatsapp":
                wa = max(4, wa // 2)
            score += wa
            flags.append("no_whatsapp")
            reasons.append("no WhatsApp click-to-chat")

    r = float(rating) if rating is not None else 0.0
    nrev = int(reviews or 0)
    if r >= 4.0 and nrev >= 20 and "has_chat_stack" not in flags:
        if not _has_any(low, CHAT_MARKERS):
            score += int(w["reviews_no_chat"])
            flags.append("reviews_no_chat")
            reasons.append(f"{nrev} reviews / {r:.1f}★ but no chatbot")

    if nrev and nrev < 15:
        score += int(w["newish"])
        flags.append("newish")
        reasons.append("few reviews — likely newer or under-invested online")

    score = max(0, min(100, score))
    gap_count = sum(1 for f in flags if f in _GAP_FLAGS)
    poor_marks = {
        "no_booking",
        "no_whatsapp",
        "no_ssl",
        "site_down",
        "no_mobile",
        "reviews_no_chat",
    }
    if no_site:
        track = "no_site"
    elif social_only:
        track = "social_only"
    elif skip:
        track = "has_stack"
    elif any(f in poor_marks for f in flags):
        track = "poor_site"
    else:
        track = "strong"
    if track in {"no_site", "social_only", "poor_site"}:
        grade = "Needs work" if gap_count >= 3 or no_site or social_only or "site_down" in flags else "OK"
    else:
        grade = "Strong"
    return {
        "score": score,
        "flags": flags,
        "reasons": reasons,
        "skip": skip,
        "gap_count": gap_count,
        "track": track,
        "grade": grade,
        "name": name,
    }
