"""Tests for Part 3 (tracker + CLI), Part 4 (onboarding), Part 5 (WhatsApp webhook).

Run with:
    .venv/bin/pytest tests/test_pipeline_parts_3_4_5.py -v
"""

from __future__ import annotations

import csv
import json
import textwrap
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ─── Fixtures ─────────────────────────────────────────────────────────────

SAMPLE_LEAD = {
    "name":          "Austin Best HVAC LLC",
    "phone":         "+1 512 555 0199",
    "address":       "1402 S Congress Ave, Austin TX 78704",
    "website":       "",
    "vertical":      "hvac",
    "flags":         ["no_website", "no_booking"],
    "reasons":       ["no website on Google Business Profile", "no obvious online booking"],
    "score":         38,
    "track":         "no_site",
    "grade":         "Needs work",
    "lead_id":       "a1b2c3d4e5f6",
    "source":        "nominatim",
    "draft_primary": "Hi Austin Best HVAC team, I found you on Google — no website came up. ...",
    "draft_email":   "Hi Austin Best HVAC team, I found you on Google — no website came up. ...",
}

SAMPLE_CONFIG = {
    "businessName":  "Test HVAC Co",
    "tagline":       "AI front desk",
    "brandColor":    "#1e6b4f",
    "vertical":      "hvac",
    "tier":          "1",
    "services":      [{"name": "AC Repair", "price": "$150", "duration": "1hr"}],
    "hours":         {"Mon-Fri": "8am-6pm", "Emergency": "24/7"},
    "location":      "Austin, TX",
    "phone":         "+1 512 555 0100",
    "bookingPolicy": "Same-day available. Emergencies get priority.",
    "extraFaqs":     ["Licensed contractor. Sample only."],
    "_onboarded_at": "2026-08-15T12:00:00+00:00",
}


# ─── PART 3: tracker ──────────────────────────────────────────────────────

class TestTracker:
    def test_live_row_test_mode_false(self, tmp_path):
        from src.agents.leadgen.tracker import _live_row
        row = _live_row(SAMPLE_LEAD, run_id="run_001", test_mode=False)
        assert row["TEST_MODE"] == "FALSE"
        assert row["business_name"] == "Austin Best HVAC LLC"
        assert row["date_sent"] == ""
        assert row["replied"] == ""
        assert row["closed"] == ""
        assert row["priced"] == ""

    def test_live_row_test_mode_true(self, tmp_path):
        from src.agents.leadgen.tracker import _live_row
        row = _live_row(SAMPLE_LEAD, run_id="run_001", test_mode=True)
        assert row["TEST_MODE"] == "TRUE"

    def test_outcome_fields_always_empty(self, tmp_path):
        """Outcome fields must never be pre-filled — founder fills them manually."""
        from src.agents.leadgen.tracker import _live_row
        row = _live_row(SAMPLE_LEAD, run_id="run_001", test_mode=False)
        for field in ["date_sent", "sent_by", "replied", "followup_sent",
                      "priced", "closed", "tier", "value_pkr_or_usd", "notes"]:
            assert row[field] == "", f"Field '{field}' must be empty — got: {row[field]!r}"

    def test_write_live_csv_creates_file(self, tmp_path, monkeypatch):
        from src.agents.leadgen import tracker
        monkeypatch.setattr(tracker, "LIVE_DIR", tmp_path)
        monkeypatch.setattr(tracker, "LIVE_CSV", tmp_path / "leads_LIVE.csv")
        path = tracker.write_live_csv([SAMPLE_LEAD], run_id="run_001")
        assert path.is_file()
        rows = list(csv.DictReader(path.open()))
        assert len(rows) == 1
        assert rows[0]["TEST_MODE"] == "FALSE"

    def test_write_test_csv_creates_file(self, tmp_path):
        from src.agents.leadgen.tracker import write_test_csv
        out = write_test_csv([SAMPLE_LEAD], run_id="run_001", out_dir=tmp_path)
        assert out.is_file()
        rows = list(csv.DictReader(out.open()))
        assert rows[0]["TEST_MODE"] == "TRUE"

    def test_live_and_test_csvs_never_share_files(self, tmp_path, monkeypatch):
        from src.agents.leadgen import tracker
        monkeypatch.setattr(tracker, "LIVE_DIR", tmp_path / "live")
        monkeypatch.setattr(tracker, "LIVE_CSV", tmp_path / "live" / "leads_LIVE.csv")
        live = tracker.write_live_csv([SAMPLE_LEAD], run_id="live001")
        test = tracker.write_test_csv([SAMPLE_LEAD], run_id="test001", out_dir=tmp_path / "test")
        assert live != test, "Live and test CSVs must be separate files"


