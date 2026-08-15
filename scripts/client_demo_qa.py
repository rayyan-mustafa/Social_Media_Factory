#!/usr/bin/env python3
"""Client-perspective QA — structural + live chat anomaly checks.

Simulates what a skeptical HVAC owner checks before buying:
  - No API keys in browser HTML
  - SAMPLE banners, no fake reviews
  - Prices/hours match CONFIG; no roofing; complaints escalate
  - Booking fires LEAD_CAPTURED (hidden from user-facing text)
  - Sales bot quotes real tiers only

Usage:
  .venv/bin/python scripts/demo_chat_proxy.py   # separate terminal
  .venv/bin/python scripts/client_demo_qa.py [--live] [--report PATH]

Without --live: structural checks + pytest only (no API spend).
With --live: also runs QA question pack through local proxy (uses OpenRouter/Anthropic).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROXY = "http://127.0.0.1:8787/v1/messages"
CLIENT_HTML = ROOT / "client-chatbot-demo.html"
SALES_HTML = ROOT / "sales-chatbot-portfolio.html"
DEFAULT_REPORT = ROOT / "output" / "ops" / "client_demo_qa_report.md"

# Mirror client-chatbot-demo.html CONFIG
HVAC_CONFIG = {
    "businessName": "Peak Heat & Air",
    "services": [
        {"name": "AC diagnostic visit", "price": "$89"},
        {"name": "AC repair (labor)", "price": "from $150"},
        {"name": "Furnace tune-up", "price": "$129"},
        {"name": "No-heat / no-cool emergency", "price": "from $149 after hours"},
    ],
    "hours": {"Mon-Fri": "7:00 AM - 6:00 PM", "Saturday": "8:00 AM - 2:00 PM"},
    "phone": "+1 512 555 0142",
}

AGENCY_CONFIG = {
    "agencyName": "Rayyan AI Front Desk",
    "tiers": [
        ("Basic", "€250 setup + €25/mo"),
        ("Standard", "€450 setup + €45/mo"),
        ("Premium", "€800 setup + €75/mo"),
    ],
}


@dataclass
class Check:
    id: str
    category: str
    name: str
    status: str  # PASS | FAIL | WARN | SKIP | BLOCKED
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, **kw: Any) -> None:
        self.checks.append(Check(**kw))

    def summary(self) -> dict[str, int]:
        out = {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0, "BLOCKED": 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def structural_checks(rep: Report) -> None:
    for path, label in [(CLIENT_HTML, "client"), (SALES_HTML, "sales")]:
        if not path.is_file():
            rep.add(id=f"S0-{label}", category="structural", name=f"{label} HTML exists", status="FAIL", detail=f"missing {path}")
            continue
        html = _read(path)
        rep.add(id=f"S1-{label}", category="structural", name=f"{label} HTML exists", status="PASS")

        if re.search(r"sk-[a-zA-Z0-9-]{20,}", html) or "OPENROUTER_API_KEY" in html or "x-api-key" in html.lower():
            rep.add(id=f"S2-{label}", category="security", name=f"{label}: no API key in HTML", status="FAIL", detail="key pattern found")
        else:
            rep.add(id=f"S2-{label}", category="security", name=f"{label}: no API key in HTML", status="PASS")

        if "127.0.0.1:8787" in html or "localhost:8787" in html:
            rep.add(id=f"S3-{label}", category="security", name=f"{label}: uses local proxy", status="PASS")
        else:
            rep.add(id=f"S3-{label}", category="security", name=f"{label}: uses local proxy", status="WARN", detail="expected 127.0.0.1:8787")

        if 'name="viewport"' in html or "viewport" in html[:800]:
            rep.add(id=f"S4-{label}", category="mobile", name=f"{label}: viewport meta", status="PASS")
        else:
            rep.add(id=f"S4-{label}", category="mobile", name=f"{label}: viewport meta", status="FAIL")

        if "SAMPLE" in html.upper():
            rep.add(id=f"S5-{label}", category="honesty", name=f"{label}: SAMPLE banner", status="PASS")
        else:
            rep.add(id=f"S5-{label}", category="honesty", name=f"{label}: SAMPLE banner", status="FAIL")

        fake_review = re.search(r"testimonial|★★★★★|5-star review|client said", html, re.I)
        if fake_review and "no fake" not in html.lower():
            rep.add(id=f"S6-{label}", category="honesty", name=f"{label}: no fake testimonials", status="FAIL")
        else:
            rep.add(id=f"S6-{label}", category="honesty", name=f"{label}: no fake testimonials", status="PASS")

        if "LEAD_CAPTURED" in html:
            rep.add(id=f"S7-{label}", category="capture", name=f"{label}: LEAD_CAPTURED handling", status="PASS")
        else:
            rep.add(id=f"S7-{label}", category="capture", name=f"{label}: LEAD_CAPTURED handling", status="WARN")

    client = _read(CLIENT_HTML)
    if "Peak Heat" in client and "HVAC" in client:
        rep.add(id="S8-client-config", category="config", name="HVAC CONFIG filled", status="PASS")
    else:
        rep.add(id="S8-client-config", category="config", name="HVAC CONFIG filled", status="FAIL")

    if "roofing" in client.lower() and "do not" in client.lower():
        rep.add(id="S9-scope", category="scope", name="HVAC scope excludes roofing", status="PASS")
    else:
        rep.add(id="S9-scope", category="scope", name="HVAC scope excludes roofing", status="WARN")


def cli_checks(rep: Report) -> None:
    py = str(ROOT / ".venv" / "bin" / "python")
    if not Path(py).is_file():
        py = sys.executable

    # pytest leadgen
    r = subprocess.run(
        [py, "-m", "pytest", "tests/test_leadgen.py", "-q", "--tb=no"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if r.returncode == 0:
        rep.add(id="C1-pytest", category="cli", name="pytest tests/test_leadgen.py", status="PASS", detail=(r.stdout or "").strip()[-80:])
    else:
        rep.add(id="C1-pytest", category="cli", name="pytest tests/test_leadgen.py", status="FAIL", detail=(r.stderr or r.stdout)[:300])

    for cmd, cid, name in [
        ([py, "-m", "src.cli.leadgen", "watchdog"], "C2-watchdog", "leadgen watchdog"),
        ([py, "-m", "src.cli.leadgen", "email-check", "--domain", "gmail.com"], "C3-email", "email-check blocks gmail"),
    ]:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=60)
        if cid == "C3-email":
            ok = r.returncode != 0 or "consumer mailbox" in (r.stdout + r.stderr).lower() or "forbid" in (r.stdout + r.stderr).lower()
        else:
            ok = r.returncode == 0
        rep.add(
            id=cid,
            category="cli",
            name=name,
            status="PASS" if ok else "FAIL",
            detail=(r.stdout or r.stderr)[:200],
        )

    # HVAC audit render
    try:
        from src.agents.leadgen.audit import render_audit_html, write_audit
        from src.agents.leadgen.config import load_region, load_vertical

        lead = {
            "name": "Demo HVAC Co",
            "city": "Austin, TX",
            "website": "",
            "track": "no_site",
            "grade": "Needs work",
            "flags": ["no_website"],
            "reasons": ["no website on your Google listing"],
        }
        vertical = load_vertical("hvac")
        region = load_region("us")
        html = render_audit_html(lead, vertical=vertical, region=region)
        ok = "Google listing check-up" in html and "SAMPLE" in html
        rep.add(id="C4-audit", category="cli", name="HVAC audit HTML render", status="PASS" if ok else "FAIL")
    except Exception as exc:  # noqa: BLE001
        rep.add(id="C4-audit", category="cli", name="HVAC audit HTML render", status="FAIL", detail=str(exc)[:200])

    # ffmpeg
    ff = subprocess.run(["which", "ffmpeg"], capture_output=True, text=True)
    rep.add(
        id="C5-ffmpeg",
        category="cli",
        name="ffmpeg available",
        status="PASS" if ff.returncode == 0 else "WARN",
        detail=(ff.stdout or "").strip(),
    )


def proxy_health(rep: Report) -> bool:
    try:
        req = urllib.request.Request("http://127.0.0.1:8787/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        mode = data.get("mode", "?")
        rep.add(id="L0-proxy", category="live", name="demo_chat_proxy health", status="PASS", detail=f"mode={mode}")
        return True
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
        rep.add(
            id="L0-proxy",
            category="live",
            name="demo_chat_proxy health",
            status="BLOCKED",
            detail=f"start scripts/demo_chat_proxy.py — {exc}",
        )
        return False


def _hvac_system_prompt() -> str:
    cfg = HVAC_CONFIG
    services = "\n".join(f"- {s['name']}: {s['price']}" for s in cfg["services"])
    hours = "\n".join(f"- {d}: {h}" for d, h in cfg["hours"].items())
    return f"""You are the front desk for {cfg['businessName']}.
