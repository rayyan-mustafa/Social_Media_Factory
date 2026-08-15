"""WHB free-first LLM router (no live network)."""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock

from src.services.llm import LLMError
from src.services.openrouter import OpenRouterError
from src.services.vision_judge import VISION_MODEL_LOCKED
from src.services.whb_llm_router import (
    FREE_ATTEMPTS,
    FREE_MODEL,
    STAGE_CLAIM_FIX,
    STAGE_SCRIPT,
    STAGE_SHOT_PLAN,
    STAGE_VALIDATE,
    WAVESPEED_FALLBACK_MODEL,
    WhbFreeFirstRouter,
    WhbLlmRouterError,
    channel_uses_free_first,
    stage_uses_free_first,
    strip_openrouter_safety_preamble,
)
from src.services.weird_biology_research import SourceItem, SourcePack
from src.services.weird_biology_scenes import VisualBeat, VisualTimeline
from src.services.weird_biology_script import SCRIPT_MODEL_DEFAULT, WeirdBiologyScriptWriter
from src.services.weird_biology_shot_plan import PLANNER_MODEL, WeirdBiologyShotPlanner
from src.services.weird_biology_validate import (
    FALLBACK_MODEL,
    OPENROUTER_MODEL,
    WeirdBiologyValidator,
)


ROOT = Path(__file__).resolve().parents[1]


