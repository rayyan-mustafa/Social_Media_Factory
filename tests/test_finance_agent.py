"""FinanceAgent open-market price ticket tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.agents.cost_guardian import CostGuardian
from src.agents.finance_agent import (
    DEFAULT_BASE,
    FinanceAgent,
    TICKET_BEGIN,
    TICKET_END,
    render_cto_snippet,
)
from src.agents.store import OpsStore


@pytest.fixture()
def fin_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FinanceAgent:
    monkeypatch.setenv("SHEET_CHANNELS", "napstorian,napping_historian")
    monkeypatch.setenv("TTS_BACKEND", "kokoro")
    monkeypatch.setenv("RUNPOD_WATCHDOG", "1")
    monkeypatch.setenv("RUNPOD_AUTO_START", "1")
    monkeypatch.setenv("RUNPOD_PREP_VOICE", "1")
    monkeypatch.setenv("RUNPOD_PREP_WHILE_GPU", "1")
    monkeypatch.setenv("RUNPOD_KILL_ON_ERROR", "1")
    monkeypatch.setenv("MAX_GPU_CONCURRENT", "1")
    monkeypatch.setenv("MAX_PREP_CONCURRENT", "1")
    monkeypatch.setenv("RUNPOD_LEARN_LOOKBACK_DAYS", "365")
    monkeypatch.setenv("IDEA_STOCK_TARGET", "15")
    store = OpsStore(tmp_path / "ops")
    # Seed one proven terminal job (Armada-class proof signal)
    store.create_job(
        title="What If the Spanish Armada Had Won?",
        status="private",
        stage="private",
    )
    agent = FinanceAgent(store=store, ops_dir=tmp_path / "ops")
    agent.fin = {
        "enabled": True,
        "recheck_hours": 24,
        "pkr_per_usd": 280,
        "base_floor_usd": DEFAULT_BASE["floor_usd"],
        "base_easy_ask_usd": DEFAULT_BASE["easy_ask_usd"],
        "base_stretch_usd": DEFAULT_BASE["stretch_usd"],
        "update_cto_status": True,
    }
    return agent


def test_compute_ticket_bands_and_category(fin_env: FinanceAgent):
    ticket = fin_env.compute_ticket()
    h = ticket["headline"]
    assert ticket["category"].startswith("productized")
    assert ticket["live_params"]["not_saas_yet"] is True
    assert ticket["live_params"]["channel_count"] == 2
    assert 0.15 <= float(ticket["live_params"]["per_video_usd"]) <= 0.80
    assert h["floor_usd"] < h["easy_ask_usd"] < h["stretch_usd"]
    # CTO-realistic indie band — not $100k SaaS
    assert 2000 <= h["floor_usd"] <= 8000
    assert 5000 <= h["easy_ask_usd"] <= 18000
    assert 12000 <= h["stretch_usd"] <= 35000
    assert h["stretch_usd"] < 50000
    assert h["easy_ask_pkr"] == pytest.approx(h["easy_ask_usd"] * 280)
    assert ticket["score"]["overall_score"] >= 55
    assert any("Unit economics" in d for d in ticket["drivers"])


def test_write_ticket_and_recheck_skip(fin_env: FinanceAgent, tmp_path: Path):
    out = fin_env.maybe_recompute(force=True, update_cto=False)
    assert out["skipped"] is False
    assert fin_env.ticket_path.exists()
    data = json.loads(fin_env.ticket_path.read_text(encoding="utf-8"))
    assert data["headline"]["easy_ask_usd"] == out["headline"]["easy_ask_usd"]
    assert fin_env.ticket_jsonl.exists()

    skip = fin_env.maybe_recompute(force=False, update_cto=False)
    assert skip["skipped"] is True
    assert "fresh" in skip["reason"]


def test_due_after_interval(fin_env: FinanceAgent):
    fin_env.write_ticket(fin_env.compute_ticket())
    raw = json.loads(fin_env.ticket_path.read_text(encoding="utf-8"))
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    raw["at"] = old.isoformat()
    fin_env.ticket_path.write_text(json.dumps(raw), encoding="utf-8")
    due, reason = fin_env.due_for_recheck()
    assert due is True
    assert "age" in reason


def test_cto_status_markers(fin_env: FinanceAgent, tmp_path: Path):
    cto = tmp_path / "ops" / "CTO_STATUS.md"
    cto.write_text(
        "# CTO status — investor one-pager (Rayyan)\n\n"
        "**Updated:** test\n\n## Bottom line\n\nok\n",
        encoding="utf-8",
    )
    ticket = fin_env.compute_ticket()
    assert fin_env.update_cto_status(ticket) is True
    body = cto.read_text(encoding="utf-8")
    assert TICKET_BEGIN in body and TICKET_END in body
    assert "Price ticket" in body
    assert f"${ticket['headline']['easy_ask_usd']:,.0f}" in body
    # Idempotent replace
    ticket2 = dict(ticket)
    ticket2["headline"] = dict(ticket["headline"])
    ticket2["headline"]["easy_ask_usd"] = 11100.0
    fin_env.update_cto_status(ticket2)
    body2 = cto.read_text(encoding="utf-8")
    assert body2.count(TICKET_BEGIN) == 1
    assert "$11,100" in body2


def test_cheaper_video_raises_or_holds_score(fin_env: FinanceAgent):
    cheap = fin_env.collect_live_params()
    cheap["per_video_usd"] = 0.20
    expensive = dict(cheap)
    expensive["per_video_usd"] = 2.50
    s_cheap = fin_env.score_factors(cheap)
    s_exp = fin_env.score_factors(expensive)
    assert s_cheap["factors"]["unit_economics"] > s_exp["factors"]["unit_economics"]
    assert s_cheap["overall_score"] > s_exp["overall_score"]


def test_render_snippet_mentions_not_saas():
    fake = {
        "at": "2026-08-07T00:00:00+00:00",
        "headline": {
            "floor_usd": 3500,
            "easy_ask_usd": 9500,
            "stretch_usd": 22000,
            "floor_pkr": 980000,
            "easy_ask_pkr": 2660000,
            "stretch_pkr": 6160000,
        },
        "ticket": {"pkr_per_usd": 280},
        "score": {"overall_score": 77},
        "live_params": {
            "per_video_usd": 0.31,
            "channels": ["napstorian", "napping_historian"],
            "tts_backend": "kokoro",
            "stills_backend": "pod",
            "max_gpu_concurrent": 1,
        },
        "drivers": ["Unit economics ~$0.31"],
    }
    snip = render_cto_snippet(fake)
    assert "not" in snip.lower() and "saas" in snip.lower()
    assert "$9,500" in snip


def test_cost_guardian_still_works_alongside(fin_env: FinanceAgent):
    g = CostGuardian(store=fin_env.store)
    ok, msg = g.check_can_start_job(estimated_usd=0.31)
    assert ok
    assert "cap=" in msg
