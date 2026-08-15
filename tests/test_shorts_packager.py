"""40s Shorts factory: window snap, Gate S, parent harvest, farm isolation, cascade."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.agents.sheet_channels import (
    empire_shorts_tab_names,
    is_shorts_tab,
    longform_channel_from_shorts_tab,
    shorts_tab_name,
)
from src.agents.sheet_hygiene import cascade_shorts_from_longform
from src.agents.shorts_harvest import invent_shorts_title
from src.agents.title_queue import SHORTS_COLUMNS, TitleQueue
from src.content.derivatives import is_module_allowed, shorts_clip_allowed
from src.services.shorts_packager import (
    SPEED_FACTOR,
    TARGET_S,
    MIN_S,
    MAX_S,
    atempo_chain,
    expected_output_s,
    select_window,
    source_window_params,
)


_CONTENT_STOP = frozenset(
    {"the", "a", "an", "and", "of", "in", "on", "to", "if", "what", "why", "how"}
)


def _content_words(title: str) -> list[str]:
    return [
        w
        for w in re.findall(r"[a-z0-9']+", (title or "").lower())
        if w not in _CONTENT_STOP
    ]


def test_select_window_snaps_to_complete_scene():
    start, dur = select_window([10, 10, 10, 12])
    assert start == 0.0
    assert dur == pytest.approx(42.0)


def test_select_window_exact_40():
    start, dur = select_window([20, 20, 20])
    assert start == 0.0
    assert dur == pytest.approx(40.0)


def test_select_window_empty_uses_target():
    start, dur = select_window([])
    assert start == 0.0
    assert 38.0 <= dur <= 45.0


def test_select_window_never_exceeds_hard_max():
    _, dur = select_window([90])
    assert dur <= 45.0


def test_gate_s_rejects_landscape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.services import gate_shorts as gs

    fake = tmp_path / "clip.mp4"
    fake.write_bytes(b"x" * 20_000)

    monkeypatch.setattr(
        gs,
        "_probe",
        lambda _p: {
            "width": 1280,
            "height": 720,
            "has_video": True,
            "has_audio": True,
            "duration_s": 40.0,
        },
    )
    gate = gs.run_gate_s(fake)
    assert gate.ok is False
    assert any("1280x720" in e for e in gate.errors)


def test_gate_s_ok_1080x1920(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.services import gate_shorts as gs

    fake = tmp_path / "clip.mp4"
    fake.write_bytes(b"x" * 20_000)
    monkeypatch.setattr(
        gs,
        "_probe",
        lambda _p: {
            "width": 1080,
            "height": 1920,
            "has_video": True,
            "has_audio": True,
            "duration_s": 40.2,
        },
    )
    gate = gs.run_gate_s(fake)
    assert gate.ok is True
    assert gate.errors == []


def test_gate_s_duration_band(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.services import gate_shorts as gs

    fake = tmp_path / "clip.mp4"
    fake.write_bytes(b"x" * 20_000)
    monkeypatch.setattr(
        gs,
        "_probe",
        lambda _p: {
            "width": 1080,
            "height": 1920,
            "has_video": True,
            "has_audio": True,
            "duration_s": 70.0,
        },
    )
    gate = gs.run_gate_s(fake)
    assert gate.ok is False
    assert any("60" in e or "exceeds" in e.lower() for e in gate.errors)


def test_shorts_tabs_are_not_farm_channels(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHEET_CHANNELS", "napstorian,napping_historian,napstorian_shorts")
    from src.agents import sheet_channels as sc

    chans = sc.configured_sheet_channels()
    assert "napstorian" in chans
    assert "napping_historian" in chans
    assert not any(is_shorts_tab(c) for c in chans)


def test_farm_pick_ignores_shorts_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("SHEET_CHANNELS", "napstorian")
    from src.agents import title_queue as tq

    monkeypatch.setattr(tq, "local_queue_path", lambda ch: tmp_path / f"q_{ch}.csv")
    q = TitleQueue(channel="napstorian")
    q.append_titles(
        [
            {
                "title": "What If Rome Never Fell?",
                "approved": True,
                "policy_ok": True,
                "status": "queued",
            }
        ],
        channel="napstorian",
    )
    sq = TitleQueue(channel="napstorian_shorts")
    sq.append_titles(
        [
            {
                "title": "Punchy short hook",
                "kind": "shorts",
                "approved": True,
                "policy_ok": True,
                "status": "queued",
                "parent_job_id": "job1",
            }
        ],
        channel="napstorian_shorts",
    )
    picked = TitleQueue(channel="napstorian").pick_approved(limit=5, mutate_state=False)
    titles = [r.title for r in picked]
    assert "What If Rome Never Fell?" in titles
    assert all((r.kind or "") != "shorts" for r in picked)
    assert all(not is_shorts_tab(r.channel) for r in picked)


def test_invent_shorts_title_is_short_click_hook():
    t = invent_shorts_title(
        parent_title="The Eternal Senate: What If Rome Never Fell But Transformed?",
        hook="A sealed letter could have ended Tudor England.",
        channel="napstorian",
    )
    assert "rome" in t.lower()
    assert t.lower() != "what if rome?"
    assert "never" in t.lower() or "fell" in t.lower() or "letter" in t.lower()
    assert len(_content_words(t)) > 2
    assert len(t) <= 40
    assert len(t.split()) <= 8
    assert "what if what if" not in t.lower()


def test_invent_historian_title_is_short():
    t = invent_shorts_title(
        parent_title="The Unquiet Queen: Why Catherine of Aragon Still Haunts the Tudor Dynasty",
        hook="A ghostly legacy of a failed marriage",
        channel="napping_historian",
    )
    assert len(t) <= 40
    assert len(t.split()) <= 8
    assert "catherine" in t.lower()
    assert len(_content_words(t)) > 2
    assert "what if what if" not in t.lower()


def test_invent_shorts_title_variants_differ():
    a = invent_shorts_title(
        parent_title="What If Rome Never Fell?",
        hook="A secret decree",
        channel="napstorian",
        variant=0,
    )
    b = invent_shorts_title(
        parent_title="What If Rome Never Fell?",
        hook="A secret decree",
        channel="napstorian",
        variant=1,
    )
    assert a != b
    for t in (a, b):
        assert "rome" in t.lower()
        assert len(_content_words(t)) > 2
        assert len(t) <= 40
        assert "what if what if" not in t.lower()
        assert t.lower() != "what if rome?"


def test_invent_historian_variants_differ():
    a = invent_shorts_title(
        parent_title="The Secret Letters of Anne Boleyn: Hidden Schemes in the Tudor Court",
        hook="A sealed letter",
        channel="napping_historian",
        variant=0,
    )
    b = invent_shorts_title(
        parent_title="The Secret Letters of Anne Boleyn: Hidden Schemes in the Tudor Court",
        hook="A sealed letter",
        channel="napping_historian",
        variant=1,
    )
    assert a != b
    for t in (a, b):
        assert "boleyn" in t.lower()
        assert len(_content_words(t)) > 2
        assert len(t) <= 40


def test_select_window_start_at_skips_prefix():
    start, dur = select_window([10] * 10, start_at_s=22)
    assert start == pytest.approx(20.0)
    assert 38.0 <= dur <= 45.0


def test_trailer_windows_long_historian_media():
    from src.services.shorts_packager import trailer_window_starts

    starts = trailer_window_starts(
        [100] * 36,
        channel="napping_historian",
        media_duration_s=945.0,
    )
    assert starts[0] == 0.0
    assert len(starts) >= 3
    assert all(s < 945.0 - 38.0 for s in starts)


def test_speed_factor_source_vs_output_target():
    assert SPEED_FACTOR == pytest.approx(1.20)
    params = source_window_params()
    assert params["target_s"] == pytest.approx(TARGET_S * SPEED_FACTOR)
    assert params["target_s"] == pytest.approx(48.0)
    out = expected_output_s(params["target_s"])
    assert out == pytest.approx(TARGET_S)
    assert MIN_S <= out <= MAX_S
    assert atempo_chain(SPEED_FACTOR) == "atempo=1.2"
    assert "atempo=2.0" in atempo_chain(2.4)


def test_select_window_source_band_after_speed():
    params = source_window_params()
    start, dur = select_window(
        [12] * 8,
        target_s=params["target_s"],
        min_s=params["min_s"],
        max_s=params["max_s"],
        hard_max_s=params["hard_max_s"],
    )
    assert start == 0.0
    assert dur == pytest.approx(48.0)
    assert MIN_S <= expected_output_s(dur) <= MAX_S


def test_harvest_binds_parent_and_copies_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    from src.agents import shorts_harvest as sh
    from src.agents import title_queue as tq
    from src.agents.store import JobRecord, OpsStore

    monkeypatch.setattr(tq, "local_queue_path", lambda ch: tmp_path / f"q_{ch}.csv")
    monkeypatch.setattr(sh, "ROOT", tmp_path)

    job_dir = tmp_path / "job"
    (job_dir / "script").mkdir(parents=True)
    (job_dir / "video").mkdir()
    (job_dir / "video" / "final.mp4").write_bytes(b"0" * 2000)
    (job_dir / "script" / "script.json").write_text(
        json.dumps(
            {
                "title": "What If the Armada Had Won?",
                "hook": "Spain takes the Channel and the Tudor court panics.",
            }
        ),
        encoding="utf-8",
    )

    job = JobRecord(
        id="job_armada",
        title="What If the Armada Had Won?",
        status="private",
        video_id="vid_parent",
        job_dir=str(job_dir),
        meta={"channel": "napstorian"},
    )
    store = MagicMock(spec=OpsStore)
    store.list_jobs.return_value = [job]

    lf = TitleQueue(channel="napstorian")
    lf.append_titles(
        [
            {
                "title": "What If the Armada Had Won?",
                "job_id": "job_armada",
                "video_id": "vid_parent",
                "approved": True,
                "public_approved": False,
                "policy_ok": True,
                "status": "private",
            }
        ],
        channel="napstorian",
    )
    out = sh.harvest_shorts_titles(
        channel="napstorian", store=store, queue=lf, dry_run=False
    )
    assert out["added"] >= 1
    sq = TitleQueue(channel="napstorian_shorts")
    rows = sq.list_rows(channel="napstorian_shorts")
    assert rows
    assert rows[0].parent_job_id == "job_armada"
    assert rows[0].parent_video_id == "vid_parent"
    assert rows[0].approved is True
    assert rows[0].public_approved is True
    assert rows[0].kind == "shorts"
    assert len(rows[0].title) <= 40
    assert len(rows[0].title.split()) <= 8
    assert "armada" in rows[0].title.lower()
    assert len(_content_words(rows[0].title)) > 2
    assert rows[0].title.lower() != "what if the armada?"

    rows[0].public_approved = False
    sq.replace_channel_rows("napstorian_shorts", rows)
    sh.harvest_shorts_titles(channel="napstorian", store=store, queue=lf, dry_run=False)
    refreshed = TitleQueue(channel="napstorian_shorts").list_rows(
        channel="napstorian_shorts"
    )
    assert refreshed[0].public_approved is True


def test_cascade_mirrors_longform_approve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    from src.agents import title_queue as tq

    monkeypatch.setattr(tq, "local_queue_path", lambda ch: tmp_path / f"q_{ch}.csv")
    lf = TitleQueue(channel="napstorian")
    lf.append_titles(
        [
            {
                "title": "Parent Doc",
                "job_id": "j1",
                "video_id": "v1",
                "approved": True,
                "public_approved": False,
                "policy_ok": True,
                "status": "queued",
            }
        ],
        channel="napstorian",
    )
    sq = TitleQueue(channel="napstorian_shorts")
    sq.append_titles(
        [
            {
                "title": "Short hook",
                "kind": "shorts",
                "parent_job_id": "j1",
                "job_id": "j1",
                "approved": False,
                "public_approved": False,
                "policy_ok": True,
                "status": "hold",
            }
        ],
        channel="napstorian_shorts",
    )
    result = cascade_shorts_from_longform(queue=lf, channels=["napstorian"], dry_run=False)
    assert result["updated"] >= 1
    rows = TitleQueue(channel="napstorian_shorts").list_rows(channel="napstorian_shorts")
    assert rows[0].approved is True
    assert rows[0].public_approved is True
    assert rows[0].status != "hold"


def test_cascade_does_not_demote_independent_shorts_approve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    from src.agents import title_queue as tq

    monkeypatch.setattr(tq, "local_queue_path", lambda ch: tmp_path / f"q_{ch}.csv")
    lf = TitleQueue(channel="napstorian")
    lf.append_titles(
        [
            {
                "title": "Unmade Doc",
                "job_id": "j_hold",
                "approved": False,
                "public_approved": False,
                "policy_ok": True,
                "status": "hold",
            }
        ],
        channel="napstorian",
    )
    sq = TitleQueue(channel="napstorian_shorts")
    sq.append_titles(
        [
            {
                "title": "What if Rome?",
                "kind": "shorts",
                "parent_job_id": "j_hold",
                "job_id": "j_hold",
                "approved": True,
                "public_approved": True,
                "policy_ok": True,
                "status": "queued",
            }
        ],
        channel="napstorian_shorts",
    )
    cascade_shorts_from_longform(queue=lf, channels=["napstorian"], dry_run=False)
    rows = TitleQueue(channel="napstorian_shorts").list_rows(channel="napstorian_shorts")
    assert rows[0].approved is True
    assert rows[0].public_approved is True
    assert rows[0].status != "hold"


def test_shorts_clip_allowlist_does_not_unfreeze_podcast():
    assert is_module_allowed("podcast_audio") is False
    assert is_module_allowed("tiktok_shorts_text") is False
    assert shorts_clip_allowed(allow_video_reuse=True) is True


def test_empire_shorts_tab_names_cover_factory():
    names = empire_shorts_tab_names()
    assert "napstorian_shorts" in names
    assert "napping_historian_shorts" in names
    assert shorts_tab_name("art_mysteries") == "art_mysteries_shorts"
    assert longform_channel_from_shorts_tab("art_mysteries_shorts") == "art_mysteries"
    assert "kind" in SHORTS_COLUMNS
    assert "parent_video_id" in SHORTS_COLUMNS
    assert "window_index" in SHORTS_COLUMNS


def test_shorts_description_no_longform_url():
    from src.agents.shorts_publish import shorts_description

    d = shorts_description(title="What if Rome?", hook="A quick beat")
    assert "What if Rome?" in d
    assert "A quick beat" in d
    assert "#Shorts" in d
    assert "youtu.be" not in d
    assert "click" not in d.lower()
    assert "know more" not in d.lower()


def test_shorts_description_title_only():
    from src.agents.shorts_publish import shorts_description

    d = shorts_description(title="Ghost short")
    assert d.splitlines()[0] == "Ghost short"
    assert "#Shorts" in d
    assert "youtu.be" not in d


def test_publish_short_passes_related_video_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.agents.shorts_publish import publish_short_for_row
    from src.agents.store import JobRecord, OpsStore
    from src.agents.title_queue import TitleRow
    from src.services import gate_shorts as gs

    job_dir = tmp_path / "job"
    script_dir = job_dir / "script"
    script_dir.mkdir(parents=True)
    script_dir.joinpath("script.json").write_text(
        json.dumps({"title": "Short title", "hook": "", "outline": {"chapters": []}}),
        encoding="utf-8",
    )
    clip = job_dir / "shorts" / "w0_short.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"x" * 20_000)

    monkeypatch.setattr(
        gs,
        "_probe",
        lambda _p: {
            "width": 1080,
            "height": 1920,
            "has_video": True,
            "has_audio": True,
            "duration_s": 40.0,
        },
    )
    monkeypatch.setattr(
        "src.content.derivatives.shorts_clip_allowed",
        lambda **_: True,
    )

    store = MagicMock(spec=OpsStore)
    store.list_jobs.return_value = [
        JobRecord(
            id="j1",
            title="What If Rome Never Fell?",
            status="public",
            video_id="romeParent1",
            job_dir=str(job_dir),
            meta={"channel": "napstorian"},
        ),
    ]
    store.get_job.return_value = store.list_jobs.return_value[0]

    captured: dict[str, object] = {}

    class FakePub:
        def publish_private(self, **kwargs):
            captured.update(kwargs)
            from src.domain.models import GateAResult, PublishResult

            return PublishResult(
                ok=True,
                dry_run=bool(kwargs.get("dry_run")),
                title=str(kwargs.get("title") or ""),
                description=str(kwargs.get("description") or ""),
                final_path=str(kwargs.get("final_path") or ""),
                gate_a=GateAResult(ok=True),
            )

    monkeypatch.setattr(
        "src.services.publish_youtube.PublishModule",
        lambda channel=None: FakePub(),
    )
    monkeypatch.setattr(
        "src.agents.shorts_publish.resolve_final_mp4",
        lambda *a, **k: job_dir / "video" / "final.mp4",
    )
    final_mp4 = job_dir / "video" / "final.mp4"
    final_mp4.parent.mkdir(parents=True)
    final_mp4.write_bytes(b"x" * 20_000)
    monkeypatch.setattr(
        "src.agents.shorts_publish.shorts_output_path",
        lambda *a, **k: clip,
    )

    row = TitleRow(
        row_index=2,
        title="What if Rome?",
        parent_title="What If Rome Never Fell?",
        parent_video_id="romeParent1",
        parent_job_id="j1",
        approved=True,
        policy_ok=True,
    )
    out = publish_short_for_row(row, channel="napstorian", store=store, dry_run=True)
    assert out.get("ok") is True
    assert captured.get("related_video_id") == "romeParent1"
    assert captured.get("shorts_mode") is True
    assert "youtu.be" not in str(captured.get("description") or "")


def test_set_shorts_related_video_builds_payload():
    from src.services.publish_youtube import SHORTS_RELATED_VIDEO_FIELD, PublishModule

    payload = PublishModule._shorts_related_video_payload(related_video_id="abc123xyz00")
    assert payload == {SHORTS_RELATED_VIDEO_FIELD: "abc123xyz00"}
    assert PublishModule._shorts_related_video_payload(related_video_id="") == {}


def test_resolve_cta_matches_parent_then_latest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.agents.shorts_publish import resolve_shorts_cta
    from src.agents.store import JobRecord, OpsStore
    from src.agents.title_queue import TitleRow

    monkeypatch.setattr("src.agents.shorts_publish.ROOT", tmp_path)
    store = MagicMock(spec=OpsStore)
    store.list_jobs.return_value = [
        JobRecord(
            id="j1",
            title="What If Rome Never Fell?",
            status="public",
            video_id="romeParent1",
            meta={"channel": "napstorian"},
            updated_at="2026-08-01T00:00:00+00:00",
        ),
        JobRecord(
            id="j2",
            title="What If the Spanish Armada Had Conquered England?",
            status="public",
            video_id="armadaLate1",
            meta={"channel": "napstorian"},
            updated_at="2026-08-10T00:00:00+00:00",
        ),
    ]
    row = TitleRow(
        row_index=2,
        title="What if Rome?",
        parent_title="What If Rome Never Fell?",
        parent_video_id="romeParent1",
    )
    cta = resolve_shorts_cta(row, channel="napstorian", store=store)
    assert cta["video_id"] == "romeParent1"
    assert cta["kind"] == "matched"

    row2 = TitleRow(row_index=3, title="Random ghost beat", parent_title="", parent_video_id="")
    cta2 = resolve_shorts_cta(row2, channel="napstorian", store=store)
    assert cta2["video_id"] == "armadaLate1"
    assert cta2["kind"] == "latest"