class _FakeOpenRouter:
    def __init__(self, responses: list[object]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def chat_text(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise OpenRouterError("no more fake responses")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return str(item)


class _FakeLLM:
    def __init__(self, *, json_data=None, json_exc: Exception | None = None, text: str = ""):
        self.json_data = json_data
        self.json_exc = json_exc
        self.text = text
        self.chat_json_calls: list[dict] = []
        self.chat_text_calls: list[dict] = []

    def chat_json(self, **kwargs):
        self.chat_json_calls.append(kwargs)
        if self.json_exc is not None:
            raise self.json_exc
        return self.json_data

    def chat_text(self, **kwargs):
        self.chat_text_calls.append(kwargs)
        return self.text


def _tiny_timeline() -> VisualTimeline:
    return VisualTimeline(
        beats=[
            VisualBeat(
                index=0,
                text="Look at your arms.",
                section=1,
                section_name="SECTION 1",
                start_s=0.0,
                end_s=2.0,
                word_count=4,
            )
        ],
        full_vo_duration_s=2.0,
        topic="goosebumps",
    )


def test_channel_and_stage_gates():
    assert channel_uses_free_first("weird_human_biology")
    assert channel_uses_free_first("WHB")
    assert not channel_uses_free_first("napstorian")
    assert not channel_uses_free_first("napping_historian")
    assert stage_uses_free_first("weird_human_biology", STAGE_SHOT_PLAN)
    assert stage_uses_free_first("weird_human_biology", STAGE_VALIDATE)
    assert stage_uses_free_first("weird_human_biology", STAGE_CLAIM_FIX)
    assert not stage_uses_free_first("weird_human_biology", STAGE_SCRIPT)
    assert not stage_uses_free_first("weird_human_biology", "script_writer")
    assert not stage_uses_free_first("napstorian", STAGE_SHOT_PLAN)


def test_strip_safety_preamble():
    body, safety = strip_openrouter_safety_preamble(
        "User Safety: safe\nSafety Categories: None\n{\"shots\":[]}"
    )
    assert safety == "safe"
    assert body.startswith("{")


def test_free_first_success_skips_fallback():
    fake_or = _FakeOpenRouter(['{"ok": true}'])
    fallback = MagicMock(return_value={"ok": False, "via": "fallback"})
    router = WhbFreeFirstRouter(settings=MagicMock(), openrouter=fake_or)
    value, meta = router.call(
        system="s",
        user="u",
        parse=lambda t: {"raw": t},
        fallback=fallback,
        fallback_model=WAVESPEED_FALLBACK_MODEL,
        stage=STAGE_SHOT_PLAN,
    )
    assert value == {"raw": '{"ok": true}'}
    assert meta.model_used == FREE_MODEL
    assert meta.used_fallback is False
    assert len(fake_or.calls) == 1
    assert fake_or.calls[0]["model"] == FREE_MODEL
    fallback.assert_not_called()


def test_free_fails_twice_then_fallback():
    fake_or = _FakeOpenRouter(
        [OpenRouterError("boom1"), "not-json-so-parse-fails"]
    )
    fallback = MagicMock(return_value={"shots": []})
    router = WhbFreeFirstRouter(settings=MagicMock(), openrouter=fake_or)

    def _parse(text: str):
        raise LLMError(f"bad: {text[:20]}")

    value, meta = router.call(
        system="s",
        user="u",
        parse=_parse,
        fallback=fallback,
        fallback_model=WAVESPEED_FALLBACK_MODEL,
        stage=STAGE_SHOT_PLAN,
    )
    assert value == {"shots": []}
    assert meta.used_fallback is True
    assert meta.model_used == WAVESPEED_FALLBACK_MODEL
    assert len(fake_or.calls) == FREE_ATTEMPTS
    fallback.assert_called_once()


def test_script_stage_skips_free_router():
    fake_or = _FakeOpenRouter(["should not be called"])
    fallback = MagicMock(return_value="script-fallback")
    router = WhbFreeFirstRouter(
        settings=MagicMock(),
        channel="weird_human_biology",
        openrouter=fake_or,
    )
    value, meta = router.call(
        system="s",
        user="u",
        parse=lambda t: t,
        fallback=fallback,
        fallback_model=SCRIPT_MODEL_DEFAULT,
        stage=STAGE_SCRIPT,
    )
    assert value == "script-fallback"
    assert meta.skipped_free is True
    assert fake_or.calls == []
    fallback.assert_called_once()


def test_script_writer_does_not_use_router():
    text = (ROOT / "src/services/weird_biology_script.py").read_text(encoding="utf-8")
    assert "whb_llm_router" not in text
    assert "WhbFreeFirstRouter" not in text
    src = inspect.getsource(WeirdBiologyScriptWriter.generate)
    assert "WhbFreeFirstRouter" not in src
    assert SCRIPT_MODEL_DEFAULT == "google/gemini-2.5-flash"
    assert SCRIPT_MODEL_DEFAULT != FREE_MODEL


def test_vision_judge_stays_locked():
    assert VISION_MODEL_LOCKED == "openrouter/free"
    text = (ROOT / "src/services/vision_judge.py").read_text(encoding="utf-8")
    assert 'VISION_MODEL_LOCKED = "openrouter/free"' in text
    assert "fallback_model" not in text
    assert "WAVESPEED_FALLBACK_MODEL" not in text
    # Payload model is the lock constant, never a paid VL id.
    assert '"model": VISION_MODEL_LOCKED' in text
    assert "google/gemini" not in text
    assert "anthropic/" not in text


def test_shot_planner_free_success_skips_wavespeed():
    shots_json = '{"shots":[{"beat_index":0,"pose":"stand","emotion":"neutral","props":[],"kinetic_text":null,"xray":false,"internal":"nerve","camera":"wide","layout":"default","bubble_bars":0}]}'
    fake_or = _FakeOpenRouter(
        [f"User Safety: safe\n{shots_json}"]
    )
    fake_llm = _FakeLLM(json_data={"shots": [{"beat_index": 0, "pose": "point"}]})
    planner = WeirdBiologyShotPlanner(llm=fake_llm, openrouter=fake_or)
    plan = planner.plan(_tiny_timeline())
    assert plan["model"] == FREE_MODEL
    assert plan["fallback_model"] == PLANNER_MODEL
    assert plan["source"] == "llm"
    assert plan["shots"][0]["pose"] == "stand"
    assert len(fake_or.calls) == 1
    assert fake_llm.chat_json_calls == []
    assert fake_llm.chat_text_calls == []


def test_shot_planner_free_fails_twice_uses_flash_lite():
    fake_or = _FakeOpenRouter([OpenRouterError("e1"), OpenRouterError("e2")])
    fake_llm = _FakeLLM(
        json_data={
            "shots": [
                {
                    "beat_index": 0,
                    "pose": "think",
                    "emotion": "calm",
                    "props": [],
                    "xray": False,
                    "internal": "nerve",
                    "camera": "wide",
                    "layout": "default",
                    "bubble_bars": 0,
                }
            ]
        }
    )
    planner = WeirdBiologyShotPlanner(llm=fake_llm, openrouter=fake_or)
    plan = planner.plan(_tiny_timeline())
    assert plan["model"] == PLANNER_MODEL
    assert plan["shots"][0]["pose"] == "think"
    assert len(fake_or.calls) == 2
    assert len(fake_llm.chat_json_calls) == 1
    assert fake_llm.chat_json_calls[0]["model"] == PLANNER_MODEL


def test_wired_fallback_ids():
    assert FREE_MODEL == "openrouter/free"
    assert OPENROUTER_MODEL == FREE_MODEL
    assert PLANNER_MODEL == "google/gemini-2.5-flash-lite"
    assert FALLBACK_MODEL == "google/gemini-2.5-flash-lite"
    assert WAVESPEED_FALLBACK_MODEL == "google/gemini-2.5-flash-lite"


def test_router_both_fail_raises():
    fake_or = _FakeOpenRouter([OpenRouterError("e1"), OpenRouterError("e2")])
    router = WhbFreeFirstRouter(settings=MagicMock(), openrouter=fake_or)

    def _boom():
        raise LLMError("fallback down")

    try:
        router.call(
            system="s",
            user="u",
            parse=lambda t: t,
            fallback=_boom,
            fallback_model=WAVESPEED_FALLBACK_MODEL,
            stage=STAGE_VALIDATE,
        )
        raise AssertionError("expected WhbLlmRouterError")
    except WhbLlmRouterError as exc:
        assert "openrouter×2" in str(exc)


def _pack() -> SourcePack:
    return SourcePack(
        topic="goosebumps",
        fetched_at="2026-01-01T00:00:00Z",
        items=[
            SourceItem(
                url="https://pubmed.ncbi.nlm.nih.gov/1",
                title="Piloerection",
                fetched_at="2026-01-01T00:00:00Z",
                excerpt=("Goosebumps arise from arrector pili muscle contraction. " * 4),
                source_hub="pubmed",
            )
        ],
    )


def test_validator_free_success_skips_fallback():
    fake_or = _FakeOpenRouter(["ALL CLAIMS VERIFIED\n\nNo issues."])
    fake_ws = _FakeLLM(text="VALIDATION FAILED — 9 UNVERIFIED CLAIMS")
    v = WeirdBiologyValidator(
        settings=MagicMock(),
        openrouter=fake_or,
        wavespeed=fake_ws,
    )
    result = v.validate("Goosebumps are a reflex.", _pack())
    assert result.ok is True
    assert result.model_used == FREE_MODEL
    assert len(fake_or.calls) == 1
    assert fake_ws.chat_text_calls == []


def test_validator_free_fails_twice_uses_flash_lite():
    fake_or = _FakeOpenRouter([OpenRouterError("e1"), OpenRouterError("e2")])
    fake_ws = _FakeLLM(text="ALL CLAIMS VERIFIED\n\nOk.")
    v = WeirdBiologyValidator(
        settings=MagicMock(),
        openrouter=fake_or,
        wavespeed=fake_ws,
    )
    result = v.validate("Goosebumps are a reflex.", _pack())
    assert result.ok is True
    assert result.model_used == FALLBACK_MODEL
    assert len(fake_or.calls) == 2
    assert len(fake_ws.chat_text_calls) == 1
    assert fake_ws.chat_text_calls[0]["model"] == FALLBACK_MODEL
