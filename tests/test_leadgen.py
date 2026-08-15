"""Lead-gen hunter: gap score, drafts, phantom, no auto-send."""

from __future__ import annotations

from pathlib import Path

from src.agents.leadgen.config import is_chain, load_region, load_vertical
from src.agents.leadgen.draft import draft_for_region
from src.agents.leadgen.phantom import render_phantom_html
from src.agents.leadgen.score import score_lead


def test_regions_and_verticals_load():
    us = load_region("us")
    assert us["primary_channel"] == "email"
    gulf = load_region("gulf")
    assert gulf["primary_channel"] == "whatsapp"
    salon = load_vertical("salon")
    assert "booking" in salon["offer"].lower() or "chatbot" in salon["offer"].lower()


def test_chain_skip():
    assert is_chain("Great Clips Austin North")
    assert not is_chain("Luna Color Bar")


def test_score_no_website():
    s = score_lead(name="Luna Salon", website="", rating=4.6, reviews=80, html="")
    assert s["score"] >= 30
    assert "no_website" in s["flags"]
    assert s["skip"] is False
    assert s["track"] == "no_site"


def test_social_hosts_are_not_real_sites():
    from src.agents.leadgen.score import classify_listing_website, score_lead

    assert classify_listing_website("https://instagram.com/luna")["kind"] == "social_only"
    assert classify_listing_website("https://youtube.com/@shop")["flag"] == "youtube_only"
    ig = score_lead(name="Luna", website="https://www.instagram.com/luna", rating=4.2, reviews=30)
    assert ig["track"] == "social_only"
    assert "instagram_only" in ig["flags"]


def test_poor_site_kept_without_chat_stack():
    html = "<html><head></head><body>Call us to book. Welcome.</body></html>"
    s = score_lead(
        name="Oak & Ivy Hair",
        website="https://oakivy.example",
        rating=4.5,
        reviews=40,
        html=html,
        fetch_ok=True,
        final_url="https://oakivy.example",
    )
    assert s["skip"] is False
    assert s["track"] == "poor_site"
    assert "no_booking" in s["flags"]
    assert "no_mobile" in s["flags"]


def test_score_skips_intercom():
    html = '<script src="https://widget.intercom.io/widget/abc"></script>'
    s = score_lead(
        name="Chain Spa",
        website="https://example.com",
        rating=4.8,
        reviews=200,
        html=html,
        fetch_ok=True,
        final_url="https://example.com",
    )
    assert s["skip"] is True


def test_score_no_booking_on_https_site():
    html = "<html><body>Welcome to our salon. Call us.</body></html>"
    s = score_lead(
        name="Oak & Ivy Hair",
        website="https://oakivy.example",
        rating=4.5,
        reviews=40,
        html=html,
        fetch_ok=True,
        final_url="https://oakivy.example",
    )
    assert "no_booking" in s["flags"]
    assert s["gap_count"] >= 2
    assert s["score"] >= 25


def test_draft_names_the_gap():
    region = load_region("us")
    vertical = load_vertical("salon")
    lead = {
        "name": "Oak & Ivy Hair",
        "city": "Austin, TX",
        "reasons": ["no obvious online booking"],
        "flags": ["no_booking"],
        "track": "poor_site",
        "vertical": "salon",
    }
    d = draft_for_region(lead, region=region, vertical=vertical)
    assert d["channel"] == "email"
    # LLM draft: check structural guarantees (opt-out, Google ref) not verbatim business name
    assert "STOP" in d["email"]
    assert "Google" in d["email"]
    assert len(d["email"]) > 80, "Draft should be a meaningful length"


def test_gulf_draft_is_whatsapp():
    region = load_region("gulf")
    vertical = load_vertical("salon")
    lead = {"name": "Noir Studio", "flags": ["no_website"], "track": "no_site", "vertical": "salon"}
    d = draft_for_region(lead, region=region, vertical=vertical)
    assert d["channel"] == "whatsapp"
    # LLM draft: check structural guarantees (STOP, non-empty) not verbatim business name
    assert "STOP" in d["primary"]
    assert len(d["primary"]) > 40, "WhatsApp draft should be meaningful"


def test_phantom_contains_sample_banner(tmp_path: Path):
    region = load_region("us")
    vertical = load_vertical("salon")
    html = render_phantom_html(
        {"name": "Oak & Ivy Hair", "city": "Austin, TX", "phone": "+1 512 555 0100"},
        vertical=vertical,
        region=region,
    )
    assert "SAMPLE DEMO" in html
    assert "Oak &amp; Ivy Hair" in html or "Oak & Ivy Hair" in html
    assert "not their live site" in html.lower() or "SAMPLE" in html


def test_audit_html_is_plain_language():
    from src.agents.leadgen.audit import render_audit_html

    region = load_region("us")
    vertical = load_vertical("salon")
    html = render_audit_html(
        {
            "name": "Oak & Ivy Hair",
            "city": "Austin, TX",
            "website": "https://oakivy.example",
            "track": "poor_site",
            "grade": "Needs work",
            "flags": ["no_booking", "no_mobile", "no_whatsapp"],
        },
        vertical=vertical,
        region=region,
    )
    low = html.lower()
    assert "check-up" in low
    assert "cls" not in low
    assert "lighthouse" not in low
    assert "core web vitals" not in low
    assert "Oak &amp; Ivy Hair" in html or "Oak & Ivy Hair" in html
    assert "SAMPLE" in html


