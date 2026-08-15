"""Unit tests for runpod_stills_benchmark_v1 capacity pre-flight."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.runpod import capacity
from src.runpod.capacity import (
    CapacityDeferError,
    GpuLevelProbe,
    apply_secure_only_to_spec,
    assert_capacity_allows_stills,
    classify_levels,
    decide,
    evaluate_schedule,
    run_capacity_benchmark,
    stock_is_available,
)


class FakeClient:
    def __init__(self, stocks: dict[tuple[str, str], str | None]):
        # key: (gpu, COMMUNITY|SECURE) → stockStatus
        self.stocks = stocks
        self.calls: list[tuple[str, bool]] = []

    def graphql(self, query: str, variables: dict | None = None):
        variables = variables or {}
        gpu = variables["id"]
        secure = bool(variables["secure"])
        cloud = "SECURE" if secure else "COMMUNITY"
        self.calls.append((gpu, secure))
        status = self.stocks.get((gpu, cloud))
        return {
            "data": {
                "gpuTypes": [
                    {
                        "id": gpu,
                        "displayName": gpu,
                        "lowestPrice": {
                            "stockStatus": status,
                            "uninterruptablePrice": 0.22 if status else None,
                            "availableGpuCounts": [1] if status else None,
                        },
                    }
                ]
            }
        }


def test_stock_is_available():
    assert stock_is_available("High")
    assert stock_is_available("medium")
    assert stock_is_available("LOW")
    assert not stock_is_available("None")
    assert not stock_is_available(None)
    assert not stock_is_available("")


def test_classify_green_yellow_red():
    green = [
        GpuLevelProbe("NVIDIA RTX A5000", "COMMUNITY", "Low", True),
        GpuLevelProbe("NVIDIA A40", "SECURE", "None", False),
    ]
    c, comm, sec = classify_levels(green, secure_only_factory=False)
    assert c == "GREEN" and comm and not sec

    yellow = [
        GpuLevelProbe("NVIDIA RTX A5000", "COMMUNITY", "None", False),
        GpuLevelProbe("NVIDIA GeForce RTX 3090", "COMMUNITY", "None", False),
        GpuLevelProbe("NVIDIA RTX A5000", "SECURE", "Low", True),
    ]
    c, comm, sec = classify_levels(yellow, secure_only_factory=False)
    assert c == "YELLOW" and not comm and sec

    # Secure-only factory: Secure stock is GREEN (A40 Secure lock).
    secure_only = [
        GpuLevelProbe("NVIDIA A40", "SECURE", "Medium", True),
    ]
    c, comm, sec = classify_levels(secure_only, secure_only_factory=True)
    assert c == "GREEN" and not comm and sec

    red = [
        GpuLevelProbe("NVIDIA RTX A5000", "COMMUNITY", "None", False),
        GpuLevelProbe("NVIDIA A40", "SECURE", None, False),
    ]
    c, _, _ = classify_levels(red, secure_only_factory=False)
    assert c == "RED"


def test_decide_yellow_defers_without_allow_secure():
    d, allow, msg = decide(
        "YELLOW",
        allow_secure_flag=False,
        in_window=True,
        require_window_flag=False,
        secure_only_factory=False,
    )
    assert d == "defer" and allow is False
    assert "ALLOW_SECURE" in msg

    d, allow, _ = decide(
        "YELLOW",
        allow_secure_flag=True,
        in_window=True,
        require_window_flag=False,
        secure_only_factory=False,
    )
    assert d == "proceed_secure_only" and allow is True


def test_decide_secure_only_red_waits_for_green():
    d, allow, msg = decide(
        "RED",
        allow_secure_flag=True,
        in_window=True,
        require_window_flag=False,
        secure_only_factory=True,
    )
    assert d == "skip" and allow is False
    assert "wait for next GREEN" in msg
    assert "no fallbacks" in msg


def test_decide_secure_only_yellow_defers():
    d, allow, msg = decide(
        "YELLOW",
        allow_secure_flag=True,
        in_window=True,
        require_window_flag=False,
        secure_only_factory=True,
    )
    assert d == "defer" and allow is False
    assert "no GPU fallbacks" in msg


def test_decide_require_window_blocks():
    d, allow, msg = decide(
        "GREEN",
        allow_secure_flag=False,
        in_window=False,
        require_window_flag=True,
    )
    assert d == "defer" and allow is False
    assert "REQUIRE_WINDOW" in msg


def test_schedule_primary_window():
    # 08:30 UTC → primary
    now = datetime(2026, 8, 7, 8, 30, tzinfo=timezone.utc)
    ok, label, _ = evaluate_schedule(now=now)
    assert ok and label == "primary"

    # 04:00 UTC → backup
    now = datetime(2026, 8, 7, 4, 0, tzinfo=timezone.utc)
    ok, label, _ = evaluate_schedule(now=now)
    assert ok and label == "backup"

    # 14:00 UTC → outside
    now = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)
    ok, label, advice = evaluate_schedule(now=now)
    assert not ok and label == ""
    assert "Outside" in advice


_LEGACY_COMMUNITY_LEVELS = [
    ("NVIDIA RTX A5000", "COMMUNITY"),
    ("NVIDIA GeForce RTX 3090", "COMMUNITY"),
    ("NVIDIA RTX A5000", "SECURE"),
    ("NVIDIA A40", "SECURE"),
]


def test_benchmark_green_proceed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    jsonl = tmp_path / "bench.jsonl"
    monkeypatch.setattr(capacity, "JSONL_PATH", jsonl)
    client = FakeClient(
        {
            ("NVIDIA RTX A5000", "COMMUNITY"): "Medium",
            ("NVIDIA GeForce RTX 3090", "COMMUNITY"): "None",
            ("NVIDIA RTX A5000", "SECURE"): "Low",
            ("NVIDIA A40", "SECURE"): "Medium",
        }
    )
    result = run_capacity_benchmark(
        client=client,
        levels=_LEGACY_COMMUNITY_LEVELS,
        jsonl_path=jsonl,
        allow_secure_flag=False,
        require_window_flag=False,
        secure_only_factory=False,
        now=datetime(2026, 8, 7, 8, 0, tzinfo=timezone.utc),
    )
    assert result.scheme == "runpod_stills_benchmark_v1"
    assert result.classification == "GREEN"
    assert result.decision == "proceed"
    assert result.allow_create is True
    assert jsonl.exists()
    row = json.loads(jsonl.read_text().splitlines()[0])
    assert row["classification"] == "GREEN"
    # No create calls — FakeClient only has graphql
    assert all(isinstance(c, tuple) for c in client.calls)


def test_benchmark_yellow_defers(tmp_path: Path):
    client = FakeClient(
        {
            ("NVIDIA RTX A5000", "COMMUNITY"): None,
            ("NVIDIA GeForce RTX 3090", "COMMUNITY"): "None",
            ("NVIDIA RTX A5000", "SECURE"): "High",
            ("NVIDIA A40", "SECURE"): "Low",
        }
    )
    result = run_capacity_benchmark(
        client=client,
        levels=_LEGACY_COMMUNITY_LEVELS,
        log_jsonl=False,
        allow_secure_flag=False,
        require_window_flag=False,
        secure_only_factory=False,
        now=datetime(2026, 8, 7, 8, 0, tzinfo=timezone.utc),
    )
    assert result.classification == "YELLOW"
    assert result.decision == "defer"
    assert result.allow_create is False


def test_benchmark_a40_secure_only_is_green():
    """A40 Secure-only factory: Secure stock → GREEN (farm can proceed)."""
    client = FakeClient({("NVIDIA A40", "SECURE"): "Low"})
    result = run_capacity_benchmark(
        client=client,
        levels=[("NVIDIA A40", "SECURE")],
        log_jsonl=False,
        allow_secure_flag=True,
        require_window_flag=False,
        secure_only_factory=True,
        now=datetime(2026, 8, 7, 8, 0, tzinfo=timezone.utc),
    )
    assert result.classification == "GREEN"
    assert result.decision == "proceed"
    assert result.allow_create is True
    assert result.secure_available


def test_assert_raises_on_red(monkeypatch: pytest.MonkeyPatch):
    client = FakeClient(
        {
            ("NVIDIA RTX A5000", "COMMUNITY"): None,
            ("NVIDIA GeForce RTX 3090", "COMMUNITY"): None,
            ("NVIDIA RTX A5000", "SECURE"): None,
            ("NVIDIA A40", "SECURE"): None,
        }
    )
    monkeypatch.setenv("RUNPOD_CAPACITY_PROBE", "1")

    def _bench(**kwargs):
        kwargs.setdefault("levels", _LEGACY_COMMUNITY_LEVELS)
        kwargs.setdefault("secure_only_factory", False)
        kwargs["client"] = client
        kwargs["log_jsonl"] = False
        return run_capacity_benchmark(**kwargs)

    monkeypatch.setattr(capacity, "run_capacity_benchmark", _bench)
    with pytest.raises(CapacityDeferError, match="RED"):
        assert_capacity_allows_stills(client=client)


def test_assert_skip_disabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RUNPOD_CAPACITY_PROBE", "0")
    assert assert_capacity_allows_stills(skip=False) is None
    assert assert_capacity_allows_stills(skip=True) is None


def test_apply_secure_only_to_spec():
    spec = SimpleNamespace(
        gpu_type_id="NVIDIA RTX A5000",
        cloud_type="COMMUNITY",
        gpu_fallbacks=[
            ("NVIDIA GeForce RTX 3090", "COMMUNITY"),
            ("NVIDIA RTX A5000", "SECURE"),
            ("NVIDIA A40", "SECURE"),
        ],
    )
    apply_secure_only_to_spec(spec)
    assert spec.cloud_type == "SECURE"
    assert spec.gpu_type_id == "NVIDIA RTX A5000"
    assert all(c == "SECURE" for _, c in spec.gpu_fallbacks)


def test_ephemeral_stills_session_calls_benchmark(monkeypatch: pytest.MonkeyPatch):
    """Dry-read: EphemeralStillsSession.__enter__ invokes capacity assert."""
    from src.services import visuals_runpod_pod as vrp

    calls: list[dict] = []

    def fake_assert(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            decision="proceed",
            classification="GREEN",
            to_dict=lambda: {"classification": "GREEN"},
        )

    class BoomSession:
        def __init__(self, spec):
            self.spec = spec
            self.base_url = None
            self.pod_id = None

        def __enter__(self):
            raise RuntimeError("stop before create — probe already ran")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(vrp, "assert_farm_capacity_green", fake_assert)
    monkeypatch.setattr(vrp, "stills_pod_spec_from_settings", lambda: SimpleNamespace(
        gpu_type_id="NVIDIA RTX A5000",
        cloud_type="COMMUNITY",
        gpu_fallbacks=[],
    ))
    monkeypatch.setattr(vrp, "EphemeralPodSession", BoomSession)
    monkeypatch.setattr(vrp, "ComfyPodClient", lambda url: None)

    sess = vrp.EphemeralStillsSession()
    with pytest.raises(RuntimeError, match="stop before create"):
        sess.__enter__()
    assert len(calls) == 1
    assert calls[0].get("skip") is False
