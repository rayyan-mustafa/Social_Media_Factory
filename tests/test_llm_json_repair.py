"""Tests for LLM JSON repair + same-model retries + model failover → HOLD."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.services.llm import JSON_FAILOVER_EXHAUSTED, LLMClient, LLMError, parse_json_object


def test_parse_strips_markdown_fence():
    raw = '```json\n{"title": "x", "hook": "y"}\n```'
    assert parse_json_object(raw) == {"title": "x", "hook": "y"}


def test_parse_extracts_object_from_prose():
    raw = 'Sure! Here you go:\n{"a": 1, "b": [2, 3]}\nThanks!'
    assert parse_json_object(raw) == {"a": 1, "b": [2, 3]}


def test_parse_repairs_trailing_commas():
    raw = '{\n  "title": "July 20th",\n  "chapters": [{"id": "c1"},],\n}'
    obj = parse_json_object(raw)
    assert obj["title"] == "July 20th"
    assert obj["chapters"] == [{"id": "c1"}]


def test_parse_repairs_smart_quotes():
    raw = '{“title”: “hello”, “n”: 1}'
    assert parse_json_object(raw) == {"title": "hello", "n": 1}


def test_parse_raises_llm_error_not_json_decode():
    with pytest.raises(LLMError, match="Could not parse JSON"):
        parse_json_object("not json at all {{{")


def test_parse_closes_truncated_object_mid_string():
    raw = (
        '{\n  "title": "The Sultan\'s Dream",\n  "hook": "A letter",\n'
        '  "estimated_sentence_budget": 125,\n  "chapters": [\n'
        '    {\n      "id": 1,\n      "title": "The Hook",\n'
        '      "goal": "Open on the moment of Ottoman breakthrough, then pivot'
    )
    obj = parse_json_object(raw)
    assert obj["title"] == "The Sultan's Dream"
    assert obj["estimated_sentence_budget"] == 125
    assert isinstance(obj["chapters"], list)
    assert obj["chapters"][0]["id"] == 1


def _settings(**kw: Any) -> MagicMock:
    settings = MagicMock()
    settings.wavespeed_api_key = "test-key"
    settings.llm_api_key = ""
    settings.llm_base_url = "https://example.invalid/v1"
    settings.llm_outline_model = "deepseek/deepseek-v3.2"
    settings.llm_model = "deepseek/deepseek-v3.2"
    settings.llm_send_temperature = True
    settings.llm_reasoning_enabled = False
    settings.llm_json_response_format = False
    settings.llm_timeout_s = 30.0
    settings.llm_max_retries = 0
    settings.llm_json_same_model_retries = 2
    settings.llm_json_parse_retries = 3
    for k, v in kw.items():
        setattr(settings, k, v)
    return settings


def test_chat_json_same_model_two_retries_then_ok(monkeypatch: pytest.MonkeyPatch):
    """1 initial + 2 retries on same model; succeeds on 3rd call."""
    client = LLMClient(_settings())
    models: list[str] = []

    def fake_chat(messages, **kw) -> dict[str, Any]:
        models.append(kw.get("model") or "default")
        n = len(models)
        if n < 3:
            return {"role": "assistant", "content": "{bad json,,,}"}
        return {"role": "assistant", "content": json.dumps({"ok": True, "n": n})}

    monkeypatch.setattr(client, "_chat_message", fake_chat)
    out = client.chat_json(
        system="s",
        user="u",
        model="deepseek/deepseek-v3.2",
        same_model_retries=2,
    )
    assert out == {"ok": True, "n": 3}
    assert len(models) == 3
    assert set(models) == {"deepseek/deepseek-v3.2"}


def test_chat_json_failover_after_same_model_retries(monkeypatch: pytest.MonkeyPatch):
    """Primary 3 fails → switch to fallback → success on first fallback call."""
    client = LLMClient(_settings())
    models: list[str] = []

    def fake_chat(messages, **kw) -> dict[str, Any]:
        m = kw.get("model") or "default"
        models.append(m)
        if m.startswith("deepseek"):
            return {"role": "assistant", "content": "{bad"}
        return {
            "role": "assistant",
            "content": json.dumps({"ok": True, "via": m}),
        }

    monkeypatch.setattr(client, "_chat_message", fake_chat)
    out = client.chat_json(
        system="s",
        user="u",
        model="deepseek/deepseek-v3.2",
        fallback_model="anthropic/claude-3-haiku",
        same_model_retries=2,
    )
    assert out["ok"] is True
    assert out["via"] == "anthropic/claude-3-haiku"
    assert models.count("deepseek/deepseek-v3.2") == 3
    assert models.count("anthropic/claude-3-haiku") == 1


def test_chat_json_failover_exhausted_raises(monkeypatch: pytest.MonkeyPatch):
    """Primary 1+2 + fallback 1+2 all fail → failover exhausted (6 calls)."""
    client = LLMClient(_settings())
    models: list[str] = []

    def always_bad(messages, **kw) -> dict[str, Any]:
        models.append(kw.get("model") or "?")
        return {"role": "assistant", "content": "{not: valid}"}

    monkeypatch.setattr(client, "_chat_message", always_bad)
    with pytest.raises(LLMError, match=JSON_FAILOVER_EXHAUSTED):
        client.chat_json(
            system="s",
            user="u",
            model="deepseek/deepseek-v3.2",
            fallback_model="anthropic/claude-3-haiku",
            same_model_retries=2,
        )
    assert len(models) == 6
    assert models.count("deepseek/deepseek-v3.2") == 3
    assert models.count("anthropic/claude-3-haiku") == 3


def test_repair_watchdog_holds_on_failover_exhausted(tmp_path, monkeypatch):
    from src.agents.repair_watchdog import RepairWatchdog
    from src.agents.store import OpsStore
    from src.services.llm import JSON_FAILOVER_EXHAUSTED

    store = OpsStore(tmp_path / "ops")
    job_dir = tmp_path / "job_empty"
    for sub in ("script", "audio", "images", "video"):
        (job_dir / sub).mkdir(parents=True)
    job = store.create_job("What If Failover?", status="failed", stage="failed")
    store.update_job(
        job.id,
        error=(
            f"{JSON_FAILOVER_EXHAUSTED} "
            "(deepseek/deepseek-v3.2×3; anthropic/claude-3-haiku×3): boom"
        ),
        job_dir=str(job_dir),
        meta={"farm_phase": "prep"},
    )

    monkeypatch.setattr(
        "src.agents.repair_watchdog.spawn_farm_job",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    actions = RepairWatchdog(store=store).scan_script_json_failures(spawn=True)
    assert any(a.get("held") or a.get("blocked") for a in actions)
    refreshed = store.get_job(job.id)
    assert refreshed.status == "hold"
