"""Vision free-pool 429 pacing helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.services.vision_judge import (
    _VisionRateGate,
    _retry_after_seconds,
)


def test_retry_after_header_seconds():
    resp = MagicMock()
    resp.headers = {"Retry-After": "7"}
    assert _retry_after_seconds(resp, attempt=0) == 7.0


def test_retry_after_exponential_fallback():
    resp = MagicMock()
    resp.headers = {}
    # attempt 0 → 2, attempt 1 → 4, attempt 2 → 8 (capped at 25)
    assert _retry_after_seconds(resp, attempt=0) == 2.0
    assert _retry_after_seconds(resp, attempt=1) == 4.0
    assert _retry_after_seconds(resp, attempt=2) == 8.0


def test_retry_after_caps_large_header():
    resp = MagicMock()
    resp.headers = {"Retry-After": "90"}
    assert _retry_after_seconds(resp, attempt=0) == 25.0


def test_rate_gate_release_does_not_shrink_429_cooldown():
    g = _VisionRateGate()
    g.acquire()
    g.note_429(30.0)
    cooldown = g._next_ok
    g.release(penalize_s=0.0)
    assert g._next_ok >= cooldown
