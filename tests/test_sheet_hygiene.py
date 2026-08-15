"""Sheet hygiene: historian What-If + policy-blocked + scheduled consume-drop."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.sheet_hygiene import (
    drop_scheduled_consumed_rows,
    hygiene_channel_sheet,
    should_drop_scheduled_consumed,
    should_remove_row,
)
from src.agents.title_queue import TitleRow


def _row(**kwargs) -> TitleRow:
    defaults = dict(
        row_index=2,
        title="The Secret History of the Tudor Court",
        channel="napping_historian",
        policy_ok=True,
        approved=False,
        status="queued",
        job_id="",
        video_id="",
    )
    defaults.update(kwargs)
    return TitleRow(**defaults)


def test_historian_what_if_removed():
    drop, reason = should_remove_row(
        _row(title="What If Anne Boleyn Survived?"),
        channel="napping_historian",
    )
    assert drop and reason == "historian_what_if"


def test_napstorian_what_if_kept():
    drop, _ = should_remove_row(
        _row(
            title="What If Anne Boleyn Survived?",
            channel="napstorian",
        ),
        channel="napstorian",
    )
    assert not drop


def test_policy_blocked_queued_removed():
    drop, reason = should_remove_row(
        _row(title="Clickbait Junk", policy_ok=False),
        channel="napping_historian",
    )
    assert drop and reason == "policy_blocked"


def test_protected_job_not_removed():
    drop, _ = should_remove_row(
        _row(
            title="What If Rome Never Fell?",
            policy_ok=False,
            job_id="job_abc",
            status="private",
        ),
        channel="napping_historian",
    )
    assert not drop


def test_scheduled_sheet_status_is_droppable():
    drop, reason = should_drop_scheduled_consumed(
        _row(
            title="What If Scheduled?",
            channel="napstorian",
            status="scheduled",
            job_id="job_sched",
            video_id="vid_1",
        )
    )
    assert drop and reason == "sheet_status_scheduled"


def test_ops_job_scheduled_drops_even_if_sheet_private():
    drop, reason = should_drop_scheduled_consumed(
        _row(
            title="Private but job scheduled",
            channel="napping_historian",
            status="private",
            job_id="job_sched",
            video_id="vid_2",
        ),
        scheduled_job_ids={"job_sched"},
    )
    assert drop and reason == "ops_job_scheduled"


def test_private_job_not_scheduled_kept():
    drop, _ = should_drop_scheduled_consumed(
        _row(
            title="Still private",
            status="private",
            job_id="job_priv",
            video_id="vid_3",
        ),
        scheduled_job_ids={"other"},
    )
    assert not drop


def test_drop_scheduled_rewrites_and_credits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("SHEET_CHANNELS", "napstorian,napping_historian")
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

    credits_root = tmp_path / "ops"
    credits_root.mkdir()
    archive_root = tmp_path / "archive"

    q = tq.TitleQueue()
    q.append_titles(
        [
            {
                "title": "What If Armed Nap?",
                "policy_ok": True,
                "approved": True,
                "status": "scheduled",
                "job_id": "job_nap",
                "video_id": "vid_nap",
            },
            {
                "title": "What If Still Queued?",
                "policy_ok": True,
                "approved": False,
                "status": "queued",
            },
        ],
        channel="napstorian",
    )
    q.append_titles(
        [
            {
                "title": "The Hidden Letters Already Armed",
                "policy_ok": True,
                "approved": True,
                "status": "private",
                "job_id": "job_hist",
                "video_id": "vid_hist",
            },
            {
                "title": "The Quiet Mystery Still Stock",
                "policy_ok": True,
                "approved": False,
                "status": "queued",
            },
        ],
        channel="napping_historian",
    )

    out = drop_scheduled_consumed_rows(
        queue=q,
        dry_run=False,
        channels=["napstorian", "napping_historian"],
        archive_root=archive_root,
        credit_refills=True,
        credits_root=credits_root,
        scheduled_job_ids={"job_hist"},
    )
    assert out["removed"] == 2
    assert out["by_channel"]["napstorian"] == 1
    assert out["by_channel"]["napping_historian"] == 1
    assert idea_mod.load_refill_credits(root=credits_root) == {
        "napstorian": 1,
        "napping_historian": 1,
    }
    assert q.count_idea_stock(channel="napstorian") == 1
    assert q.count_idea_stock(channel="napping_historian") == 1
    assert all(r.status != "scheduled" for r in q.list_rows(channel="napstorian"))
    assert all(r.job_id != "job_hist" for r in q.list_rows(channel="napping_historian"))
    assert archive_root.exists()
    settings_mod.get_settings.cache_clear()


def test_hygiene_rewrites_csv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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

    q = tq.TitleQueue()
    q.append_titles(
        [
            {
                "title": "What If Napoleon Won?",
                "policy_ok": True,
                "approved": False,
                "status": "queued",
            },
            {
                "title": "The Hidden Letters of Anne Boleyn",
                "policy_ok": True,
                "approved": False,
                "status": "queued",
            },
            {
                "title": "Blocked Clickbait Title",
                "policy_ok": False,
                "approved": False,
                "status": "queued",
            },
        ],
        channel="napping_historian",
    )
    assert q.count_idea_stock(channel="napping_historian") == 2  # What-If + doc

    out = hygiene_channel_sheet(
        "napping_historian",
        queue=q,
        dry_run=False,
        archive_root=tmp_path / "archive",
    )
    assert out["what_if_removed"] == 1
    assert out["policy_blocked_removed"] == 1
    assert out["after"] == 1
    assert q.count_idea_stock(channel="napping_historian") == 1
    left = q.list_rows(channel="napping_historian")
    assert left[0].title.startswith("The Hidden")
    assert (tmp_path / "archive").exists()


def test_count_idea_stock_requires_empty_job_id(
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

    q = tq.TitleQueue()
    q.append_titles(
        [
            {
                "title": "What If A?",
                "policy_ok": True,
                "approved": False,
                "status": "queued",
            },
            {
                "title": "What If B?",
                "policy_ok": True,
                "approved": True,
                "status": "queued",
                "job_id": "job_done",
            },
        ],
        channel="napstorian",
    )
    assert q.count_idea_stock(channel="napstorian") == 1


def test_hygiene_runs_before_refill_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """*/10 refill path strips historian What-If before counting stock."""
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
                "title": "What If Legacy Historian?",
                "policy_ok": True,
                "approved": False,
                "status": "queued",
            }
        ],
        channel="napping_historian",
    )
    q.append_titles(
        [
            {
                "title": "What If Nap A?",
                "policy_ok": True,
                "approved": False,
                "status": "queued",
            },
            {
                "title": "What If Nap B?",
                "policy_ok": True,
                "approved": False,
                "status": "queued",
            },
        ],
        channel="napstorian",
    )

    class FakeTrends:
        def __init__(self, *a, **k):
            pass

        def harvest_titles(self, *, dry_run=False, channel=None, limit=None):
            rows = [
                {
                    "title": f"The Dark History of Topic {i}",
                    "policy_ok": True,
                    "approved": False,
                    "status": "queued",
                }
                for i in range(limit or 1)
            ]
            q.append_titles(rows, channel=channel)
            return rows

    monkeypatch.setattr(idea_mod, "TrendsAgent", FakeTrends)
    monkeypatch.setattr(idea_mod, "OpsStore", lambda: object())
    monkeypatch.setattr(
        idea_mod, "OpsLedger", lambda *_a, **_k: type("L", (), {})()
    )

    out = idea_mod.maybe_refill_idea_stock(queue=q, dry_run=False)
    assert out.get("hygiene", {}).get("what_if_removed") == 1
    assert q.count_idea_stock(channel="napping_historian") == 2
    assert all(
        not r.title.lower().startswith("what if")
        for r in q.list_rows(channel="napping_historian")
    )
    settings_mod.get_settings.cache_clear()


def test_refill_drops_scheduled_then_harvests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """*/10: scheduled rows leave the sheet; consume credits invent replacements."""
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("SHEET_CHANNELS", "napstorian,napping_historian")
    monkeypatch.setenv("PUBLISH_SCHEDULE", "1")
    monkeypatch.setenv("IDEA_STOCK_TARGET", "2")

    import src.agents.idea_stock as idea_mod
    import src.agents.sheet_channels as sc
    import src.agents.sheet_hygiene as hyg
    import src.agents.title_queue as tq
    from src.services import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    path_fn = lambda ch: tmp_path / f"title_queue_{ch}.csv"
    monkeypatch.setattr(sc, "local_queue_path", path_fn)
    monkeypatch.setattr(tq, "local_queue_path", path_fn)
    monkeypatch.setattr(sc, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")
    monkeypatch.setattr(tq, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")

    credits_root = tmp_path / "ops"
    credits_root.mkdir()
    monkeypatch.setattr(hyg, "OPS_DIR", credits_root)
    monkeypatch.setattr(idea_mod, "OPS_DIR", credits_root)

    q = tq.TitleQueue()
    for ch, title in (
        ("napstorian", "What If Already Scheduled?"),
        ("napping_historian", "The Already Scheduled Mystery"),
    ):
        q.append_titles(
            [
                {
                    "title": title,
                    "policy_ok": True,
                    "approved": True,
                    "status": "scheduled",
                    "job_id": f"job_{ch}",
                    "video_id": f"vid_{ch}",
                },
                {
                    "title": f"Stock keep {ch} 1",
                    "policy_ok": True,
                    "approved": False,
                    "status": "queued",
                },
                {
                    "title": f"Stock keep {ch} 2",
                    "policy_ok": True,
                    "approved": False,
                    "status": "queued",
                },
            ],
            channel=ch,
        )

    calls: list[tuple[str | None, int | None]] = []

    class FakeTrends:
        def __init__(self, *a, **k):
            pass

        def harvest_titles(self, *, dry_run=False, channel=None, limit=None):
            calls.append((channel, limit))
            rows = [
                {
                    "title": f"Replace {channel} {i}",
                    "policy_ok": True,
                    "approved": False,
                    "status": "queued",
                }
                for i in range(int(limit or 1))
            ]
            q.append_titles(rows, channel=channel)
            return rows

    monkeypatch.setattr(idea_mod, "TrendsAgent", FakeTrends)
    monkeypatch.setattr(idea_mod, "OpsStore", lambda: object())
    monkeypatch.setattr(
        idea_mod, "OpsLedger", lambda *_a, **_k: type("L", (), {})()
    )
    # No ops scheduled ids — sheet status alone is enough.
    monkeypatch.setattr(hyg, "scheduled_job_ids_from_store", lambda _store=None: set())

    out = idea_mod.maybe_refill_idea_stock(
        queue=q, dry_run=False, credits_root=credits_root
    )
    assert out.get("hygiene", {}).get("scheduled_removed") == 2
    assert ("napstorian", 1) in calls
    assert ("napping_historian", 1) in calls
    assert all(r.status != "scheduled" for r in q.list_rows())
    assert idea_mod.load_refill_credits(root=credits_root) == {}
    settings_mod.get_settings.cache_clear()


def test_obsolete_gate_r_length_note_detected():
    from src.agents.sheet_hygiene import is_obsolete_auto_hold_note

    assert is_obsolete_auto_hold_note(
        "Gate R FAILED — runtime 1320s below min_duration band"
    )
    assert is_obsolete_auto_hold_note(
        "HOLD: Gate R length/duration check failed for publish"
    )
    assert not is_obsolete_auto_hold_note(
        "original — inspired by competitor format/topic; smm_winner_bias=era:Tudor"
    )
    assert not is_obsolete_auto_hold_note(
        "HOLD:process_crash stage=edit resumable=True: stuck in edit"
    )


def test_script_hold_exhausted_release_no_script():
    from src.agents.sheet_hygiene import should_release_script_hold_exhausted

    row = _row(
        approved=True,
        status="hold",
        job_id="job_old",
        notes=(
            "HOLD exhausted — max repairs (2) reached for class=script "
            "— HOLD exhausted; move to next job"
        ),
    )
    ok, reason = should_release_script_hold_exhausted(row, job=None)
    assert ok and reason == "script_hold_exhausted_no_script"


def test_script_hold_exhausted_keeps_rows_with_script(tmp_path: Path):
    from types import SimpleNamespace

    from src.agents.sheet_hygiene import should_release_script_hold_exhausted

    job_dir = tmp_path / "job"
    (job_dir / "script").mkdir(parents=True)
    (job_dir / "script" / "script.json").write_text("{}", encoding="utf-8")
    job = SimpleNamespace(status="hold", job_dir=str(job_dir), meta={})
    row = _row(
        approved=True,
        status="hold",
        job_id="job_has_script",
        notes="HOLD exhausted — max repairs (2) reached for class=script",
    )
    ok, reason = should_release_script_hold_exhausted(row, job=job)
    assert not ok and reason == "has_script_json"


def test_hygiene_hold_notes_releases_and_clears(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    import src.agents.sheet_channels as sc
    import src.agents.title_queue as tq
    from src.agents import sheet_hygiene as hyg
    from src.services import settings as settings_mod

    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("SHEET_CHANNELS", "napstorian,napping_historian")
    settings_mod.get_settings.cache_clear()
    path_fn = lambda ch: tmp_path / f"title_queue_{ch}.csv"
    monkeypatch.setattr(sc, "local_queue_path", path_fn)
    monkeypatch.setattr(tq, "local_queue_path", path_fn)
    monkeypatch.setattr(sc, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")
    monkeypatch.setattr(tq, "legacy_local_queue_path", lambda: tmp_path / "title_queue.csv")

    class FakeStore:
        def __init__(self):
            self.jobs = {}

        def get_job(self, jid):
            return self.jobs.get(jid)

        def update_job(self, jid, **kwargs):
            j = self.jobs[jid]
            for k, v in kwargs.items():
                setattr(j, k, v)
            return j

    store = FakeStore()
    store.jobs["job_script_dead"] = SimpleNamespace(
        id="job_script_dead",
        status="hold",
        job_dir=str(tmp_path / "missing_job"),
        meta={"resumable": False, "repair_blocked": True},
        error="script HOLD",
    )

    q = tq.TitleQueue()
    rows = [
        _row(
            row_index=2,
            title="Dark Tudor Court Secrets",
            approved=True,
            status="hold",
            job_id="job_script_dead",
            notes=(
                "HOLD exhausted — max repairs (2) reached for class=script "
                "— HOLD exhausted; move to next job"
            ),
        ),
        _row(
            row_index=3,
            title="Gate R Length Legacy",
            approved=True,
            status="hold",
            job_id="",
            notes="Gate R FAILED — duration 400s below min_duration retention band",
        ),
        _row(
            row_index=4,
            title="Keep Inspiration",
            approved=False,
            status="queued",
            notes="original — inspired by competitor format/topic; smm_winner_bias=era:Tudor",
        ),
    ]
    q.replace_channel_rows("napping_historian", rows)

    out = hyg.hygiene_hold_notes_channel(
        "napping_historian",
        queue=q,
        store=store,
        dry_run=False,
        archive_root=tmp_path / "archive",
    )
    assert out["released"] == 1
    assert out["cleared_notes"] == 1
    after = q.list_rows(channel="napping_historian")
    by_title = {r.title: r for r in after}
    assert by_title["Dark Tudor Court Secrets"].status == "queued"
    assert by_title["Dark Tudor Court Secrets"].job_id == ""
    assert by_title["Dark Tudor Court Secrets"].approved is True
    assert "new-format farm" in (by_title["Dark Tudor Court Secrets"].notes or "")
    assert by_title["Gate R Length Legacy"].status == "queued"
    assert (by_title["Gate R Length Legacy"].notes or "") == ""
    assert "smm_winner_bias" in (by_title["Keep Inspiration"].notes or "")
    assert store.jobs["job_script_dead"].meta.get("superseded_by_refarm") is True
    settings_mod.get_settings.cache_clear()
