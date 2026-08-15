"""Personalized email / WhatsApp drafts via LLM proxy. Never auto-sends.

Uses the same proxy/system-prompt pattern as scripts/demo_chat_proxy.py:
  POST http://127.0.0.1:8787/v1/messages  (or OPENROUTER_API_KEY / ANTHROPIC_API_KEY direct)
  Body: {model, max_tokens, system, messages}
  Response: {content: [{type: "text", text: "..."}]}

The ice-breaker line is selected from the AGENCY_MASTER §3 table based on
gap_type (track) × vertical to ensure each draft leads with ONE concrete gap.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib import error as url_error
from urllib import request as url_request

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AGENCY_MASTER §3 — ice-breaker lines keyed by (track, vertical_id)
# Fallback: ("_", "_") catches anything not explicitly listed.
# ---------------------------------------------------------------------------
_ICEBREAKERS: dict[tuple[str, str], str] = {
    # no_site
    ("no_site", "hvac"):        "I looked you up on Google — no website came up, just the listing.",
    ("no_site", "plumbing"):    "I searched you on Google Maps — there's no site link, just a number.",
    ("no_site", "electrical"):  "Your Google listing doesn't have a website, so evening callers hit voicemail.",
    ("no_site", "salon"):       "Your Google listing has no website, so booking relies entirely on calls.",
    ("no_site", "clinic"):      "I found you on Google Maps — no site, so new patients can't self-book.",
    ("no_site", "_"):           "I found you on Google — no website on your listing, just the phone number.",
    # social_only
    ("social_only", "hvac"):    "Your listing links to Instagram, not a proper site — so after-hours leads bounce.",
    ("social_only", "plumbing"):"Google sends traffic to your Facebook page, which can't capture bookings.",
    ("social_only", "_"):       "Your Google listing links to social, not a site — so leads go to an inbox, not a booking.",
    # poor_site
    ("poor_site", "hvac"):      "Your site loads but the booking button doesn't work on mobile — I tested it.",
    ("poor_site", "plumbing"):  "Your site loads but has no service-request form — calls only.",
    ("poor_site", "salon"):     "Your site loads but there's no online booking — still call-only in 2024.",
    ("poor_site", "_"):         "Your site loads but has no way to capture a lead after hours.",
    # universal fallback
    ("_", "_"):                 "I came across your Google listing and spotted a gap worth a quick note about.",
}


def _icebreaker(track: str, vertical_id: str) -> str:
    """Pick the best ice-breaker for this lead's gap type + vertical."""
    key = (track, vertical_id)
    if key in _ICEBREAKERS:
        return _ICEBREAKERS[key]
    fallback_track = (track, "_")
    if fallback_track in _ICEBREAKERS:
        return _ICEBREAKERS[fallback_track]
    return _ICEBREAKERS[("_", "_")]


# ---------------------------------------------------------------------------
# LLM call — same proxy pattern as scripts/demo_chat_proxy.py
# ---------------------------------------------------------------------------

_PROXY_URL = os.getenv("DEMO_PROXY_URL", "http://127.0.0.1:8787/v1/messages")
_OPENROUTER = (os.getenv("OPENROUTER_API_KEY") or "").strip()
_ANTHROPIC  = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
_OR_MODEL   = (os.getenv("DEMO_CHAT_MODEL") or "google/gemini-2.5-flash").strip()


