"""Live alert email cooldown + EOD payload shape (mocked)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.agents import eod_digest as eod
from src.streaming import live_alerts as la


def test_email_allowed_same_day_same_fp(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(la, "STATE_PATH", tmp_path / "live_alert_state.json")
    monkeypatch.setattr(la, "OPS", tmp_path)
    now = datetime(2026, 8, 9, 18, 0, tzinfo=timezone.utc)
    st = {
        "channels": {
            "napping_historian": {
                "last_email_day": la._karachi_day(now),
                "last_email_fingerprint": "reconnect_storm",
                "last_email_at": now.isoformat(),
            }
        }
    }
    ok, why = la.email_allowed(
        "napping_historian", fingerprint="reconnect_storm", state=st, now=now
    )
    assert ok is False
    assert "already_emailed" in why or "cooldown" in why


def test_email_allowed_after_cooldown(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(la, "STATE_PATH", tmp_path / "live_alert_state.json")
    monkeypatch.setattr(la, "OPS", tmp_path)
    monkeypatch.setattr(la, "_cooldown_hours", lambda: 1.0)
    now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)  # next Karachi day-ish
    prev = now - timedelta(hours=7)
    st = {
        "channels": {
            "napstorian": {
                "last_email_day": "2026-08-09",
                "last_email_fingerprint": "dead_encode",
                "last_email_at": prev.isoformat(),
            }
        }
    }
    ok, why = la.email_allowed(
        "napstorian", fingerprint="dead_encode", state=st, now=now
    )
    assert ok is True
    assert why == "ok"


def test_should_escalate_attempts_exhausted() -> None:
    st = {"channels": {"napping_historian": {"attempts": 3}}}
    esc, why = la.should_escalate(
        "napping_historian",
        issues=["reconnect_storm"],
        alive=False,
        repair_claimed_ok=False,
        state=st,
    )
    assert esc is True
    assert "attempts_exhausted" in why


def test_should_escalate_verify_still_broken() -> None:
    esc, why = la.should_escalate(
        "napstorian",
        issues=["supervise_without_encode"],
        alive=False,
        repair_claimed_ok=True,
        state={"channels": {"napstorian": {"attempts": 1}}},
    )
    assert esc is True
    assert why == "verify_still_broken_after_fix"


def test_eod_payload_shape(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(eod, "OPS", tmp_path)
    monkeypatch.setattr(
        eod,
        "collect_finance_day",
        lambda day=None: {
            "day": day or "2026-08-09",
            "total_spend_usd": 1.23,
            "by_category": {"runpod": 1.0},
            "per_item": [],
            "channels_involved": ["napstorian"],
            "runpod_sessions": [],
            "finance_ticket": {},
            "month_spend_total": 10.0,
        },
    )
    monkeypatch.setattr(
        eod,
        "collect_smm_day",
        lambda day=None: {
            "day": day or "2026-08-09",
            "n_touched": 2,
            "status_counts": {"public": 1, "private": 1},
            "videos": [],
            "projection": {},
            "scorecard_path": None,
        },
    )
    monkeypatch.setattr(
        eod,
        "collect_live_status",
        lambda: {
            "channels": {
                "napstorian": {"alive": True, "issues": []},
                "napping_historian": {"alive": True, "issues": []},
            },
            "keys_configured_both": True,
            "keys_policy": "two_keys_two_channels_never_merge",
            "featured": {},
            "ts": "2026-08-09T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        eod,
        "collect_ram_status",
        lambda: {
            "available_gb": 3.1,
            "available_percent": 40.0,
            "total_gb": 7.6,
            "unhealthy": False,
            "reason": "ok",
            "last_actions": [],
        },
    )
    monkeypatch.setattr(
        eod,
        "collect_smm_changes_day",
        lambda day=None: {
            "day": day or "2026-08-09",
            "channels": {
                "napstorian": {
                    "schedule_hours": [],
                    "editing_levers": [],
                    "thumbnail_designs": [
                        {
                            "before_style": {"preset": "napstorian_punch", "map_vs_face": "either", "mood": "dramatic"},
                            "after_style": {"preset": "napstorian_punch", "map_vs_face": "face", "mood": "dramatic"},
                            "prompt_addendum": "SMM competitor-vision redesign (What-If punch):",
                            "competitor_refs": [{"video_id": "abc", "title": "Sample", "thumb_url": "https://i.ytimg.com/vi/abc/hqdefault.jpg"}],
                            "source": "smm_thumb_redesign_log",
                        }
                    ],
                    "soft_packaging": [],
                    "works": [],
                    "fails": [],
                    "experiments": [],
                    "prompt_patches": [],
                },
                "napping_historian": {
                    "schedule_hours": [],
                    "editing_levers": [],
                    "thumbnail_designs": [],
                    "soft_packaging": [],
                    "works": [],
                    "fails": [],
                    "experiments": [],
                    "prompt_patches": [],
                },
            },
        },
    )
    payload = eod.build_eod_payload(day="2026-08-09")
    assert payload["ok"] is True
    assert payload["finance"]["total_spend_usd"] == 1.23
    assert payload["smm"]["n_touched"] == 2
    assert "smm_changes" in payload
    assert "napstorian" in payload["live"]["channels"]
    assert "available_gb" in payload["ram"]
    md = eod.format_eod_markdown(payload)
    assert "EOD digest" in md
    assert "Cost / finance" in md
    assert "SMM changes today" in md
    assert "Thumbnail design changes" in md
    assert "Live (both channels)" in md
    assert "RAM" in md


def test_eod_window_and_stamp(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(eod, "EOD_STAMP", tmp_path / "eod_digest_last_day.txt")
    # 23:30 Karachi = 18:30 UTC
    late = datetime(2026, 8, 9, 18, 30, tzinfo=timezone.utc)
    assert eod.eod_window_open(late) is True
    early = datetime(2026, 8, 9, 10, 0, tzinfo=timezone.utc)
    assert eod.eod_window_open(early) is False
    assert eod.already_sent_today(late) is False
    eod.EOD_STAMP.write_text(eod.karachi_day(late) + "\n", encoding="utf-8")
    assert eod.already_sent_today(late) is True
