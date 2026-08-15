"""Ephemeral pod sessions must auto-log their cost on exit (no real pods)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import src.runpod.cost_log as cost_log
from src.agents.store import OpsStore
from src.runpod.lifecycle import EphemeralPodSession, PodSpec


class FakeClient:
    def __init__(self):
        self.terminated: list[str] = []

    def terminate_pod(self, pod_id: str) -> None:
        self.terminated.append(pod_id)


def _fake_session(pod: dict) -> EphemeralPodSession:
    spec = PodSpec(
        name_prefix="yt-stills",
        gpu_type_id="NVIDIA GeForce RTX 3090",
        image_name="yanwk/comfyui-boot:cu124-slim",
    )
    session = EphemeralPodSession(spec=spec, client=FakeClient())
    session.pod = pod
    return session


def test_exit_writes_jsonl_and_spend_ledger(tmp_path: Path, monkeypatch):
    store = OpsStore(tmp_path / "ops")
    monkeypatch.setattr(cost_log, "JSONL_PATH", tmp_path / "runpod_pod_costs.jsonl")
    monkeypatch.setattr(cost_log, "_make_store", lambda: store)

    started = datetime.now(timezone.utc) - timedelta(minutes=30)
    session = _fake_session(
        {
            "id": "fakepod123",
            "costPerHr": 0.22,
            "createdAt": started.isoformat(),
        }
    )
    session.selected_gpu_type_id = "NVIDIA GeForce RTX 3090"
    session.selected_cloud_type = "COMMUNITY"

    session.__exit__(None, None, None)

    # Pod terminated, then cost logged.
    assert session.client.terminated == ["fakepod123"]
    lines = (tmp_path / "runpod_pod_costs.jsonl").read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["pod_id"] == "fakepod123"
    assert rec["purpose"] == "yt-stills"
    assert rec["gpu"] == "NVIDIA GeForce RTX 3090"
    assert rec["cloud"] == "COMMUNITY"
    assert rec["usd_per_hour"] == 0.22
    assert 29.0 <= rec["minutes"] <= 31.0
    assert abs(rec["estimated_usd"] - 0.11) < 0.005

    spend = json.loads((tmp_path / "ops" / "spend.json").read_text())
    items = [i for i in spend["items"] if i["category"] == "runpod"]
    assert len(items) == 1
    assert items[0]["amount_usd"] == rec["estimated_usd"]
    assert "fakepod123" in items[0]["note"]
    assert "yt-stills" in items[0]["note"]


def test_rate_table_fallback_when_pod_payload_has_no_cost(
    tmp_path: Path, monkeypatch
):
    store = OpsStore(tmp_path / "ops")
    monkeypatch.setattr(cost_log, "JSONL_PATH", tmp_path / "runpod_pod_costs.jsonl")
    monkeypatch.setattr(cost_log, "_make_store", lambda: store)

    # GraphQL create payload: no costPerHr/createdAt → rate table +
    # session created_at_utc fallback.
    session = _fake_session({"id": "gqlpod456"})
    session.created_at_utc = (
        datetime.now(timezone.utc) - timedelta(minutes=60)
    ).isoformat()
    session.selected_gpu_type_id = "NVIDIA GeForce RTX 3090"
    session.selected_cloud_type = "COMMUNITY"

    session.__exit__(None, None, None)

    rec = json.loads(
        (tmp_path / "runpod_pod_costs.jsonl").read_text().splitlines()[0]
    )
    assert rec["usd_per_hour"] == 0.22
    assert 59.0 <= rec["minutes"] <= 61.0
    assert abs(rec["estimated_usd"] - 0.22) < 0.005
    assert store.month_spend_total() == rec["estimated_usd"]


def test_cost_logging_failure_never_breaks_exit(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cost_log, "JSONL_PATH", tmp_path / "runpod_pod_costs.jsonl")

    def boom():
        raise RuntimeError("store down")

    monkeypatch.setattr(cost_log, "_make_store", boom)
    session = _fake_session(
        {
            "id": "fakepod789",
            "costPerHr": 0.22,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
    )
    # Must not raise even though the spend store is broken.
    session.__exit__(None, None, None)
    assert session.client.terminated == ["fakepod789"]
    # JSONL line still written.
    assert (tmp_path / "runpod_pod_costs.jsonl").exists()


def test_no_pod_no_log(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cost_log, "JSONL_PATH", tmp_path / "runpod_pod_costs.jsonl")
    session = _fake_session({})
    session.pod = None
    session.__exit__(None, None, None)
    assert not (tmp_path / "runpod_pod_costs.jsonl").exists()
