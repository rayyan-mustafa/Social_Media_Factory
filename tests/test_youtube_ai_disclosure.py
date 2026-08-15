"""YouTube description AI disclosure footer — shared across channels."""

from __future__ import annotations

from src.services.settings import Settings
from src.services.youtube_meta import (
    DEFAULT_YOUTUBE_AI_DISCLOSURE,
    append_youtube_ai_disclosure,
    strip_youtube_ai_disclosure_footer,
)


def test_default_disclosure_states_human_script_not_ai_script():
    text = DEFAULT_YOUTUBE_AI_DISCLOSURE.lower()
    assert "ai-assisted" in text
    assert "visuals" in text
    assert "narration" in text
    assert "script is original and human-written" in text
    assert "ai-written" not in text
    assert "ai-generated script" not in text


def test_settings_default_matches_shared_constant():
    s = Settings(youtube_ai_disclosure_text=DEFAULT_YOUTUBE_AI_DISCLOSURE)
    assert s.youtube_ai_disclosure_text == DEFAULT_YOUTUBE_AI_DISCLOSURE
    assert "human-written" in s.youtube_ai_disclosure_text


def test_append_disclosure_once_with_separator():
    body = "Hook line.\n\nBody copy.\n\nSubscribe and share your theory."
    out = append_youtube_ai_disclosure(body, DEFAULT_YOUTUBE_AI_DISCLOSURE)
    assert out.count("——") == 1
    assert out.rstrip().endswith(DEFAULT_YOUTUBE_AI_DISCLOSURE)
    assert "human-written" in out
    # Idempotent
    again = append_youtube_ai_disclosure(out, DEFAULT_YOUTUBE_AI_DISCLOSURE)
    assert again.count(DEFAULT_YOUTUBE_AI_DISCLOSURE) == 1


def test_append_replaces_legacy_disclosure_footer():
    legacy = (
        "Body.\n\n——\n"
        "This video includes AI-generated visuals and AI-assisted narration. "
        "Altered or synthetic media is disclosed per YouTube guidelines.\n"
    )
    out = append_youtube_ai_disclosure(legacy, DEFAULT_YOUTUBE_AI_DISCLOSURE)
    assert "AI-generated visuals and AI-assisted narration" not in out
    assert DEFAULT_YOUTUBE_AI_DISCLOSURE in out
    assert out.count("——") == 1


def test_strip_footer_keeps_body():
    full = append_youtube_ai_disclosure("Prose only.", DEFAULT_YOUTUBE_AI_DISCLOSURE)
    stripped = strip_youtube_ai_disclosure_footer(full)
    assert stripped == "Prose only."
    assert "——" not in stripped