# ─── PART 3: CLI hard gate ─────────────────────────────────────────────────

class TestCLIHardGate:
    def test_live_mode_without_qa_flag_is_blocked(self, capsys):
        from src.agents.leadgen.cli import main
        rc = main(["--mode", "live", "--city", "Austin", "--vertical", "hvac"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "LIVE MODE BLOCKED" in captured.err
        assert "PRE_CUSTOMER_QA" in captured.err

    def test_test_mode_does_not_require_qa_flag(self, capsys, monkeypatch):
        # Patch hunt_leads and write_run so no real scraping happens
        mock_hunt = {
            "ok": True, "leads": [], "region": "default_region", "vertical": "hvac",
            "city": "Austin", "source": "nominatim", "places_error": "",
            "n": 0, "skipped_chain": 0, "skipped_chat_stack": 0,
            "elapsed_s": 0.1, "homepage_fetches": 0, "places_details": 0,
            "places_searches": 0, "region_pack": {}, "vertical_pack": {},
        }
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            mock_summary = {
                "ok": True, "run_id": "test_run", "out_dir": td,
                "csv": f"{td}/leads.csv", "drafts": f"{td}/drafts.md",
                "phantom": "", "audits_dir": td, "audits_n": 0,
                "cost_sheet": "", "cost_before": {}, "cost_after": {},
                "n": 0, "source": "nominatim", "places_error": "",
                "skipped_chain": 0, "skipped_chat_stack": 0,
                "elapsed_s": 0.1, "sheet": {"ok": False},
                "top": [],
            }
            monkeypatch.setattr("src.agents.leadgen.cli.hunt_leads",
                                lambda **kw: mock_hunt, raising=False)
            monkeypatch.setattr("src.agents.leadgen.cli.write_run",
                                lambda *a, **kw: mock_summary, raising=False)
            from src.agents.leadgen import cli as cli_mod
            with patch("src.agents.leadgen.cli.hunt_leads", return_value=mock_hunt), \
                 patch("src.agents.leadgen.cli.write_run", return_value=mock_summary), \
                 patch("src.agents.leadgen.tracker.write_test_csv", return_value=Path(td) / "t.csv"):
                rc = cli_mod.main(["--mode", "test", "--city", "Austin", "--region", "us"])
                assert rc == 0


# ─── PART 3: scoreboard ───────────────────────────────────────────────────

class TestScoreboard:
    def _write_sample_csv(self, path: Path, rows: list[dict]) -> None:
        from src.agents.leadgen.tracker import LIVE_COLUMNS
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=LIVE_COLUMNS, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)

    def _base_row(self, **kwargs) -> dict:
        base = {c: "" for c in [
            "business_name","phone","address","website_url","category","tags",
            "score","draft_text","date_scraped","date_sent","sent_by","replied",
            "followup_sent","priced","closed","tier","value_pkr_or_usd","notes",
            "TEST_MODE","lead_id","run_id","source","track","grade"
        ]}
        base["TEST_MODE"] = "FALSE"
        base["score"] = "20"
        base.update(kwargs)
        return base

    def test_no_csv_returns_message(self, tmp_path):
        from src.agents.leadgen.scoreboard import run
        result = run(csv_path=tmp_path / "missing.csv")
        assert "No live CSV found" in result

    def test_scoreboard_counts_stages(self, tmp_path):
        from src.agents.leadgen.scoreboard import run
        csv_path = tmp_path / "leads_LIVE.csv"
        rows = [
            self._base_row(business_name="A", date_sent=""),         # untouched
            self._base_row(business_name="B", date_sent="2026-08-10"),  # sent
            self._base_row(business_name="C", date_sent="2026-08-10", replied="YES"),  # replied
            self._base_row(business_name="D", date_sent="2026-08-10", replied="YES", priced="YES"),
            self._base_row(business_name="E", date_sent="2026-08-10", closed="WON"),
            self._base_row(business_name="F", TEST_MODE="TRUE"),  # should be excluded
        ]
        self._write_sample_csv(csv_path, rows)
        result = run(csv_path=csv_path)
        assert "Untouched" in result
        assert "Closed Won" in result
        assert "TEST" not in result or "TRUE" not in result.upper().replace("TEST_MODE", "")

    def test_scoreboard_skips_test_rows(self, tmp_path):
        from src.agents.leadgen.scoreboard import run
        csv_path = tmp_path / "leads_LIVE.csv"
        rows = [self._base_row(business_name="Test", TEST_MODE="TRUE")]
        self._write_sample_csv(csv_path, rows)
        result = run(csv_path=csv_path)
        assert "TOTAL LIVE LEADS IN TRACKER ..... 0" in result


# ─── PART 4: onboarding ───────────────────────────────────────────────────

class TestOnboarding:
    def test_slug_generation(self):
        from src.agents.onboarding.new_client import _slug
        assert _slug("Austin Best HVAC LLC") == "austin_best_hvac_llc"
        assert _slug("  Test Co. 123!  ") == "test_co_123"

    def test_inject_config_removes_sample_banner(self, tmp_path):
        from src.agents.onboarding.new_client import _inject_config
        template = (
            '<div class="sample-banner">SAMPLE DEMO</div>\n'
            '<script>\n'
            'const CONFIG = {\n  businessName: "Peak Heat & Air",\n'
            '  tagline: "old tagline",\n  brandColor: "#000",\n'
            '  services: [],\n  hours: {},\n  location: "",\n'
            '  phone: "",\n  bookingPolicy: "",\n  extraFaqs: []\n};\n'
            '</script>'
        )
        result = _inject_config(template, SAMPLE_CONFIG, "test_hvac_co")
        assert "SAMPLE DEMO" not in result
        assert "sample-banner" not in result

    def test_inject_config_replaces_business_name(self, tmp_path):
        from src.agents.onboarding.new_client import _inject_config
        template = (
            '<script>\n'
            'const CONFIG = {\n  businessName: "Old Name",\n'
            '  tagline: "x",\n  brandColor: "#000",\n'
            '  services: [],\n  hours: {},\n  location: "",\n'
            '  phone: "",\n  bookingPolicy: "",\n  extraFaqs: []\n};\n'
            '</script>'
        )
        result = _inject_config(template, SAMPLE_CONFIG, "test_hvac_co")
        assert "Test HVAC Co" in result
        assert "Old Name" not in result

    def test_write_readme(self, tmp_path):
        from src.agents.onboarding.new_client import _write_readme
        path = _write_readme(tmp_path, SAMPLE_CONFIG, notes="Test note")
        assert path.is_file()
        content = path.read_text()
        assert "Test HVAC Co" in content
        assert "Test note" in content
        assert "Do NOT deploy" in content

    def test_from_lead_prefill(self, tmp_path, monkeypatch):
        """--from-lead should pre-fill without inventing data not in the row."""
        from src.agents.leadgen.tracker import LIVE_COLUMNS
        csv_path = tmp_path / "leads_LIVE.csv"
        row = {c: "" for c in LIVE_COLUMNS}
        row.update({
            "business_name": "Austin Best HVAC LLC",
            "phone": "+1 512 555 0199",
            "address": "1402 S Congress Ave",
            "category": "hvac",
            "TEST_MODE": "FALSE",
        })
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            import csv as _csv
            w = _csv.DictWriter(f, fieldnames=LIVE_COLUMNS, extrasaction="ignore")
            w.writeheader()
            w.writerow(row)

        from src.agents.onboarding import new_client as nc
        monkeypatch.setattr(nc, "LIVE_CSV", csv_path)
        result = nc._load_from_lead("Austin Best HVAC LLC")
        assert result is not None
        assert result["business_name"] == "Austin Best HVAC LLC"
        assert result["phone"] == "+1 512 555 0199"
        # Should NOT invent any field not in the CSV
        assert result.get("services") is None  # not in the CSV


# ─── PART 5: WhatsApp webhook ─────────────────────────────────────────────

class TestWhatsAppWebhook:
    def _make_webhook_module(self, tmp_path, monkeypatch):
        from src.channels.whatsapp import webhook as wh
        monkeypatch.setattr(wh, "WA_NUMBERS_CONFIG", tmp_path / "whatsapp_numbers.yaml")
        monkeypatch.setattr(wh, "CLIENTS_DIR", tmp_path / "clients")
        monkeypatch.setattr(wh, "_LEAD_LOG", tmp_path / "whatsapp_leads.csv")
        return wh

    def _write_numbers_yaml(self, path: Path, slug: str, number: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"{slug}:\n  numbers:\n    - \"{number}\"\n",
            encoding="utf-8",
        )

    def _write_client_config(self, clients_dir: Path, slug: str) -> None:
        d = clients_dir / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(
            json.dumps(SAMPLE_CONFIG, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def test_unrecognized_number_gets_generic_reply(self, tmp_path, monkeypatch):
        wh = self._make_webhook_module(tmp_path, monkeypatch)
        # No numbers.yaml — empty map
        with patch.object(wh, "_send_twilio_reply", return_value=None):
            reply = wh.handle_inbound("whatsapp:+19995551234", "Hello")
        assert "get back to you" in reply.lower() or "call" in reply.lower()

    def test_known_number_routes_to_client(self, tmp_path, monkeypatch):
        wh = self._make_webhook_module(tmp_path, monkeypatch)
        self._write_numbers_yaml(tmp_path / "whatsapp_numbers.yaml", "test_hvac_co", "+15125550100")
        self._write_client_config(tmp_path / "clients", "test_hvac_co")
        proxy_reply = "Hi there! I'm the assistant for Test HVAC Co. How can I help?"
        fake_response = json.dumps({
            "content": [{"type": "text", "text": proxy_reply}]
        }).encode("utf-8")
        with patch("urllib.request.urlopen") as mock_urlopen, \
             patch.object(wh, "_send_twilio_reply", return_value=None):
            mock_cm = MagicMock()
            mock_cm.__enter__ = MagicMock(return_value=mock_cm)
            mock_cm.__exit__ = MagicMock(return_value=False)
            mock_cm.read = MagicMock(return_value=fake_response)
            mock_urlopen.return_value = mock_cm
            reply = wh.handle_inbound("whatsapp:+15125550100", "What are your hours?")
        assert proxy_reply in reply

    def test_lead_captured_token_is_logged(self, tmp_path, monkeypatch):
        wh = self._make_webhook_module(tmp_path, monkeypatch)
        self._write_numbers_yaml(tmp_path / "whatsapp_numbers.yaml", "test_hvac_co", "+15125550100")
        self._write_client_config(tmp_path / "clients", "test_hvac_co")
        proxy_reply = "Got it! I'll confirm your booking shortly.\nLEAD_CAPTURED"
        fake_response = json.dumps({
            "content": [{"type": "text", "text": proxy_reply}]
        }).encode("utf-8")
        with patch("urllib.request.urlopen") as mock_urlopen, \
             patch.object(wh, "_send_twilio_reply", return_value=None):
            mock_cm = MagicMock()
            mock_cm.__enter__ = MagicMock(return_value=mock_cm)
            mock_cm.__exit__ = MagicMock(return_value=False)
            mock_cm.read = MagicMock(return_value=fake_response)
            mock_urlopen.return_value = mock_cm
            reply = wh.handle_inbound("whatsapp:+15125550100", "I want to book AC repair for Friday")
        # Token must be stripped from reply
        assert "LEAD_CAPTURED" not in reply
        # Lead log must be written
        assert (tmp_path / "whatsapp_leads.csv").is_file()
        rows = list(csv.DictReader((tmp_path / "whatsapp_leads.csv").open()))
        assert len(rows) == 1
        assert rows[0]["client_slug"] == "test_hvac_co"

    def test_lead_captured_token_not_shown_to_user(self, tmp_path, monkeypatch):
        wh = self._make_webhook_module(tmp_path, monkeypatch)
        self._write_numbers_yaml(tmp_path / "whatsapp_numbers.yaml", "test_hvac_co", "+15125550100")
        self._write_client_config(tmp_path / "clients", "test_hvac_co")
        proxy_reply = "Perfect, see you Friday!\nLEAD_CAPTURED"
        fake_response = json.dumps({
            "content": [{"type": "text", "text": proxy_reply}]
        }).encode("utf-8")
        with patch("urllib.request.urlopen") as mock_urlopen, \
             patch.object(wh, "_send_twilio_reply", return_value=None):
            mock_cm = MagicMock()
            mock_cm.__enter__ = MagicMock(return_value=mock_cm)
            mock_cm.__exit__ = MagicMock(return_value=False)
            mock_cm.read = MagicMock(return_value=fake_response)
            mock_urlopen.return_value = mock_cm
            sent_messages = []
            wh._send_twilio_reply = lambda to, body: sent_messages.append(body)
            wh.handle_inbound("whatsapp:+15125550100", "I want to book")
        # If _send_twilio_reply was called, LEAD_CAPTURED must not be in the message
        for msg in sent_messages:
            assert "LEAD_CAPTURED" not in msg

    def test_no_sending_capability_in_leadgen_pipeline(self):
        """Confirm the leadgen pipeline has no actual email/SMS send() calls."""
        import pathlib
        leadgen_dir = pathlib.Path("src/agents/leadgen")
        # These patterns indicate actual sending, not read-only policy checks
        forbidden_calls = [
            "smtplib.SMTP(",
            ".sendmail(",
            ".send_message(",
            "client.messages.create",
            "twilio.rest",
        ]
        # email_auth.py reads a policy flag 'vps_may_send_smtp' — allowed (read-only)
        allowed_files = {"email_auth.py"}
        for pyfile in leadgen_dir.glob("*.py"):
            if pyfile.name in allowed_files:
                continue
            src = pyfile.read_text(encoding="utf-8")
            for term in forbidden_calls:
                for line in src.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if term in stripped:
                        pytest.fail(
                            f"Sending-related code found in {pyfile}: '{term}' on line: {stripped!r}"
                        )
