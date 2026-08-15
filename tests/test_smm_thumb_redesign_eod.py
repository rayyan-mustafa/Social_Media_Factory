"""Unit tests for SMM competitor thumb redesign cue extraction + EOD email section."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.agents import eod_digest as eod
from src.services import smm_thumb_redesign as tr


def test_youtube_thumb_urls() -> None:
    urls = tr.youtube_thumb_urls("abc123")
    assert urls[0].endswith("/abc123/maxresdefault.jpg")
    assert "hqdefault" in urls[1]


def test_collect_top_competitor_refs(tmp_path: Path) -> None:
    insp = {
        "items": [
            {
                "kind": "competitor_video",
                "video_id": "vidLow",
                "blocked_title": "Low views",
                "video_views": 10,
            },
            {
                "kind": "competitor_video",
                "video_id": "vidHigh",
                "blocked_title": "What If Rome Survived?",
                "video_views": 9000,
                "channel_label": "ahh",
            },
            {"kind": "other", "video_id": "skip"},
        ]
    }
    (tmp_path / "last_inspiration.json").write_text(json.dumps(insp), encoding="utf-8")
    refs = tr.collect_top_competitor_refs("napstorian", limit=2, ops_dir=tmp_path)
    assert len(refs) == 2
    assert refs[0]["video_id"] == "vidHigh"
    assert refs[0]["thumb_url"].endswith("/vidHigh/maxresdefault.jpg")


def test_extract_style_cues_napstorian_what_if_punch() -> None:
    analyses = [
        {
            "analyzer": "pillow",
            "map_vs_face": "face",
            "mood": "dramatic",
            "text_density": 0.5,
            "color_contrast": 0.72,
        },
        {
            "analyzer": "pillow",
            "map_vs_face": "face",
            "mood": "warm_dramatic",
            "text_density": 0.4,
            "color_contrast": 0.68,
        },
    ]
    cues = tr.extract_style_cues_from_analyses(analyses, channel="napstorian")
    assert cues["preset"] == "napstorian_punch"
    assert cues["map_vs_face"] == "face"
    assert cues["text_color"] == "yellow"
    assert cues["mood"] in {"dramatic", "warm_dramatic"}
    assert cues["n_samples"] == 2
    add = tr.build_prompt_addendum("napstorian", cues)
    assert "What-If punch" in add
    assert "face" in add.lower()


def test_extract_style_cues_historian_history_calling() -> None:
    # Competitors look face-heavy; historian must still soft-force artifact/History Calling.
    analyses = [
        {
            "analyzer": "pillow",
            "map_vs_face": "face",
            "mood": "dramatic",
            "text_density": 0.3,
            "color_contrast": 0.45,
        },
        {
            "analyzer": "vision",
            "map_vs_face": "face",
            "mood": "dramatic",
            "text_density": 0.35,
            "color_contrast": 0.5,
            "style_notes": "moody parchment close-ups dominate peers",
        },
    ]
    cues = tr.extract_style_cues_from_analyses(analyses, channel="napping_historian")
    assert cues["preset"] == "historian_soft"
    assert cues["map_vs_face"] == "artifact"
    assert cues["text_color"] == "soft_white"
    assert "calm" in cues["mood"] or "moody" in cues["mood"]
    add = tr.build_prompt_addendum("napping_historian", cues)
    assert "History Calling" in add
    assert "Secret Document" in add or "artifact" in add.lower()


def test_patch_thumbnail_template_replace(tmp_path: Path, monkeypatch) -> None:
    tpl = tmp_path / "thumbnail_template.txt"
    tpl.write_text("BASE TEMPLATE\n", encoding="utf-8")
    monkeypatch.setattr(tr, "thumbnail_template_path", lambda channel: tpl)
    a = tr.patch_thumbnail_template("napstorian", "first guidance")
    assert a["ok"] and a["action"] == "appended"
    assert "SMM THUMB REDESIGN napstorian" in tpl.read_text(encoding="utf-8")
    b = tr.patch_thumbnail_template("napstorian", "second guidance updated")
    assert b["action"] == "replaced"
    text = tpl.read_text(encoding="utf-8")
    assert "second guidance updated" in text
    assert text.count("# === SMM THUMB REDESIGN") == 1
    assert "first guidance" not in text


def test_eod_includes_thumb_redesign_section(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(eod, "OPS", tmp_path)
    monkeypatch.setattr(eod, "ROOT", tmp_path)
    day = "2026-08-10"
    # Karachi day 2026-08-10 ≈ UTC 2026-08-09 19:00 → 2026-08-10 18:59
    ts = "2026-08-10T10:00:00+00:00"

    (tmp_path / "smm_thumb_redesign_log.jsonl").write_text(
        json.dumps(
            {
                "ts": ts,
                "channel": "napstorian",
                "before": {
                    "preset": "napstorian_punch",
                    "map_vs_face": "either",
                    "mood": "dramatic",
                    "text_color": "yellow",
                },
                "after": {
                    "preset": "napstorian_punch",
                    "map_vs_face": "face",
                    "mood": "dramatic",
                    "text_color": "yellow",
                    "word_count_max": 4,
                    "contrast_boost": 1.2,
                },
                "prompt_addendum": "SMM competitor-vision redesign (What-If punch):\n- Focal: face",
                "competitor_refs": [
                    {
                        "video_id": "RUCU-4M-Qmg",
                        "title": "Dumbest Alternate History",
                        "thumb_url": "https://i.ytimg.com/vi/RUCU-4M-Qmg/maxresdefault.jpg",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "premium_editor_log.jsonl").write_text(
        json.dumps(
            {
                "ts": ts,
                "channel": "napping_historian",
                "lever": "hook_pattern",
                "value": "false_assumption",
                "reds": ["first_60s_retention_pct"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "ceo_actions.jsonl").write_text(
        json.dumps(
            {
                "at": ts,
                "type": "prompt_patch",
                "path": "/x/config/prompts/thumbnail_template.txt",
                "marker": "THUMB_NAPSTORIAN",
                "ok": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "smm_works.jsonl").write_text(
        json.dumps(
            {
                "ts": ts,
                "channel": "napstorian",
                "lever": "thumbnail_style",
                "outcome": "works",
                "note": "ctr up",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config = tmp_path / "config"
    config.mkdir()
    (config / "publish_schedule.json").write_text(
        json.dumps(
            {
                "channels": {
                    "napstorian": {
                        "preferred_hours": [4, 16],
                        "preferred_hours_source": "competitor",
                        "preferred_hours_updated_at": ts,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    # Stub OpsStore soft-packaging path to empty
    class _FakeStore:
        def _read(self, name: str):
            return []

    monkeypatch.setattr(eod, "OpsStore", _FakeStore)

    changes = eod.collect_smm_changes_day(day)
    nap = changes["channels"]["napstorian"]
    hist = changes["channels"]["napping_historian"]
    assert nap["schedule_hours"]
    assert nap["thumbnail_designs"]
    assert nap["thumbnail_designs"][0]["after_style"]["map_vs_face"] == "face"
    assert nap["prompt_patches"]
    assert nap["works"]
    assert hist["editing_levers"]
    assert hist["editing_levers"][0]["lever"] == "hook_pattern"

    monkeypatch.setattr(
        eod,
        "collect_finance_day",
        lambda day=None: {
            "day": day,
            "total_spend_usd": 0,
            "by_category": {},
            "per_item": [],
            "channels_involved": [],
            "runpod_sessions": [],
            "finance_ticket": {},
            "month_spend_total": 0,
        },
    )
    monkeypatch.setattr(
        eod,
        "collect_smm_day",
        lambda day=None: {
            "day": day,
            "n_touched": 0,
            "status_counts": {},
            "videos": [],
            "projection": {},
            "scorecard_path": None,
        },
    )
    monkeypatch.setattr(
        eod,
        "collect_live_status",
        lambda: {"channels": {}, "keys_configured_both": True, "keys_policy": "x", "featured": {}},
    )
    monkeypatch.setattr(
        eod,
        "collect_ram_status",
        lambda: {"available_gb": 4, "available_percent": 50, "total_gb": 8, "unhealthy": False, "reason": "ok"},
    )
    monkeypatch.setattr(eod, "collect_smm_changes_day", lambda day=None: changes)

    payload = eod.build_eod_payload(day=day)
    assert "smm_changes" in payload
    md = eod.format_eod_markdown(payload)
    assert "SMM changes today" in md
    assert "Thumbnail design changes" in md
    assert "What-If punch" in md or "before→after" in md
    assert "RUCU-4M-Qmg" in md
    assert "Editing levers applied" in md
    assert "false_assumption" in md
    assert "Schedule / hour" in md or "preferred_hours" in md
