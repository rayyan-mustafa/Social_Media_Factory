"""SMM engagement pin: once per video, no Watching-title prefix."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.agents import smm_agent as sa
from src.agents.smm_agent import SocialMediaManager


def test_pin_engagement_comment_no_watching_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(sa, "_smm_pinned_videos_path", lambda: tmp_path / "pins.json")
    smm = SocialMediaManager.__new__(SocialMediaManager)
    smm.smm_cfg = {
        "pin_comment_text": "Drop your idea below.",
        "pin_comment_text_by_channel": {},
    }
    smm.cfg = {"pin_comment_on_upload": True}
    smm.ledger = MagicMock()
    smm._normalize_channel = lambda c: c or "napstorian"  # type: ignore[method-assign]
    smm._build_youtube = MagicMock(side_effect=AssertionError("should not post"))  # type: ignore[method-assign]

    # First call claims + would post — mock YouTube
    yt = MagicMock()
    yt.commentThreads.return_value.insert.return_value.execute.return_value = {
        "id": "thread1",
        "snippet": {"topLevelComment": {"id": "Ug_abc"}},
    }
    yt.comments.return_value.setModerationStatus.return_value.execute.return_value = {}
    smm._build_youtube = MagicMock(return_value=yt)  # type: ignore[method-assign]

    first = smm.pin_engagement_comment(
        "vid123",
        title="Why Catherine of Aragon Still Haunts the Tudor Dynasty",
        channel="napping_historian",
    )
    assert first["ok"] is True
    assert first.get("skipped") is not True
    assert "Watching" not in (first.get("text") or "")
    assert "Catherine" not in (first.get("text") or "")
    assert first["text"] == "Drop your idea below."
    assert first["comment_id"] == "Ug_abc"

    second = smm.pin_engagement_comment(
        "vid123",
        title="Why Catherine of Aragon Still Haunts the Tudor Dynasty",
        channel="napping_historian",
    )
    assert second.get("skipped") is True
    assert second.get("reason") == "already_claimed_for_video"
    assert yt.commentThreads.return_value.insert.call_count == 1


def test_pin_claim_blocks_retry_after_failed_post(tmp_path, monkeypatch):
    monkeypatch.setattr(sa, "_smm_pinned_videos_path", lambda: tmp_path / "pins.json")
    smm = SocialMediaManager.__new__(SocialMediaManager)
    smm.smm_cfg = {"pin_comment_text": "Hello fans.", "pin_comment_text_by_channel": {}}
    smm.cfg = {}
    smm.ledger = MagicMock()
    smm._normalize_channel = lambda c: "napstorian"  # type: ignore[method-assign]
    smm._build_youtube = MagicMock(side_effect=RuntimeError("oauth boom"))  # type: ignore[method-assign]

    failed = smm.pin_engagement_comment("vid_fail", title="Some Title")
    assert failed["ok"] is False

    smm._build_youtube = MagicMock(side_effect=AssertionError("no second post"))  # type: ignore[method-assign]
    again = smm.pin_engagement_comment("vid_fail", title="Some Title")
    assert again.get("skipped") is True
    assert again.get("reason") == "already_claimed_for_video"
