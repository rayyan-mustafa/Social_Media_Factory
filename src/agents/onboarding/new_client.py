"""Client onboarding CLI.

Usage:
    # Interactive mode — prompts for each field:
    python -m src.agents.onboarding.new_client

    # Pre-fill from a scraped lead in leads_LIVE.csv:
    python -m src.agents.onboarding.new_client --from-lead "Austin Best HVAC LLC"

Outputs (local only — never auto-deployed):
    clients/<slug>/config.json      — CONFIG object (used by chatbot template)
    clients/<slug>/chatbot.html     — ready-to-deploy chatbot HTML (SAMPLE banner removed)
    clients/<slug>/README.md        — internal onboarding note

The CONFIG shape is identical to what client-chatbot-demo.html expects,
so no translation layer is needed.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
LIVE_CSV = ROOT / "output" / "leadgen_live" / "leads_LIVE.csv"
CLIENTS_DIR = ROOT / "clients"
TEMPLATE_HTML = ROOT / "client-chatbot-demo.html"

# ── Default brand color per vertical ───────────────────────────────────────
_VERTICAL_COLORS: dict[str, str] = {
    "hvac":       "#1e6b4f",
    "plumbing":   "#1a4a8a",
    "electrical": "#b45309",
    "salon":      "#7c3aed",
    "clinic":     "#0f6b72",
    "dentist":    "#155e75",
    "autoshop":   "#9a1616",
    "gym":        "#166534",
    "wedding":    "#831843",
    "_":          "#1a1a2e",
}


def _slug(name: str) -> str:
    """Convert business name to a filesystem-safe slug."""
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")[:40]


def _prompt(label: str, default: str = "", required: bool = True) -> str:
    """Prompt user for a value with an optional default."""
    hint = f" [{default}]" if default else ""
    marker = "*" if required and not default else " "
    while True:
        val = input(f"  {marker} {label}{hint}: ").strip()
        if val:
            return val
        if default:
            return default
        if not required:
            return ""
        print("    ↑ This field is required.")


def _prompt_list(label: str, example: str = "") -> list[str]:
    """Prompt for a list of items (one per line, blank to finish)."""
    print(f"  * {label} (one per line, blank to finish)")
    if example:
        print(f"    Example: {example}")
    items: list[str] = []
    while True:
        val = input(f"    Item {len(items)+1}: ").strip()
        if not val:
            if items:
                break
            print("    ↑ At least one item required.")
        else:
            items.append(val)
    return items


def _prompt_services() -> list[dict[str, str]]:
    """Prompt for service list: name, price, duration."""
    print("\n  * Services (name + price + duration). Blank name to finish.")
    services: list[dict[str, str]] = []
    while True:
        name = input(f"    Service {len(services)+1} name (blank to finish): ").strip()
        if not name:
            if services:
                break
            print("    ↑ At least one service required.")
            continue
        price    = input(f"      Price for '{name}': ").strip() or "Contact for pricing"
        duration = input(f"      Duration/notes for '{name}': ").strip() or ""
        services.append({"name": name, "price": price, "duration": duration})
    return services


def _prompt_hours() -> dict[str, str]:
    """Prompt for business hours as a dict."""
    print("\n  * Hours (e.g. 'Mon-Fri: 8am-6pm'). Blank key to finish.")
    hours: dict[str, str] = {}
    while True:
        day = input(f"    Day/period (blank to finish): ").strip()
        if not day:
            if hours:
                break
            print("    ↑ At least one hours entry required.")
            continue
        time = input(f"      Hours for '{day}': ").strip() or "Call for hours"
        hours[day] = time
    return hours


def _load_from_lead(business_name: str) -> dict[str, Any] | None:
    """Pre-fill from a row in leads_LIVE.csv."""
    if not LIVE_CSV.is_file():
        print(f"  ⚠  No live CSV found at {LIVE_CSV}. Run the pipeline first.")
        return None
    with LIVE_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("business_name", "").strip().lower() == business_name.strip().lower():
                return dict(row)
    print(f"  ⚠  '{business_name}' not found in {LIVE_CSV}.")
    return None


def _collect_config(prefill: dict[str, Any] | None = None) -> dict[str, Any]:
    """Collect CONFIG fields interactively; prefill from lead data if available."""
    print("\n" + "─" * 60)
    print("  CLIENT ONBOARDING — fill in the details below")
    print("  (* = required, blank = use default in brackets)")
    print("─" * 60 + "\n")

    p = prefill or {}
    name     = _prompt("Business name", default=p.get("business_name") or "")
    vertical = _prompt("Vertical (hvac / salon / clinic / etc.)", default=p.get("category") or "hvac", required=False) or "hvac"
    color    = _prompt("Brand color (hex)", default=_VERTICAL_COLORS.get(vertical, _VERTICAL_COLORS["_"]), required=False)
    tagline  = _prompt("Tagline / one-liner", default=f"AI front desk — ask about services or book")
    location = _prompt("Location / service area", default=p.get("address") or "")
    phone    = _prompt("Phone number", default=p.get("phone") or "", required=False) or ""
    booking  = _prompt("Booking policy", default="Same-day slots when available. Emergencies get priority call-back.")
    tier     = _prompt("Client tier (1 / 2 / 3)", default="1")

    services = _prompt_services()
    hours    = _prompt_hours()

    print("\n  * Extra FAQs / caveats (one per line, blank to finish)")
    faqs: list[str] = []
    while True:
        f = input(f"    FAQ {len(faqs)+1} (blank to finish): ").strip()
        if not f:
            break
        faqs.append(f)

    return {
        "businessName":  name,
        "tagline":       tagline,
        "brandColor":    color,
        "vertical":      vertical,
        "tier":          tier,
        "services":      services,
        "hours":         hours,
        "location":      location,
        "phone":         phone,
        "bookingPolicy": booking,
        "extraFaqs":     faqs,
        "_onboarded_at": datetime.now(timezone.utc).isoformat(),
    }


def _inject_config(template_html: str, config: dict[str, Any], slug: str) -> str:
    """Inject the new CONFIG into the chatbot HTML template.

    - Replaces the existing CONFIG object
    - Removes the SAMPLE banner
    - Returns modified HTML string
    """
    # Remove the SAMPLE banner line
    html = re.sub(
        r'\s*<div class="sample-banner"[^>]*>.*?</div>\s*\n?',
        "\n",
        template_html,
        flags=re.DOTALL,
    )

    # Build the new CONFIG JS block
    services_js = json.dumps(config["services"], ensure_ascii=False, indent=4)
    hours_js    = json.dumps(config["hours"],    ensure_ascii=False, indent=4)
    faqs_js     = json.dumps(config["extraFaqs"],ensure_ascii=False, indent=4)

    new_config = (
        "const CONFIG = {\n"
        f"  businessName: {json.dumps(config['businessName'])},\n"
        f"  tagline: {json.dumps(config['tagline'])},\n"
        f"  brandColor: {json.dumps(config['brandColor'])},\n"
        f"  services: {services_js},\n"
        f"  hours: {hours_js},\n"
        f"  location: {json.dumps(config.get('location',''))},\n"
        f"  phone: {json.dumps(config.get('phone',''))},\n"
        f"  bookingPolicy: {json.dumps(config.get('bookingPolicy',''))},\n"
        f"  extraFaqs: {faqs_js}\n"
        "};"
    )

    # Replace the CONFIG block between markers
    html = re.sub(
        r"const CONFIG = \{.*?\};",
        new_config,
        html,
        flags=re.DOTALL,
        count=1,
    )

    # Update the page title
    html = re.sub(
        r"<title>.*?</title>",
        f"<title>{config['businessName']} — AI Front Desk</title>",
        html,
    )

    return html


def _write_readme(out_dir: Path, config: dict[str, Any], notes: str = "") -> Path:
    path = out_dir / "README.md"
    ts   = config.get("_onboarded_at", "")
    path.write_text(
        f"# {config['businessName']}\n\n"
        f"- **Onboarded**: {ts}\n"
        f"- **Tier**: {config.get('tier','?')}\n"
        f"- **Vertical**: {config.get('vertical','?')}\n"
        f"- **Phone**: {config.get('phone','')}\n"
        f"- **Location**: {config.get('location','')}\n\n"
        "## Notes\n\n"
        f"{notes or '(add notes here)'}\n\n"
        "## Files\n\n"
        "- `config.json` — CONFIG object for the chatbot\n"
        "- `chatbot.html` — ready-to-review chatbot HTML\n\n"
        "> Do NOT deploy until the founder has personally reviewed chatbot.html in the browser.\n",
        encoding="utf-8",
    )
    return path


def run(from_lead: str | None = None, notes: str = "") -> Path:
    """Main onboarding flow. Returns the output directory."""
    prefill: dict[str, Any] | None = None
    if from_lead:
        prefill = _load_from_lead(from_lead)
        if prefill:
            print(f"\n  ✓ Pre-filled from lead: {prefill.get('business_name')}")
            print(f"    (Review each field — edit anything that needs updating)\n")

    config = _collect_config(prefill)
    slug   = _slug(config["businessName"])
    out_dir = CLIENTS_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    # Write config.json
    config_path = out_dir / "config.json"
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # Write chatbot.html (inject config into template, remove SAMPLE banner)
    if TEMPLATE_HTML.is_file():
        template_html = TEMPLATE_HTML.read_text(encoding="utf-8")
        chatbot_html  = _inject_config(template_html, config, slug)
        html_path     = out_dir / "chatbot.html"
        html_path.write_text(chatbot_html, encoding="utf-8")
    else:
        print(f"  ⚠  Template not found: {TEMPLATE_HTML} — chatbot.html not generated.")

    # Write README
    _write_readme(out_dir, config, notes)

    print("\n" + "─" * 60)
    print(f"  ✅  Onboarding complete for: {config['businessName']}")
    print(f"  📁  Output directory: {out_dir}")
    print(f"       config.json    ← CONFIG for chatbot")
    print(f"       chatbot.html   ← review in browser before deploying")
    print(f"       README.md      ← internal onboarding note")
    print("─" * 60)
    print("\n  ⚠️  DO NOT deploy chatbot.html until you have reviewed it locally.")
    print("      No auto-deploy happens here — output is local only.\n")
    return out_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Interactive client onboarding — generates chatbot.html + config.json locally."
    )
    parser.add_argument(
        "--from-lead",
        metavar="BUSINESS_NAME",
        default=None,
        help="Pre-fill from a business name in output/leadgen_live/leads_LIVE.csv",
    )
    parser.add_argument(
        "--notes",
        default="",
        help="Optional notes to include in the client README.md",
    )
    args = parser.parse_args(argv)
    try:
        run(from_lead=args.from_lead, notes=args.notes)
    except KeyboardInterrupt:
        print("\n\n  Onboarding cancelled.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
