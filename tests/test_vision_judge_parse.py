"""Vision judge parser + structured-output helpers (no live network)."""

from __future__ import annotations

from src.services.vision_judge import _JUDGE_RESPONSE_FORMAT, _parse_judge_json


def test_parse_strips_safety_and_json():
    text = "User Safety: safe\nResponse Safety: safe\n{\"status\":\"PASS\",\"confidence\":9,\"reason\":\"match\"}"
    obj = _parse_judge_json(text)
    assert obj is not None
    assert obj["status"] == "PASS"
    assert obj["confidence"] == 9


def test_parse_markdown_fence():
    text = '```json\n{"status":"FAIL","confidence":2,"reason":"off"}\n```'
    obj = _parse_judge_json(text)
    assert obj is not None
    assert obj["status"] == "FAIL"


def test_parse_safety_only_returns_none():
    assert _parse_judge_json("User Safety: safe") is None


def test_parse_unsafe_gate():
    obj = _parse_judge_json("User Safety: unsafe\nSafety Categories: Violence")
    assert obj is not None
    assert obj["status"] == "FAIL"
    assert obj["confidence"] == 0


def test_judge_response_format_schema_shape():
    assert _JUDGE_RESPONSE_FORMAT["type"] == "json_schema"
    props = _JUDGE_RESPONSE_FORMAT["json_schema"]["schema"]["properties"]
    assert set(props) == {"status", "confidence", "reason"}
