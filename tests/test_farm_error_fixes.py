"""Farm error-class fixes: beats fill, tag sanitize, Comfy 502, classify."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.agents.repair_watchdog import classify_failure
from src.runpod.comfy_pod import ComfyPodClient, _is_transient_comfy_response
from src.services.youtube_meta import (
    normalize_youtube_tags,
    sanitize_youtube_tag,
    rewrite_tags_json,
)


def test_classify_beat_count_is_script_not_tts():
    err = "Chapter 3 (a) returned 7 beats, target 16"
    assert classify_failure(err) == "script"


def test_classify_comfy_502_waiting_service():
    err = (
        "scene 121 failed: failed after 3 attempts: Comfy /prompt HTTP 502: "
        "<!DOCTYPE html><title>Waiting for service to respond — RunPod</title>"
    )
    assert classify_failure(err) == "runpod_transient"


def test_classify_invalid_tags():
    err = (
        'HttpError 400 ... "The request metadata specifies invalid video keywords." '
        "... 'reason': 'invalidTags'"
    )
    assert classify_failure(err) == "invalid_tags"


def test_classify_stuck_queued():
    assert classify_failure("stuck in queued for 2673s > 1800s") == "process_crash"


def test_classify_out_of_stock_is_capacity_out_not_transient():
    from src.agents.repair_watchdog import AUTO_RESUME_CLASSES, CAPACITY_HOLD_CLASSES

    err = (
        "OUT_OF_STOCK — NVIDIA A40/SECURE unavailable; no fallbacks; "
        "wait for next GREEN window (do not retry)"
    )
    assert classify_failure(err) == "capacity_out"
    assert "capacity_out" in CAPACITY_HOLD_CLASSES
    assert "capacity_out" not in AUTO_RESUME_CLASSES
    assert (
        classify_failure(
            "capacity pre-flight deferred stills — HOLD_CAPACITY awaiting GREEN"
        )
        == "capacity_out"
    )
    assert (
        classify_failure("FARM SPAWN BLOCKED (green-light only): RED: A40 Secure out")
        == "capacity_out"
    )


def test_sanitize_youtube_tag_strips_invalid():
    assert "<" not in sanitize_youtube_tag("Roman <empire> history")
    assert sanitize_youtube_tag("<<<>>>") == ""
    assert sanitize_youtube_tag("#WhatIf History") == "WhatIf History"
    cleaned = normalize_youtube_tags(
        [
            "Roman <Empire>",
            "history|docs",
            "ok-tag",
            "a" * 120,
            "",
            "<<<",
            "duplicate",
            "Duplicate",
        ]
    )
    assert all("<" not in t and "|" not in t for t in cleaned)
    assert "ok-tag" in cleaned
    assert sum(1 for t in cleaned if t.lower() == "duplicate") == 1
    assert all(len(t) <= 100 for t in cleaned)


def test_rewrite_tags_json(tmp_path: Path):
    path = tmp_path / "tags.json"
    path.write_text(
        '["Roman <Empire>", "what-if, timeline", "ok"]\n',
        encoding="utf-8",
    )
    out = rewrite_tags_json(path)
    assert out["changed"] is True
    assert all("<" not in t for t in out["after"])
    assert "ok" in out["after"]


def test_transient_comfy_detection():
    html = "<html><title>Waiting for service to respond — RunPod</title></html>"
    assert _is_transient_comfy_response(502, html) is True
    assert _is_transient_comfy_response(400, "bad workflow") is False


def test_comfy_queue_prompt_retries_502(monkeypatch: pytest.MonkeyPatch):
    client = ComfyPodClient(
        "http://example.invalid",
        prompt_retries=3,
        prompt_retry_base_s=0.01,
        prompt_retry_max_s=0.02,
    )
    calls = {"n": 0}

    class _Resp:
        def __init__(self, status: int, payload: Any = None, text: str = ""):
            self.status_code = status
            self._payload = payload
            self.text = text

        def json(self):
            return self._payload

    def fake_post(url, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            return _Resp(
                502,
                text="<html><title>Waiting for service to respond — RunPod</title>",
            )
        return _Resp(200, payload={"prompt_id": "abc123"})

    monkeypatch.setattr("src.runpod.comfy_pod.httpx.post", fake_post)
    monkeypatch.setattr(
        ComfyPodClient,
        "wait_service_ready",
        lambda self, **kw: None,
    )
    monkeypatch.setattr("src.runpod.comfy_pod.time.sleep", lambda *_a, **_k: None)
    pid = client.queue_prompt({"1": {"class_type": "X"}})
    assert pid == "abc123"
    assert calls["n"] == 3


def test_expand_accepts_partial_then_fills(monkeypatch: pytest.MonkeyPatch):
    """Undersized LLM chunks are accepted and filled across parts."""
    from src.services.script import ScriptModule

    mod = ScriptModule.__new__(ScriptModule)
    mod.settings = MagicMock()
    mod.settings.script_max_retries = 1
    mod.settings.llm_json_same_model_retries = 0
    mod.expand_model = "mock"
    mod.expand_fallback_model = ""
    mod.llm = MagicMock()

    def _beat(i: int, prefix: str) -> dict:
        return {
            "text": f"{prefix} beat number {i} here now calmly.",
            "beat_type": "narration",
            "visual_prompt": f"still of historical scene {i}",
        }

    # First call asks for 8, returns 5; second fills remaining.
    payloads = [
        {"chapter_id": 3, "sentences": [_beat(i, "Short") for i in range(5)]},
        {"chapter_id": 3, "sentences": [_beat(i, "Follow") for i in range(3)]},
    ]
    mod.llm.chat_json.side_effect = payloads

    monkeypatch.setattr(
        "src.services.script.validate_chapter_sentences",
        lambda *a, **k: MagicMock(ok=True, warnings=[], errors=[]),
    )

    from src.domain.models import Outline, OutlineChapter

    outline = Outline(
        title="t",
        hook="h",
        chapters=[
            OutlineChapter(
                id=3,
                title="Ch3",
                goal="g",
                key_points=["k"],
                target_sentences=8,
                pacing_phase="a",
            )
        ],
        estimated_sentence_budget=8,
    )
    mod.prompts_dir = Path(".")
    mod.style_hint = ""
    mod.file_cfg = {}
    mod.pacing = MagicMock(
        phase_a_words_min=6,
        phase_a_words_max=12,
        phase_a_avg_words=9,
        phase_a_seconds=3,
        phase_b_words_min=50,
        phase_b_words_max=80,
        phase_b_words=60,
        phase_b_seconds=120,
    )
    monkeypatch.setattr(
        "src.services.script.load_prompt",
        lambda *_a, **_k: "Expand chapter {{CHAPTER_ID}} target {{TARGET_SENTENCES}}",
    )
    monkeypatch.setattr(
        "src.services.script.render_prompt",
        lambda tmpl, vals: tmpl,
    )

    exp = mod._expand_chapter(outline, outline.chapters[0])
    assert len(exp.sentences) == 8
