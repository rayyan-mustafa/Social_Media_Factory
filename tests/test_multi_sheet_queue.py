"""Multi-sheet title queue: napstorian + napping_historian pick/harvest."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_configured_channels_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SHEET_CHANNELS", raising=False)
    from src.agents import sheet_channels as sc

    # agents_settings now lists both
    chans = sc.configured_sheet_channels()
    assert "napstorian" in chans
    assert "napping_historian" in chans


def test_round_robin_order():
    from src.agents.sheet_channels import pick_channel_order, round_robin_channel_order

    ch = ["napstorian", "napping_historian"]
    assert round_robin_channel_order(ch, last_channel=None) == ch
    assert round_robin_channel_order(ch, last_channel="napstorian") == [
        "napping_historian",
        "napstorian",
    ]
    assert round_robin_channel_order(ch, last_channel="napping_historian") == [
        "napstorian",
        "napping_historian",
    ]
    # After last pick → classic RR
    assert pick_channel_order(ch, last_channel="napstorian") == [
        "napping_historian",
        "napstorian",
    ]
    # Cold start: never-farmed / older farm first among ready stock
    assert pick_channel_order(
        ch,
        last_channel=None,
        last_farm_at_by_channel={"napstorian": "2026-08-08T12:00:00+00:00"},
        channels_with_ready={"napstorian", "napping_historian"},
    ) == ["napping_historian", "napstorian"]
    # Cold start: only napstorian has stock → still prefer it despite older hist farm
    assert pick_channel_order(
        ch,
        last_channel=None,
        last_farm_at_by_channel={
            "napping_historian": "2026-01-01T00:00:00+00:00",
            "napstorian": "2026-08-08T12:00:00+00:00",
        },
        channels_with_ready={"napstorian"},
    )[0] == "napstorian"


def test_default_columns_have_no_format():
    from src.agents.title_queue import DEFAULT_COLUMNS

    assert "format" not in DEFAULT_COLUMNS
    assert DEFAULT_COLUMNS[0] == "title"
    assert "approved" in DEFAULT_COLUMNS
    assert "policy_ok" in DEFAULT_COLUMNS


def test_channel_default_profile(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("RETENTION_PROFILE", raising=False)
    monkeypatch.delenv("SHEET_CHANNEL_PROFILE_NAPSTORIAN", raising=False)
    monkeypatch.delenv("SHEET_CHANNEL_PROFILE_NAPPING_HISTORIAN", raising=False)
    from src.agents.sheet_channels import channel_default_profile

    assert channel_default_profile("napstorian") == "retention"
    assert channel_default_profile("napping_historian") == "epic"
    monkeypatch.setenv("SHEET_CHANNEL_PROFILE_NAPPING_HISTORIAN", "retention")
    assert channel_default_profile("napping_historian") == "retention"


def test_multi_sheet_pick_round_robin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("SHEET_CHANNELS", "napstorian,napping_historian")
    import src.agents.sheet_channels as sc
    import src.agents.title_queue as tq
    from src.services import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    monkeypatch.setattr(sc, "PICK_STATE_PATH", tmp_path / "pick_state.json")
    # TitleQueue imported local_queue_path by name — patch both
    path_fn = lambda ch: tmp_path / f"title_queue_{ch}.csv"
    monkeypatch.setattr(sc, "local_queue_path", path_fn)
    monkeypatch.setattr(tq, "local_queue_path", path_fn)
    monkeypatch.setattr(sc, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")
    monkeypatch.setattr(tq, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")
    monkeypatch.setattr(sc, "load_pick_state", lambda: sc.load_pick_state())
    # Rebind pick state helpers used by TitleQueue
    monkeypatch.setattr(
        tq,
        "load_pick_state",
        lambda: (
            __import__("json").loads((tmp_path / "pick_state.json").read_text())
            if (tmp_path / "pick_state.json").exists()
            else {}
        ),
    )

    def _save(last: str, *, farmed_at: str | None = None) -> None:
        import json
        from datetime import datetime, timezone

        path = tmp_path / "pick_state.json"
        prev = json.loads(path.read_text()) if path.exists() else {}
        by = dict(prev.get("last_farm_at_by_channel") or {})
        by[last] = farmed_at or datetime.now(timezone.utc).isoformat()
        path.write_text(
            json.dumps({"last_channel": last, "last_farm_at_by_channel": by}) + "\n"
        )

    monkeypatch.setattr(tq, "save_pick_state", _save)
    monkeypatch.setattr(tq, "last_farm_at_from_jobs", lambda: {})

    q = tq.TitleQueue()
    q.append_titles(
        [
            {
                "title": "What If Napstorian Idea One?",
                "policy_ok": True,
                "approved": True,
                "trend_score": 0.5,
            }
        ],
        channel="napstorian",
    )
    q.append_titles(
        [
            {
                "title": "What If Historian Idea One?",
                "policy_ok": True,
                "approved": True,
                "trend_score": 0.9,
            }
        ],
        channel="napping_historian",
    )

    # Cold start, no prior farms → configured order (napstorian first)
    first = q.pick_approved(limit=1)
    assert len(first) == 1
    assert first[0].channel == "napstorian"

    second = q.pick_approved(limit=1)
    assert len(second) == 1
    assert second[0].channel == "napping_historian"

    # pick_approved does not mutate status — both still ready until enqueue
    assert q.count_pending_ready() == 2
    settings_mod.get_settings.cache_clear()


def test_cold_start_prefers_least_recent_farm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Cold start: with both ready, pick the channel farmed least recently."""
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("SHEET_CHANNELS", "napstorian,napping_historian")
    import src.agents.sheet_channels as sc
    import src.agents.title_queue as tq
    from src.services import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    path_fn = lambda ch: tmp_path / f"title_queue_{ch}.csv"
    monkeypatch.setattr(sc, "local_queue_path", path_fn)
    monkeypatch.setattr(tq, "local_queue_path", path_fn)
    monkeypatch.setattr(sc, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")
    monkeypatch.setattr(tq, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")
    monkeypatch.setattr(tq, "last_farm_at_from_jobs", lambda: {})
    # No last_channel — cold start; napstorian farmed recently, historian never
    monkeypatch.setattr(
        tq,
        "load_pick_state",
        lambda: {
            "last_farm_at_by_channel": {
                "napstorian": "2026-08-08T12:00:00+00:00",
            }
        },
    )
    saved: list[str] = []
    monkeypatch.setattr(tq, "save_pick_state", lambda last, **_kw: saved.append(last))

    q = tq.TitleQueue()
    q.append_titles(
        [
            {
                "title": "What If Nap Ready?",
                "policy_ok": True,
                "approved": True,
                "trend_score": 0.99,
            }
        ],
        channel="napstorian",
    )
    q.append_titles(
        [
            {
                "title": "What If Hist Ready?",
                "policy_ok": True,
                "approved": True,
                "trend_score": 0.1,
            }
        ],
        channel="napping_historian",
    )
    picked = q.pick_approved(limit=1)
    assert len(picked) == 1
    assert picked[0].channel == "napping_historian"
    assert q.resolve_profile(picked[0]) == "epic"
    assert saved == ["napping_historian"]
    settings_mod.get_settings.cache_clear()


def test_count_pending_ready_does_not_mutate_pick_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("SHEET_CHANNELS", "napstorian,napping_historian")
    import src.agents.sheet_channels as sc
    import src.agents.title_queue as tq
    from src.services import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    path_fn = lambda ch: tmp_path / f"title_queue_{ch}.csv"
    monkeypatch.setattr(sc, "local_queue_path", path_fn)
    monkeypatch.setattr(tq, "local_queue_path", path_fn)
    monkeypatch.setattr(sc, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")
    monkeypatch.setattr(tq, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")
    monkeypatch.setattr(
        tq, "load_pick_state", lambda: {"last_channel": "napstorian"}
    )
    saved: list[str] = []
    monkeypatch.setattr(tq, "save_pick_state", lambda last, **_kw: saved.append(last))

    q = tq.TitleQueue()
    q.append_titles(
        [
            {
                "title": "What If Count Me?",
                "policy_ok": True,
                "approved": True,
                "trend_score": 0.5,
            }
        ],
        channel="napping_historian",
    )
    assert q.count_pending_ready() == 1
    assert saved == []
    preview = q.pick_approved(limit=1, mutate_state=False)
    assert preview[0].channel == "napping_historian"
    assert saved == []
    settings_mod.get_settings.cache_clear()

def test_multi_sheet_harvest_per_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("SHEET_CHANNELS", "napstorian,napping_historian")
    monkeypatch.setenv("PUBLISH_SCHEDULE", "1")
    monkeypatch.setenv("IDEA_STOCK_TARGET", "2")

    import src.agents.idea_stock as idea_mod
    import src.agents.sheet_channels as sc
    import src.agents.title_queue as tq
    from src.services import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    path_fn = lambda ch: tmp_path / f"title_queue_{ch}.csv"
    monkeypatch.setattr(sc, "local_queue_path", path_fn)
    monkeypatch.setattr(tq, "local_queue_path", path_fn)
    monkeypatch.setattr(sc, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")
    monkeypatch.setattr(tq, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")

    q = tq.TitleQueue()
    q.append_titles(
        [
            {
                "title": f"What If Stock Nap {i}?",
                "policy_ok": True,
                "approved": False,
                "trend_score": 0.4,
            }
            for i in range(2)
        ],
        channel="napstorian",
    )

    calls: list[tuple[str | None, int | None]] = []

    class FakeTrends:
        def __init__(self, *a, **k):
            pass

        def harvest_titles(self, *, dry_run=False, channel=None, limit=None):
            calls.append((channel, limit))
            rows = [
                {
                    "title": f"What If Harvested {channel}?",
                    "policy_ok": True,
                    "approved": False,
                    "trend_score": 0.6,
                    "status": "queued",
                }
            ]
            q.append_titles(rows, channel=channel)
            return rows

    monkeypatch.setattr(idea_mod, "TrendsAgent", FakeTrends)
    monkeypatch.setattr(idea_mod, "OpsStore", lambda: object())
    monkeypatch.setattr(
        idea_mod,
        "OpsLedger",
        lambda *_a, **_k: type("L", (), {})(),
    )

    out = idea_mod.maybe_refill_idea_stock(
        queue=q, dry_run=False, skip_hygiene=True
    )
    assert out["trigger"] == "publish_schedule"
    assert out["channels"]["napstorian"]["skipped"] is True
    assert out["channels"]["napping_historian"]["harvested"] == 1
    assert out["channels"]["napping_historian"]["need"] == 2
    assert calls == [("napping_historian", 2)]
    assert q.count_idea_stock(channel="napping_historian") == 1
    assert q.count_idea_stock(channel="napstorian") == 2
    settings_mod.get_settings.cache_clear()


def test_harvest_one_public_path_fills_both_sheets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Even 1 public-path title unlocks refill; both understocked sheets top up."""
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("SHEET_CHANNELS", "napstorian,napping_historian")
    monkeypatch.setenv("PUBLISH_SCHEDULE", "0")
    monkeypatch.setenv("IDEA_STOCK_TARGET", "3")
    monkeypatch.delenv("SHEET_PUBLISH_CHANNELS", raising=False)

    import src.agents.idea_stock as idea_mod
    import src.agents.sheet_channels as sc
    import src.agents.title_queue as tq
    from src.services import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    path_fn = lambda ch: tmp_path / f"title_queue_{ch}.csv"
    monkeypatch.setattr(sc, "local_queue_path", path_fn)
    monkeypatch.setattr(tq, "local_queue_path", path_fn)
    monkeypatch.setattr(sc, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")
    monkeypatch.setattr(tq, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")

    q = tq.TitleQueue()
    # Only ONE title on public schedule path (napstorian) — still refill BOTH sheets
    q.append_titles(
        [
            {
                "title": "What If Only One Public?",
                "policy_ok": True,
                "approved": True,
                "public_approved": True,
                "status": "scheduled",
                "scheduled_at": "2026-08-08T04:00:00+05:00",
                "trend_score": 0.9,
            }
        ],
        channel="napstorian",
    )
    # Thin stock on both (scheduled row is not queued stock)
    q.append_titles(
        [
            {
                "title": "What If Nap Stock?",
                "policy_ok": True,
                "approved": False,
                "status": "queued",
                "trend_score": 0.4,
            }
        ],
        channel="napstorian",
    )
    q.append_titles(
        [
            {
                "title": "The Secret History of the Tudor Court",
                "policy_ok": True,
                "approved": False,
                "status": "queued",
                "trend_score": 0.4,
            }
        ],
        channel="napping_historian",
    )

    assert q.count_idea_stock(channel="napstorian") == 1
    assert q.count_idea_stock(channel="napping_historian") == 1
    assert idea_mod.count_publish_path_titles(q) == 1

    limits: dict[str, int | None] = {}

    class FakeTrends:
        def __init__(self, *a, **k):
            pass

        def harvest_titles(self, *, dry_run=False, channel=None, limit=None):
            limits[channel] = limit
            prefix = (
                "Dark History New"
                if channel == "napping_historian"
                else "What If New"
            )
            rows = [
                {
                    "title": f"{prefix} {channel} {i}?",
                    "policy_ok": True,
                    "approved": False,
                    "status": "queued",
                    "trend_score": 0.5,
                }
                for i in range(limit or 1)
            ]
            q.append_titles(rows, channel=channel)
            return rows

    monkeypatch.setattr(idea_mod, "TrendsAgent", FakeTrends)
    monkeypatch.setattr(idea_mod, "OpsStore", lambda: object())
    monkeypatch.setattr(
        idea_mod,
        "OpsLedger",
        lambda *_a, **_k: type("L", (), {})(),
    )

    out = idea_mod.maybe_refill_idea_stock(
        queue=q, dry_run=False, skip_hygiene=True
    )
    assert out["triggered"] is True
    assert out["trigger"].startswith("publish_path_title")
    assert out["target_per_channel"] == 3
    assert set(limits) == {"napstorian", "napping_historian"}
    assert limits["napstorian"] == 2
    assert limits["napping_historian"] == 2
    assert out["channels"]["napstorian"]["harvested"] == 2
    assert out["channels"]["napping_historian"]["harvested"] == 2
    assert q.count_idea_stock(channel="napstorian") == 3
    assert q.count_idea_stock(channel="napping_historian") == 3
    # New ideas stay unapproved for Rayyan
    for ch in ("napstorian", "napping_historian"):
        for r in q.list_rows(channel=ch):
            if r.status == "queued" and "New" in r.title:
                assert r.approved is False
    settings_mod.get_settings.cache_clear()


def test_harvest_skips_without_publish_or_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("SHEET_CHANNELS", "napstorian,napping_historian")
    monkeypatch.setenv("PUBLISH_SCHEDULE", "0")
    monkeypatch.setenv("IDEA_STOCK_TARGET", "15")

    import src.agents.idea_stock as idea_mod
    import src.agents.sheet_channels as sc
    import src.agents.title_queue as tq
    from src.services import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    path_fn = lambda ch: tmp_path / f"title_queue_{ch}.csv"
    monkeypatch.setattr(sc, "local_queue_path", path_fn)
    monkeypatch.setattr(tq, "local_queue_path", path_fn)
    monkeypatch.setattr(sc, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")
    monkeypatch.setattr(tq, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")

    q = tq.TitleQueue()
    q.append_titles(
        [{"title": "What If Private Only?", "policy_ok": True, "status": "queued"}],
        channel="napstorian",
    )
    called = []

    class FakeTrends:
        def __init__(self, *a, **k):
            pass

        def harvest_titles(self, **kw):
            called.append(kw)
            return []

    monkeypatch.setattr(idea_mod, "TrendsAgent", FakeTrends)
    out = idea_mod.maybe_refill_idea_stock(
        queue=q, dry_run=False, skip_hygiene=True
    )
    assert out["skipped"] is True
    assert called == []
    assert "no publish" in str(out.get("reason", "")).lower()
    settings_mod.get_settings.cache_clear()


def test_pick_skips_rows_with_job_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Hard rule: approved rows with non-empty job_id are never remade."""
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("SHEET_CHANNELS", "napstorian,napping_historian")
    import src.agents.sheet_channels as sc
    import src.agents.title_queue as tq
    from src.services import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    path_fn = lambda ch: tmp_path / f"title_queue_{ch}.csv"
    monkeypatch.setattr(sc, "local_queue_path", path_fn)
    monkeypatch.setattr(tq, "local_queue_path", path_fn)
    monkeypatch.setattr(sc, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")
    monkeypatch.setattr(tq, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")
    monkeypatch.setattr(tq, "load_pick_state", lambda: {})
    monkeypatch.setattr(tq, "save_pick_state", lambda _last: None)

    q = tq.TitleQueue()
    q.append_titles(
        [
            {
                "title": "What If Already Farmed?",
                "policy_ok": True,
                "approved": True,
                "trend_score": 0.99,
                "job_id": "job_existing_abc",
                "status": "queued",
            },
            {
                "title": "What If Fresh Ready Idea?",
                "policy_ok": True,
                "approved": True,
                "trend_score": 0.5,
                "status": "queued",
            },
        ],
        channel="napstorian",
    )
    inv = q.inventory_approved()
    assert len(inv["skipped_have_job_id"]) == 1
    assert inv["skipped_have_job_id"][0].job_id == "job_existing_abc"
    assert len(inv["ready"]) == 1
    assert inv["ready"][0].title == "What If Fresh Ready Idea?"
    assert q.count_pending_ready() == 1
    picked = q.pick_approved(limit=2)
    assert len(picked) == 1
    assert picked[0].title == "What If Fresh Ready Idea?"
    assert not (picked[0].job_id or "").strip()
    settings_mod.get_settings.cache_clear()


def test_resolve_profile_without_format_column(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("RETENTION_PROFILE", raising=False)
    monkeypatch.setenv("SHEET_CHANNELS", "napstorian,napping_historian")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    from src.agents.title_queue import TitleQueue, _row_from_dict
    from src.services import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    row = _row_from_dict(2, {"title": "What If X?", "policy_ok": "TRUE"}, channel="napstorian")
    assert row.format == ""
    q = TitleQueue()
    assert q.resolve_profile(row) == "retention"
    settings_mod.get_settings.cache_clear()


def test_row_to_sheet_dict_omits_format_by_default():
    from src.agents.title_queue import DEFAULT_COLUMNS, _row_from_dict, _row_to_sheet_dict

    row = _row_from_dict(
        2,
        {"title": "What If X?", "format": "epic", "approved": "TRUE", "policy_ok": "1"},
        channel="napstorian",
    )
    # Legacy in-memory format still parsed if present in CSV
    assert row.format == "epic"
    d = _row_to_sheet_dict(row, columns=DEFAULT_COLUMNS)
    assert "format" not in d
    assert d["title"] == "What If X?"


def test_historian_prompts_dir_and_overrides(monkeypatch: pytest.MonkeyPatch):
    """napping_historian → epic profile + channel prompt pack (outline/refine/expand)."""
    monkeypatch.delenv("SHEET_CHANNEL_PROFILE_NAPPING_HISTORIAN", raising=False)
    monkeypatch.delenv("SHEET_CHANNEL_PROMPTS_DIR_NAPPING_HISTORIAN", raising=False)
    monkeypatch.delenv("SCRIPT_CHANNEL", raising=False)
    monkeypatch.setenv("RETENTION_PROFILE", "epic")

    from src.agents.sheet_channels import (
        channel_prompts_dir,
        channel_script_overrides,
    )
    from src.services.retention_profile import (
        load_retention_profiles,
        resolve_prompts_dir,
    )
    from src.services.settings import ROOT, load_prompt, load_script_settings, render_prompt
    from src.services.pacing import load_dual_pacing

    load_retention_profiles.cache_clear()

    assert channel_prompts_dir("napping_historian") == "config/prompts/napping_historian"
    assert channel_prompts_dir("napstorian") is None
    assert channel_script_overrides("napping_historian").get("chapters_target") == 37

    cfg = load_script_settings()
    d = resolve_prompts_dir(cfg, channel="napping_historian")
    assert d == ROOT / "config" / "prompts" / "napping_historian"

    # Napstorian / no channel still uses epic pack when profile=epic
    d_epic = resolve_prompts_dir(cfg, channel="napstorian")
    assert d_epic == ROOT / "config" / "prompts" / "epic"

    outline = load_prompt("outline.txt", prompts_dir=d)
    refine = load_prompt("outline_refine.txt", prompts_dir=d)
    expand = load_prompt("chapter_expand.txt", prompts_dir=d)
    assert "Napping Historian" in outline or "Napping Historian" in refine
    assert "ACCURACY" in outline and "SPECULATION" in outline and "SAFETY" in outline
    assert "{{RAW_CHAPTERS}}" in refine
    assert "Welcome back you night owls" in expand
    assert "{{PREVIOUS_SUMMARY}}" in expand
    assert "{{CHAPTER_INDEX}}" in expand

    cfg2 = {**cfg, **channel_script_overrides("napping_historian")}
    pacing = load_dual_pacing(cfg2, settings_target_scenes=240)
    rendered = render_prompt(
        outline,
        {
            "TOPIC": "The Voynich Manuscript",
            "NICHE_NOTES": "(none)",
            "TARGET_DURATION_MIN": cfg2.get("target_duration_min", 90),
            "TARGET_SECONDS_PER_SCENE": pacing.phase_a_seconds,
            "TARGET_SCENES": pacing.target_scenes,
            "MIN_SCENES": pacing.min_scenes,
            "MAX_SCENES": pacing.max_scenes,
            "CHAPTERS_TARGET": cfg2.get("chapters_target", 37),
            "PHASE_A_MAX_WORDS": pacing.phase_a_max_words,
            "PHASE_A_SCENES": pacing.phase_a_target_scenes,
            "PHASE_A_SECONDS": pacing.phase_a_seconds,
            "PHASE_A_WORDS_MIN": pacing.phase_a_words_min,
            "PHASE_A_WORDS_MAX": pacing.phase_a_words_max,
            "PHASE_B_SCENES": pacing.phase_b_target_scenes,
            "PHASE_B_SECONDS": pacing.phase_b_seconds,
            "PHASE_B_WORDS": pacing.phase_b_words,
            "VOICE_WPM": round(pacing.voice_wpm, 1),
        },
    )
    assert "{{" not in rendered
    assert "37" in rendered
    # Same dual-pacing machinery as epic / napstorian profiles
    assert pacing.phase_b_seconds == 120
    assert pacing.phase_a_target_scenes == 200
    load_retention_profiles.cache_clear()


def test_resolve_profile_historian_defaults_epic(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("RETENTION_PROFILE", raising=False)
    monkeypatch.delenv("SHEET_CHANNEL_PROFILE_NAPPING_HISTORIAN", raising=False)
    monkeypatch.setenv("SHEET_CHANNELS", "napstorian,napping_historian")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    from src.agents.title_queue import TitleQueue, _row_from_dict
    from src.services import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    row = _row_from_dict(
        2, {"title": "Ancient archives?", "policy_ok": "TRUE"}, channel="napping_historian"
    )
    q = TitleQueue()
    assert q.resolve_profile(row) == "epic"
    settings_mod.get_settings.cache_clear()


def test_competitors_path_channel_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Historian must never silently read napstorian competitors.json."""
    import src.agents.competitors_agent as ca

    nap = tmp_path / "competitors.json"
    hist = tmp_path / "competitors_napping_historian.json"
    nap.write_text('{"competitors":[{"channel_id":"NAP"}]}', encoding="utf-8")
    hist.write_text('{"competitors":[{"channel_id":"HIST"}]}', encoding="utf-8")
    monkeypatch.setattr(ca, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ca, "COMPETITORS_PATH", nap)

    assert ca.competitors_path("napstorian") == nap
    assert ca.competitors_path(None) == nap
    assert ca.competitors_path("napping_historian") == hist
    assert ca.channel_for_competitors_path(hist) == "napping_historian"
    assert ca.channel_for_competitors_path(nap) == "napstorian"

    # Known channel keeps scoped path even when file is missing (no fallback)
    hist.unlink()
    assert ca.competitors_path("napping_historian") == hist
    assert ca.competitors_path("napping_historian") != nap


def test_trends_load_competitors_respects_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import src.agents.competitors_agent as ca
    from src.agents.trends_agent import _load_competitors

    nap = tmp_path / "competitors.json"
    hist = tmp_path / "competitors_napping_historian.json"
    nap.write_text(
        '{"competitors":[{"channel_id":"NAP","eligible_for_titles":true}]}',
        encoding="utf-8",
    )
    hist.write_text(
        '{"competitors":[{"channel_id":"HIST","eligible_for_titles":true}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(ca, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ca, "COMPETITORS_PATH", nap)

    assert _load_competitors("napstorian")["competitors"][0]["channel_id"] == "NAP"
    assert (
        _load_competitors("napping_historian")["competitors"][0]["channel_id"] == "HIST"
    )