def test_cli_has_audit_command():
    from src.cli.leadgen import app

    names = {c.name for c in app.registered_commands}
    assert "audit" in names
    assert "hunt" in names


def test_nominatim_forecast_is_zero():
    from src.agents.leadgen.cost import forecast

    sheet = forecast(limit=25, source="nominatim")
    assert sheet["total_usd"] == 0
    assert sheet["line_items"]["runpod_usd"] == 0
    assert sheet["line_items"]["kokoro_usd"] == 0
    assert sheet["line_items"]["places_usd"] == 0


def test_places_forecast_under_job_cap():
    from src.agents.leadgen.cost import forecast, load_rates

    sheet = forecast(limit=25, source="places")
    job_max = float((load_rates().get("caps") or {}).get("usd_per_job_max") or 1.0)
    assert sheet["line_items"]["places_usd"] > 0
    assert sheet["total_usd"] > 0
    assert sheet["total_usd"] < job_max
    assert sheet["line_items"]["runpod_usd"] == 0


def test_write_job_sheets_before_after(tmp_path: Path):
    from src.agents.leadgen.cost import forecast, actual, write_job_sheets

    before = forecast(limit=25, source="nominatim")
    after = actual(
        source="nominatim",
        leads_n=25,
        places_searches=0,
        places_details=0,
        elapsed_s=12.0,
        homepage_fetches=10,
    )
    md = write_job_sheets(tmp_path, before=before, after=after)
    text = md.read_text(encoding="utf-8")
    assert "BEFORE" in text
    assert "AFTER" in text
    assert (tmp_path / "cost_before.json").is_file()
    assert (tmp_path / "cost_after.json").is_file()


def test_watchdog_blocks_leadgen_allow_runpod(tmp_path: Path, monkeypatch):
    import src.agents.leadgen.cost as cost_mod
    import src.agents.leadgen.watchdog as wd

    monkeypatch.setenv("LEADGEN_ALLOW_RUNPOD", "1")
    monkeypatch.setattr(wd, "SPEND_LOG", tmp_path / "leadgen_spend.jsonl")
    monkeypatch.setattr(wd, "HUNT_LOG", tmp_path / "leadgen_hunts.jsonl")
    monkeypatch.setattr(wd, "LAST_PATH", tmp_path / "leadgen_watchdog_last.json")
    monkeypatch.setattr(cost_mod, "SPEND_LOG", tmp_path / "leadgen_spend.jsonl")
    pf = wd.preflight(limit=5)
    assert pf["ok"] is False
    assert any("LEADGEN_ALLOW_RUNPOD" in e for e in pf["errors"])


def test_smm_scorecard_not_youtube(tmp_path: Path, monkeypatch):
    import src.agents.leadgen.smm as smm

    monkeypatch.setattr(smm, "CRM", tmp_path / "leadgen_crm.csv")
    monkeypatch.setattr(smm, "HUNT_LOG", tmp_path / "leadgen_hunts.jsonl")
    monkeypatch.setattr(smm, "DIGEST_MD", tmp_path / "leadgen_smm_digest.md")
    monkeypatch.setattr(smm, "DIGEST_JSON", tmp_path / "leadgen_smm_digest.json")
    card = smm.scorecard()
    assert card["agent"] == "leadgen_smm"
    assert "youtube" in card["not"]


def test_cli_has_watchdog_and_smm():
    from src.cli.leadgen import app

    names = {c.name for c in app.registered_commands}
    assert "watchdog" in names
    assert "smm" in names
    assert "hunt" in names
    assert "audit" in names
    assert "email-check" in names


def test_email_auth_fail_closed_without_records(monkeypatch):
    from src.agents.leadgen import email_auth as ea

    monkeypatch.setattr(ea, "lookup_txt", lambda name, timeout=12: [])
    monkeypatch.setattr(ea, "lookup_mx", lambda name, timeout=12: [])
    out = ea.check_domain("example-agency.test")
    assert out["ok"] is False
    assert out["spf"]["ok"] is False
    assert out["dkim"]["ok"] is False
    assert out["dmarc"]["ok"] is False
    assert out["vps_may_send_smtp"] is False


def test_email_auth_passes_spf_dkim_dmarc(monkeypatch):
    from src.agents.leadgen import email_auth as ea

    def txt(name, timeout=12):
        if name == "good.test":
            return ["v=spf1 include:_spf.google.com ~all"]
        if name == "_dmarc.good.test":
            return ["v=DMARC1; p=none; rua=mailto:dmarc@good.test"]
        if name == "google._domainkey.good.test":
            return ["v=DKIM1; k=rsa; p=MIIBIjANFakeKeyForTest"]
        return []

    monkeypatch.setattr(ea, "lookup_txt", txt)
    monkeypatch.setattr(ea, "lookup_mx", lambda name, timeout=12: ["aspmx.l.google.com"])
    out = ea.check_domain("good.test")
    assert out["ok"] is True
    assert out["spf"]["ok"] is True
    assert out["dkim"]["ok"] is True
    assert out["dmarc"]["ok"] is True


def test_email_auth_forbids_gmail_from(monkeypatch):
    from src.agents.leadgen import email_auth as ea

    monkeypatch.setattr(ea, "lookup_txt", lambda name, timeout=12: ["v=spf1 include:_spf.google.com ~all"])
    monkeypatch.setattr(ea, "lookup_mx", lambda name, timeout=12: ["aspmx.l.google.com"])
    out = ea.check_domain("gmail.com")
    assert out["ok"] is False
    assert any("consumer mailbox" in e for e in out["errors"])
