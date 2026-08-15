"""SPF / DKIM / DMARC checks for the agency sending domain. Never sends mail."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from src.services.settings import CONFIG_DIR

POLICY_PATH = CONFIG_DIR / "leadgen" / "email_sending.json"
_DOH = "https://dns.google/resolve"
_UA = "LeadgenEmailAuth/1.0"


def load_email_policy() -> dict[str, Any]:
    if not POLICY_PATH.is_file():
        return {}
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def send_domain() -> str:
    env = (os.getenv("LEADGEN_SEND_DOMAIN") or "").strip().lower().rstrip(".")
    if env:
        return env
    pol = load_email_policy()
    return str(pol.get("send_domain") or "").strip().lower().rstrip(".")


def lookup_txt(name: str, *, timeout: int = 12) -> list[str]:
    """Public TXT via DNS-over-HTTPS. Empty list on failure."""
    q = urllib.parse.urlencode({"name": name, "type": "TXT"})
    req = urllib.request.Request(f"{_DOH}?{q}", headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, TimeoutError, json.JSONDecodeError, urllib.error.URLError):
        return []
    out: list[str] = []
    for ans in payload.get("Answer") or []:
        if int(ans.get("type") or 0) != 16:
            continue
        raw = str(ans.get("data") or "").replace('"', "").strip()
        if raw:
            out.append(raw)
    return out


def lookup_mx(name: str, *, timeout: int = 12) -> list[str]:
    q = urllib.parse.urlencode({"name": name, "type": "MX"})
    req = urllib.request.Request(f"{_DOH}?{q}", headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, TimeoutError, json.JSONDecodeError, urllib.error.URLError):
        return []
    hosts: list[str] = []
    for ans in payload.get("Answer") or []:
        if int(ans.get("type") or 0) != 15:
            continue
        data = str(ans.get("data") or "").strip()
        parts = data.split()
        host = parts[-1].rstrip(".").lower() if parts else ""
        if host:
            hosts.append(host)
    return hosts


def _spf_ok(records: list[str]) -> tuple[bool, str]:
    blob = " ".join(records).lower()
    hit = [r for r in records if r.lower().startswith("v=spf1")]
    if not hit:
        return False, "no TXT v=spf1 on the domain"
    txt = hit[0]
    if "v=spf1" not in txt.lower():
        return False, "SPF record malformed"
    if not any(x in txt.lower() for x in ("include:", "ip4:", "ip6:", "a ", "mx")):
        return False, f"SPF has no include/ip mechanism: {txt[:120]}"
    return True, txt[:200]


def _dmarc_ok(records: list[str]) -> tuple[bool, str]:
    hit = [r for r in records if "v=dmarc1" in r.lower().replace(" ", "")]
    if not hit:
        return False, "no TXT v=DMARC1 on _dmarc.<domain>"
    txt = hit[0]
    low = txt.lower().replace(" ", "")
    if "p=reject" in low or "p=quarantine" in low or "p=none" in low:
        return True, txt[:200]
    return False, f"DMARC missing p= tag: {txt[:120]}"


def _dkim_ok(domain: str, selectors: list[str]) -> tuple[bool, str, list[str]]:
    found: list[str] = []
    for sel in selectors:
        recs = lookup_txt(f"{sel}._domainkey.{domain}")
        joined = " ".join(recs).lower()
        compact = joined.replace(" ", "")
        if recs and ("v=dkim1" in compact or "p=" in compact):
            found.append(sel)
    if found:
        return True, f"DKIM selectors with keys: {', '.join(found)}", found
    tried = ", ".join(selectors)
    return False, f"no DKIM TXT on <selector>._domainkey.{domain} (tried {tried})", []


def check_domain(domain: str | None = None) -> dict[str, Any]:
    """FAIL CLOSED for outreach if SPF or DMARC missing. DKIM required by policy."""
    pol = load_email_policy()
    domain = (domain or send_domain()).strip().lower().rstrip(".")
    errors: list[str] = []
    warnings: list[str] = []
    if not domain:
        warnings.append(
            "no LEADGEN_SEND_DOMAIN / email_sending.send_domain — "
            "outreach From must not be Gmail; buy a domain first"
        )
        return {
            "ok": False,
            "ready_to_send_outreach": False,
            "domain": "",
            "spf": {"ok": False, "detail": "no domain"},
            "dkim": {"ok": False, "detail": "no domain"},
            "dmarc": {"ok": False, "detail": "no domain"},
            "mx": [],
            "errors": ["set send_domain before outreach"],
            "warnings": warnings,
            "sop": "config/sop/leadgen_email_sop.md",
            "vps_may_send_smtp": bool(pol.get("vps_may_send_smtp")),
        }

    if domain in {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com"}:
        errors.append(f"{domain} is a consumer mailbox — SOP forbids it as outreach From")

    spf_recs = lookup_txt(domain)
    spf_ok, spf_detail = _spf_ok(spf_recs)
    dmarc_recs = lookup_txt(f"_dmarc.{domain}")
    dmarc_ok, dmarc_detail = _dmarc_ok(dmarc_recs)
    selectors = list(pol.get("dkim_selectors") or ["google", "default"])
    dkim_ok, dkim_detail, dkim_found = _dkim_ok(domain, [str(s) for s in selectors])
    mx = lookup_mx(domain)

    if pol.get("require_spf", True) and not spf_ok:
        errors.append(f"SPF: {spf_detail}")
    if pol.get("require_dmarc", True) and not dmarc_ok:
        errors.append(f"DMARC: {dmarc_detail}")
    if pol.get("require_dkim", True) and not dkim_ok:
        errors.append(f"DKIM: {dkim_detail}")

    mx_l = " ".join(mx)
    if mx and not any(k in mx_l for k in ("google", "outlook", "microsoft", "protection.outlook")):
        warnings.append(
            f"MX is {mx} — SOP wants Google Workspace or Microsoft 365, not this VPS"
        )
    if not mx:
        warnings.append("no MX records yet")
    if pol.get("vps_may_send_smtp"):
        errors.append("vps_may_send_smtp is true — SOP forbids Postfix on this VPS for outreach")
    if pol.get("forbid_mailchimp_cold", True):
        warnings.append("Mailchimp cold lists remain forbidden even when DNS is green")

    ok = not errors
    return {
        "ok": ok,
        "ready_to_send_outreach": ok and bool(pol.get("human_send_until_auth_green", True)),
        "domain": domain,
        "spf": {"ok": spf_ok, "detail": spf_detail, "records": spf_recs[:5]},
        "dkim": {"ok": dkim_ok, "detail": dkim_detail, "selectors": dkim_found},
        "dmarc": {"ok": dmarc_ok, "detail": dmarc_detail, "records": dmarc_recs[:3]},
        "mx": mx,
        "errors": errors,
        "warnings": warnings,
        "sop": "config/sop/leadgen_email_sop.md",
        "vps_may_send_smtp": False,
        "note": "Checks DNS only. Does not send. Auto-SMTP to leads stays off.",
    }