def _call_llm(system: str, user_msg: str, max_tokens: int = 280) -> str:
    """Forward to the local proxy (preferred) or directly to OpenRouter/Anthropic."""
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_msg}],
    }

    # 1. Try local proxy first (same endpoint the HTML chatbots use)
    try:
        req = url_request.Request(
            _PROXY_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with url_request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        texts = [b["text"] for b in (data.get("content") or []) if b.get("type") == "text"]
        if texts:
            return "\n".join(texts).strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("proxy unreachable (%s), trying direct API", exc)

    # 2. OpenRouter fallback
    if _OPENROUTER:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user_msg}]
        body = {"model": _OR_MODEL, "max_tokens": max_tokens, "messages": messages}
        req = url_request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {_OPENROUTER}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://127.0.0.1:8787",
                "X-Title": "agency-leadgen-draft",
            },
            method="POST",
        )
        with url_request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()

    # 3. Anthropic direct fallback
    if _ANTHROPIC:
        body_a = {"model": "claude-sonnet-4-6", "max_tokens": max_tokens,
                  "system": system, "messages": [{"role": "user", "content": user_msg}]}
        req = url_request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(body_a).encode("utf-8"),
            headers={
                "x-api-key": _ANTHROPIC,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with url_request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        texts = [b["text"] for b in (data.get("content") or []) if b.get("type") == "text"]
        return "\n".join(texts).strip()

    raise RuntimeError(
        "No LLM backend available. Start scripts/demo_chat_proxy.py or set "
        "OPENROUTER_API_KEY / ANTHROPIC_API_KEY in .env"
    )


# ---------------------------------------------------------------------------
# Opt-out footer (compliance)
# ---------------------------------------------------------------------------

def _opt_out(region: dict[str, Any]) -> str:
    regime = str((region.get("compliance") or {}).get("regime") or "")
    if regime == "gdpr":
        return (
            "Found you via your public Google Business listing. "
            "Reply STOP and I will not write again."
        )
    if regime == "whatsapp_opt_in_careful":
        return "Found you on Google Maps. Reply STOP if this is a bad time."
    return (
        "How we found you: public Google Business Profile. "
        "Reply STOP to unsubscribe."
    )


# ---------------------------------------------------------------------------
# Draft generators — LLM-powered
# ---------------------------------------------------------------------------

def _build_email_system(
    lead: dict[str, Any],
    region: dict[str, Any],
    vertical: dict[str, Any],
    ice: str,
) -> str:
    offer  = str(vertical.get("offer")  or "an AI website + 24/7 booking chatbot")
    pain   = str(vertical.get("pain")   or "after-hours leads going unanswered")
    city   = str(lead.get("city")       or "").strip()
    name   = str(lead.get("name")       or "there").strip()
    track  = str(lead.get("track")      or "")
    audit  = str(lead.get("audit_url")  or "").strip()
    phantom= str(lead.get("phantom_url")or "").strip()
    opt    = _opt_out(region)

    attachments = []
    if audit:
        attachments.append(f"Include this audit link naturally: {audit}")
    if phantom:
        attachments.append(f"Include this demo page link naturally: {phantom}")

    return f"""You write cold outreach emails for a one-person agency that builds AI websites + booking chatbots for local home-service businesses.

RULES (hard):
- Lead with EXACTLY this ice-breaker as the first sentence: "{ice}"
- Write in plain, human English — NOT a sales-bot tone.
- Max 5 short paragraphs. No bullet lists.
- Close by asking for a 5-minute call or a reply — nothing more.
- End with: {opt}
- Do NOT invent prices, results, or case studies.
- Do NOT use generic phrases like "boost revenue" or "transform your business".
- No emojis.
{chr(10).join('- ' + a for a in attachments)}

CONTEXT:
- Business: {name}{(' in ' + city) if city else ''}
- Gap type: {track}
- Offer: {offer}
- Core pain this solves: {pain}
"""


def _build_whatsapp_system(
    lead: dict[str, Any],
    region: dict[str, Any],
    vertical: dict[str, Any],
    ice: str,
) -> str:
    offer   = str(vertical.get("offer")  or "AI website + chatbot")
    phantom = str(lead.get("phantom_url")or "").strip()
    opt     = _opt_out(region)
    demo    = f" Demo: {phantom}" if phantom else ""

    return f"""You write short WhatsApp cold outreach messages (max 3 sentences) for a one-person agency.

RULES:
- First sentence MUST be: "{ice}"
- Then one sentence about the offer: {offer}
- Then one sentence offering the demo or check-up.{demo}
- End with: {opt}
- No emojis. No bullet points. Plain conversational English.
- Do NOT invent prices or case studies.
"""


def draft_email(lead: dict[str, Any], *, region: dict[str, Any], vertical: dict[str, Any]) -> str:
    track  = str(lead.get("track") or "")
    vid    = str(lead.get("vertical") or vertical.get("id") or "_")
    ice    = _icebreaker(track, vid)
    system = _build_email_system(lead, region, vertical, ice)
    prompt = "Write the email now. Output the email body only — no subject line, no preamble."
    try:
        return _call_llm(system, prompt, max_tokens=320).strip() + "\n"
    except Exception as exc:  # noqa: BLE001
        logger.warning("draft_email LLM failed (%s), using static fallback", exc)
        return _static_email_fallback(lead, region, vertical, ice)


def draft_whatsapp(lead: dict[str, Any], *, region: dict[str, Any], vertical: dict[str, Any]) -> str:
    track  = str(lead.get("track") or "")
    vid    = str(lead.get("vertical") or vertical.get("id") or "_")
    ice    = _icebreaker(track, vid)
    system = _build_whatsapp_system(lead, region, vertical, ice)
    prompt = "Write the WhatsApp message now. Output the message only — no preamble."
    try:
        return _call_llm(system, prompt, max_tokens=140).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("draft_whatsapp LLM failed (%s), using static fallback", exc)
        return _static_whatsapp_fallback(lead, region, vertical, ice)


# ---------------------------------------------------------------------------
# Static fallbacks (used only when LLM is completely unreachable)
# ---------------------------------------------------------------------------

def _static_email_fallback(
    lead: dict[str, Any],
    region: dict[str, Any],
    vertical: dict[str, Any],
    ice: str,
) -> str:
    name    = str(lead.get("name") or "there").strip()
    city    = str(lead.get("city") or "").strip()
    offer   = str(vertical.get("offer") or "a 7-day AI website + chatbot")
    pain    = str(vertical.get("pain")  or "after-hours leads")
    audit   = str(lead.get("audit_url") or "").strip()
    phantom = str(lead.get("phantom_url")or "").strip()
    bits    = []
    if audit:
        bits.append("I wrote a 1-page check-up in plain English (not tech jargon).")
    if phantom:
        bits.append(f"I also mocked a sample page: {phantom}")
    extra = (" " + " ".join(bits)) if bits else ""
    loc   = f" in {city}" if city else ""
    return (
        f"Hi {name} team{loc},\n\n"
        f"{ice} "
        f"That usually means {pain}.\n\n"
        f"We install {offer} so the phone can stop being the only employee after 7pm.{extra}\n\n"
        f"If useful, I can walk through the check-up — we only build a real site after you reply.\n\n"
        f"{_opt_out(region)}\n"
    )


def _static_whatsapp_fallback(
    lead: dict[str, Any],
    region: dict[str, Any],
    vertical: dict[str, Any],
    ice: str,
) -> str:
    offer   = str(vertical.get("offer") or "AI website + WhatsApp chatbot")
    phantom = str(lead.get("phantom_url") or "").strip()
    extra   = f" Demo page: {phantom}" if phantom else ""
    return (
        f"{ice} "
        f"We build {offer} in about 7 days.{extra} "
        f"I have a 1-page check-up if useful. "
        f"Want the demo? {_opt_out(region)}"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _subject(lead: dict[str, Any], vertical: dict[str, Any]) -> str:
    name  = str(lead.get("name") or "your shop").strip()
    flags = lead.get("flags") or []
    track = str(lead.get("track") or "")
    if "no_website" in flags or track == "no_site":
        return f"{name} — your Google listing has no site"
    if track == "social_only":
        return f"{name} — Google sends people to social, not a site"
    if "no_booking" in flags:
        return f"{name} — people can find you, none can book"
    return f"{name} — quick gap on your {vertical.get('label') or 'listing'}"


def draft_for_region(lead: dict[str, Any], *, region: dict[str, Any], vertical: dict[str, Any]) -> dict[str, str]:
    channel = str(region.get("primary_channel") or "email")
    email   = draft_email(lead, region=region, vertical=vertical)
    wa      = draft_whatsapp(lead, region=region, vertical=vertical)
    primary = wa if channel == "whatsapp" else email
    return {
        "channel": channel,
        "primary": primary,
        "email":   email,
        "whatsapp":wa,
        "subject": _subject(lead, vertical),
    }
