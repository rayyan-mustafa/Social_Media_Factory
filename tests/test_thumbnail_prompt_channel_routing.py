"""napping_historian uses Secret Document thumbnail pack; napstorian stays default."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.domain.models import Outline, OutlineChapter, Scene, ScriptResult, ScriptValidation
from src.services.settings import PROMPTS_DIR, ROOT
from src.services.thumbnail_prompt import (
    build_thumbnail_prompt,
    resolve_thumbnail_template_path,
)


HISTORIAN_TEMPLATE = (
    ROOT / "config" / "prompts" / "napping_historian" / "thumbnail_template.txt"
)
DEFAULT_TEMPLATE = PROMPTS_DIR / "thumbnail_template.txt"


def _script(title: str) -> ScriptResult:
    return ScriptResult(
        topic=title,
        title=title,
        hook=f"Opening beat for {title}",
        outline=Outline(
            title=title,
            chapters=[OutlineChapter(id=1, title="c1", target_sentences=1)],
        ),
        scenes=[
            Scene(
                index=0,
                text="a sealed letter surfaces in the archive.",
                visual_prompt="parchment seal candlelight",
            )
        ],
        validation=ScriptValidation(
            ok=True,
            scene_count=1,
            min_scenes=1,
            max_scenes=12,
            target_scenes=5,
            estimated_duration_s=10.0,
        ),
    )


@pytest.fixture(autouse=True)
def _clear_script_channel(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SCRIPT_CHANNEL", raising=False)


def test_resolve_historian_template_path():
    assert HISTORIAN_TEMPLATE.exists()
    path = resolve_thumbnail_template_path(channel="napping_historian")
    assert path.resolve() == HISTORIAN_TEMPLATE.resolve()


def test_resolve_napstorian_uses_default_not_epic():
    path = resolve_thumbnail_template_path(channel="napstorian")
    assert path.resolve() == DEFAULT_TEMPLATE.resolve()
    assert "Secret Document" not in path.read_text(encoding="utf-8")


def test_resolve_unset_channel_default():
    path = resolve_thumbnail_template_path()
    assert path.resolve() == DEFAULT_TEMPLATE.resolve()


def test_resolve_respects_script_channel_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCRIPT_CHANNEL", "napping_historian")
    path = resolve_thumbnail_template_path()
    assert path.resolve() == HISTORIAN_TEMPLATE.resolve()


def test_build_historian_prompt_uses_secret_document_concept():
    title = "The Forbidden Letter That Doomed a Dynasty"
    result = build_thumbnail_prompt(
        _script(title),
        seed_title=title,
        mode="rules",
        channel="napping_historian",
    )
    assert "Secret Document" in result.prompt
    assert "aged parchment" in result.prompt.lower() or "parchment" in result.prompt.lower()
    assert "ad-safe" in result.prompt.lower() or "NOT fresh gore" in result.prompt
    assert "SHE SURVIVED" not in result.prompt
    # Gold text still burned into Seedream prompt (no separate overlay).
    assert "gold serif" in result.prompt.lower()


def test_build_napstorian_prompt_unchanged_portrait_pack():
    title = "What If the Tudor Dynasty Fell in 1549?"
    result = build_thumbnail_prompt(
        _script(title),
        seed_title=title,
        mode="rules",
        channel="napstorian",
    )
    assert "Secret Document" not in result.prompt
    assert "Renaissance oil painting portrait" in result.prompt
    assert "SHE SURVIVED" not in result.prompt