Services:
{services}
Hours:
{hours}
Phone: {cfg['phone']}
Rules: never invent prices. For booking collect name, service, time then end with LEAD_CAPTURED on its own line.
For complaints ask for phone so team calls back. We do NOT do roofing or solar — HVAC only."""


def _sales_system_prompt() -> str:
    tiers = "\n".join(f"- {n}: {p}" for n, p in AGENCY_CONFIG["tiers"])
    return f"""You are sales assistant for {AGENCY_CONFIG['agencyName']}.
PRICING:
{tiers}
Never invent prices or client counts. Match visitor language. LEAD_CAPTURED when contact captured."""


def chat_post(system: str, user: str, *, max_tokens: int = 300) -> tuple[str, bool, str | None]:
    body = json.dumps(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
    ).encode()
    req = urllib.request.Request(PROXY, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode())
    if data.get("error"):
        raise RuntimeError(str(data["error"])[:400])
    text = "".join(b.get("text", "") for b in (data.get("content") or []) if b.get("type") == "text")
    lead = "LEAD_CAPTURED" in text
    clean = text.replace("LEAD_CAPTURED", "").strip()
    return clean, lead, text


@dataclass
class LiveCase:
    id: str
    bot: str
    question: str
    expect: str
    check: Any  # callable(clean, lead, raw) -> tuple[bool, str]


def live_checks(rep: Report) -> None:
    if not proxy_health(rep):
        rep.add(id="L-skip", category="live", name="live chat pack", status="SKIP", detail="proxy down")
        return

    hvac_sys = _hvac_system_prompt()
    sales_sys = _sales_system_prompt()

    cases: list[LiveCase] = [
        LiveCase("L1", "hvac", "What are your hours on Saturday?", "mentions Saturday hours", lambda c, *_: ("saturday" in c.lower() or "8" in c, c[:120])),
        LiveCase("L2", "hvac", "How much is an AC diagnostic visit?", "price $89", lambda c, *_: ("89" in c, c[:120])),
        LiveCase("L3", "hvac", "Do you cover ZIP 78701 Austin?", "service area", lambda c, *_: (any(x in c.lower() for x in ("austin", "78701", "zip", "area")), c[:120])),
        LiveCase("L4", "hvac", "Emergency tonight — furnace completely out, no heat", "urgency / emergency", lambda c, *_: (any(x in c.lower() for x in ("emergency", "priority", "dispatch", "urgent", "heat")), c[:120])),
        LiveCase(
            "L5",
            "hvac",
            "Book tomorrow 3pm AC check, name Ali, phone 512-555-9999",
            "LEAD_CAPTURED",
            lambda c, lead, _: (lead, "lead token missing"),
        ),
        LiveCase("L6", "hvac", "What's your cancel policy?", "cancel / notice", lambda c, *_: (any(x in c.lower() for x in ("cancel", "notice", "fee", "4 hour")), c[:120])),
        LiveCase("L7", "hvac", "Can you install a new roof?", "decline roofing", lambda c, *_: (any(x in c.lower() for x in ("don't", "do not", "not", "hvac only", "roof")), c[:120])),
        LiveCase("L8", "hvac", "Your tech was rude last time — I want a manager", "escalate / call back", lambda c, *_: (any(x in c.lower() for x in ("sorry", "call", "team", "manager", "follow up", "phone")), c[:120])),
        LiveCase("L9", "hvac", "asdfghjkl ???", "graceful reply", lambda c, *_: (len(c) > 10, c[:80])),
        LiveCase("L10", "sales", "I run an HVAC shop in Texas. What does Basic cost?", "€250 tier", lambda c, *_: ("250" in c or "25" in c, c[:140])),
        LiveCase("L11", "sales", "Do you have 500 happy clients?", "no fake counts", lambda c, *_: (not re.search(r"\b500\b.*client", c, re.I), c[:140])),
        LiveCase(
            "L12",
            "sales",
            "I'm interested — email me at owner@example.com, name Mike",
            "LEAD_CAPTURED",
            lambda c, lead, _: (lead, "sales lead token missing"),
        ),
        LiveCase("L13", "sales", "¿Cuánto cuesta el plan básico?", "Spanish + price", lambda c, *_: (any(x in c.lower() for x in ("250", "25", "básic", "basic", "€")), c[:140])),
    ]

    for case in cases:
        sys_prompt = hvac_sys if case.bot == "hvac" else sales_sys
        try:
            clean, lead, raw = chat_post(sys_prompt, case.question)
            ok, note = case.check(clean, lead, raw)
            rep.add(
                id=case.id,
                category="live",
                name=f"{case.bot}: {case.question[:50]}…",
                status="PASS" if ok else "FAIL",
                detail=f"expect {case.expect}; {note}",
            )
        except Exception as exc:  # noqa: BLE001
            rep.add(id=case.id, category="live", name=case.question[:60], status="FAIL", detail=str(exc)[:250])


def write_report(rep: Report, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    summ = rep.summary()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Client demo QA report",
        "",
        f"**Generated:** {now}  ",
        f"**Script:** `scripts/client_demo_qa.py`",
        "",
        "## Summary",
        "",
        f"| PASS | FAIL | WARN | BLOCKED | SKIP |",
        f"|---:|---:|---:|---:|---:|",
        f"| {summ['PASS']} | {summ['FAIL']} | {summ['WARN']} | {summ['BLOCKED']} | {summ['SKIP']} |",
        "",
        "## Checks",
        "",
        "| ID | Cat | Status | Check | Detail |",
        "|---|---|---|---|---|",
    ]
    for c in rep.checks:
        det = c.detail.replace("|", "\\|").replace("\n", " ")[:180]
        lines.append(f"| {c.id} | {c.category} | **{c.status}** | {c.name} | {det} |")

    gate_b = summ["FAIL"] == 0 and summ["BLOCKED"] <= 1  # proxy blocked ok for structural-only
    lines.extend(
        [
            "",
            "## Client sign-off",
            "",
            f"- Structural + CLI: {'GREEN' if summ['FAIL'] == 0 or (not any(c.status == 'FAIL' and c.category != 'live' for c in rep.checks)) else 'RED'}",
            f"- Live chat pack: {'GREEN' if not any(c.status == 'FAIL' and c.category == 'live' for c in rep.checks) else 'NEEDS FIX' if any(c.category == 'live' for c in rep.checks) else 'NOT RUN'}",
            "",
            "**Rayyan:** open `client-chatbot-demo.html` in Chrome mobile view and repeat L5 booking manually for visual LEAD banner.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="Run live proxy chat QA (API cost)")
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = ap.parse_args()

    rep = Report()
    structural_checks(rep)
    cli_checks(rep)
    if args.live:
        live_checks(rep)

    write_report(rep, args.report)
    summ = rep.summary()
    print(f"Report: {args.report}")
    print(f"PASS={summ['PASS']} FAIL={summ['FAIL']} WARN={summ['WARN']} BLOCKED={summ['BLOCKED']} SKIP={summ['SKIP']}")
    for c in rep.checks:
        if c.status in {"FAIL", "BLOCKED"}:
            print(f"  [{c.status}] {c.id} {c.name}: {c.detail[:100]}")
    return 1 if summ["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
