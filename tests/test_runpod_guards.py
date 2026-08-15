"""Unit tests for RunPod cost-safety guards (circuit, preflight, TTL, smoke gate)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from src.runpod import guards
from src.runpod.guards import (
    RunPodGuardError,
    assert_circuit_allows_create,
    assert_stills_smoke_gate_for_farm,
    bash_n_check,
    default_max_minutes,
    farm_may_use_runpod_pod_stills,
    load_circuit,
    preflight_docker_args,
    record_create_failure,
    record_create_success,
    stills_smoke_ok,
    write_smoke_ok,
)
from src.runpod.lifecycle import (
    STILLS_BOOTSTRAP_DOCKER_ARGS,
    EphemeralPodSession,
    PodSpec,
    docker_args_from_script,
)


@pytest.fixture()
def ops_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    circuit = tmp_path / "runpod_circuit.json"
    smoke = tmp_path / "runpod_stills_smoke_ok.json"
    monkeypatch.setattr(guards, "CIRCUIT_PATH", circuit)
    monkeypatch.setattr(guards, "SMOKE_OK_PATH", smoke)
    monkeypatch.setattr(guards, "OPS_DIR", tmp_path)
    return {"circuit": circuit, "smoke": smoke, "ops": tmp_path}


def test_default_max_minutes_stills_is_60(monkeypatch):
    monkeypatch.delenv("RUNPOD_POD_MAX_MINUTES", raising=False)
    assert default_max_minutes(kind="stills") == 60.0
    assert default_max_minutes(kind="smoke") == 60.0


def test_default_max_minutes_env_override(monkeypatch):
    monkeypatch.setenv("RUNPOD_POD_MAX_MINUTES", "45")
    assert default_max_minutes(kind="stills") == 45.0


def test_preflight_accepts_valid_bootstrap():
    preflight_docker_args(STILLS_BOOTSTRAP_DOCKER_ARGS)  # must not raise


def test_preflight_refuses_bad_syntax():
    bad = docker_args_from_script("if true; then\necho oops\n")  # missing fi
    with pytest.raises(RunPodGuardError, match="PREFLIGHT REFUSED"):
        preflight_docker_args(bad)


def test_bash_n_check_ok():
    ok, err = bash_n_check("echo hi\n")
    assert ok and err == ""


def test_circuit_force_arm_on_balance(ops_paths):
    data = record_create_failure(
        error="INSUFFICIENT_BALANCE: account balance is too low",
        path=ops_paths["circuit"],
        force_arm=True,
    )
    assert data["armed"] is True
    with pytest.raises(RunPodGuardError, match="CIRCUIT BREAKER ARMED"):
        assert_circuit_allows_create(path=ops_paths["circuit"])


def test_circuit_auto_unarm_after_window(ops_paths, monkeypatch):
    from datetime import timedelta

    from src.runpod.guards import utcnow

    record_create_failure(error="a", path=ops_paths["circuit"], force_arm=True)
    assert load_circuit(path=ops_paths["circuit"])["armed"] is True
    # Age the failure past the window.
    data = load_circuit(path=ops_paths["circuit"])
    old = (utcnow() - timedelta(hours=guards.CIRCUIT_WINDOW_HOURS + 1)).isoformat()
    data["failures"] = [{"at": old, "error": "stale", "force_arm": True}]
    guards.save_circuit(data, path=ops_paths["circuit"])
    # Prune empty → auto-unarm
    assert_circuit_allows_create(path=ops_paths["circuit"])
    assert load_circuit(path=ops_paths["circuit"])["armed"] is False


def test_insufficient_balance_aborts_without_retry(monkeypatch, ops_paths):
    """INSUFFICIENT_BALANCE must not retry same tier or walk fallbacks."""
    from src.runpod.client import RunPodClientError
    from src.runpod.lifecycle import is_insufficient_balance_error

    assert is_insufficient_balance_error(
        "GraphQL create pod failed: [{'extensions': {'code': 'INSUFFICIENT_BALANCE'}}]"
    )
    assert is_insufficient_balance_error(
        "Your account balance is too low to rent a pod. Please add funds to your account."
    )
    assert not is_insufficient_balance_error("SUPPLY_CONSTRAINT")

    monkeypatch.setattr(
        "src.runpod.lifecycle.assert_circuit_allows_create", lambda **k: None
    )
    monkeypatch.setattr(
        "src.runpod.lifecycle.preflight_docker_args", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "src.runpod.lifecycle.log_session_cost", lambda *a, **k: None
    )
    recorded: list[dict] = []

    def _rec(**kwargs):
        recorded.append(kwargs)
        return guards.record_create_failure(
            path=ops_paths["circuit"],
            **{k: v for k, v in kwargs.items() if k != "path"},
        )

    monkeypatch.setattr("src.runpod.lifecycle.record_create_failure", _rec)

    class BalanceClient:
        def __init__(self):
            self.created = 0
            self.terminated: list[str] = []

        def create_pod(self, **kwargs):  # noqa: ANN003
            self.created += 1
            raise RunPodClientError(
                "GraphQL create pod failed: [{'message': 'Your account balance "
                "is too low to rent a pod. Please add funds to your account.', "
                "'extensions': {'code': 'INSUFFICIENT_BALANCE'}}]"
            )

        def terminate_pod(self, pod_id: str) -> None:
            self.terminated.append(pod_id)

    client = BalanceClient()
    spec = PodSpec(
        name_prefix="yt-stills",
        gpu_type_id="NVIDIA A40",
        cloud_type="SECURE",
        image_name="x",
        docker_args=STILLS_BOOTSTRAP_DOCKER_ARGS,
        gpu_fallbacks=[
            ("NVIDIA RTX A5000", "SECURE"),
            ("NVIDIA GeForce RTX 3090", "COMMUNITY"),
        ],
        max_minutes=60.0,
    )
    with pytest.raises(RunPodClientError, match="INSUFFICIENT_BALANCE"):
        EphemeralPodSession(spec, client=client).__enter__()
    # One create only — no same-tier retry, no fallback walk.
    assert client.created == 1
    assert len(recorded) == 1
    assert recorded[0].get("force_arm") is True
    assert load_circuit(path=ops_paths["circuit"])["armed"] is True


def test_supply_out_no_fallbacks_waits_for_green(monkeypatch, ops_paths):
    """A40 Secure-only: supply-out stops immediately — no retry / no GPU walk."""
    from src.runpod.client import RunPodClientError

    monkeypatch.setattr(
        "src.runpod.lifecycle.assert_circuit_allows_create", lambda **k: None
    )
    monkeypatch.setattr(
        "src.runpod.lifecycle.preflight_docker_args", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "src.runpod.lifecycle.log_session_cost", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "src.runpod.lifecycle.record_create_failure",
        lambda **k: guards.record_create_failure(path=ops_paths["circuit"], **{
            kk: vv for kk, vv in k.items() if kk != "path"
        }),
    )

    class SupplyClient:
        def __init__(self):
            self.created = 0

        def create_pod(self, **kwargs):  # noqa: ANN003
            self.created += 1
            raise RunPodClientError(
                "GraphQL create pod failed: SUPPLY_CONSTRAINT — "
                "there are no longer any instances available"
            )

        def terminate_pod(self, pod_id: str) -> None:
            return None

    client = SupplyClient()
    spec = PodSpec(
        name_prefix="yt-stills",
        gpu_type_id="NVIDIA A40",
        cloud_type="SECURE",
        image_name="x",
        docker_args=STILLS_BOOTSTRAP_DOCKER_ARGS,
        gpu_fallbacks=[],
        max_minutes=60.0,
    )
    with pytest.raises(RunPodClientError, match="OUT_OF_STOCK"):
        EphemeralPodSession(spec, client=client).__enter__()
    assert client.created == 1


def test_stills_spec_a40_secure_only(monkeypatch):
    from src.services import settings as settings_mod
    from src.runpod.lifecycle import stills_pod_spec_from_settings

    settings_mod.get_settings.cache_clear()

    class FakeSettings:
        runpod_stills_volume_gb = 20
        runpod_stills_network_volume_id = ""
        runpod_stills_gpu_fallbacks = ""
        runpod_stills_data_centers = ""
        runpod_stills_ready_timeout_s = 3600
        runpod_pod_boot_timeout_s = 1800
        runpod_stills_container_disk_gb = 80
        runpod_stills_template_id = "0hlycynxue"
        runpod_stills_ready_path = "/"
        runpod_stills_gpu_type_id = "NVIDIA A40"
        runpod_stills_cloud_type = "SECURE"
        runpod_stills_image = "yanwk/comfyui-boot:cu126-slim"
        runpod_stills_volume_mount = "/workspace"
        runpod_stills_ports = "8188/http"
        runpod_stills_http_port = 8188
        comfy_ckpt_name = "flux1-dev-fp8.safetensors"
        runpod_pod_max_minutes = 60.0

    monkeypatch.setattr(settings_mod, "get_settings", lambda: FakeSettings())
    spec = stills_pod_spec_from_settings()
    assert spec.gpu_type_id == "NVIDIA A40"
    assert spec.cloud_type.upper() == "SECURE"
    assert spec.gpu_fallbacks == []
    # Candidates must be sole A40 Secure.
    sess = EphemeralPodSession(spec, client=_FakeClient())
    cands = sess._candidates()
    assert len(cands) == 1
    assert cands[0].gpu_type_id == "NVIDIA A40"
    assert cands[0].cloud_type.upper() == "SECURE"


def test_circuit_trips_after_two_failures(ops_paths):
    assert_circuit_allows_create(path=ops_paths["circuit"])
    record_create_failure(error="boot fail 1", path=ops_paths["circuit"])
    data = load_circuit(path=ops_paths["circuit"])
    assert data["armed"] is False
    record_create_failure(error="boot fail 2", path=ops_paths["circuit"])
    data = load_circuit(path=ops_paths["circuit"])
    assert data["armed"] is True
    with pytest.raises(RunPodGuardError, match="CIRCUIT BREAKER ARMED"):
        assert_circuit_allows_create(path=ops_paths["circuit"])


def test_circuit_cleared_by_success(ops_paths):
    record_create_failure(error="a", path=ops_paths["circuit"])
    record_create_failure(error="b", path=ops_paths["circuit"])
    assert load_circuit(path=ops_paths["circuit"])["armed"] is True
    record_create_success(pod_id="podX", path=ops_paths["circuit"])
    assert load_circuit(path=ops_paths["circuit"])["armed"] is False
    assert_circuit_allows_create(path=ops_paths["circuit"])


def test_circuit_reset_env(ops_paths, monkeypatch):
    record_create_failure(error="a", path=ops_paths["circuit"])
    record_create_failure(error="b", path=ops_paths["circuit"])
    monkeypatch.setenv("RUNPOD_CIRCUIT_RESET", "1")
    assert_circuit_allows_create(path=ops_paths["circuit"])
    assert load_circuit(path=ops_paths["circuit"])["armed"] is False
    assert "RUNPOD_CIRCUIT_RESET" not in __import__("os").environ


def test_smoke_gate_blocks_farm(ops_paths, monkeypatch):
    monkeypatch.setattr(guards, "image_backend_is_runpod_pod", lambda: True)
    ok, msg = farm_may_use_runpod_pod_stills(smoke_path=ops_paths["smoke"])
    assert ok is False
    assert "smoke_ok" in msg or "blocked" in msg.lower()
    with pytest.raises(RunPodGuardError, match="SMOKE GATE"):
        assert_stills_smoke_gate_for_farm(smoke_path=ops_paths["smoke"], force_check=True)


def test_smoke_gate_opens_after_ok(ops_paths, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(guards, "image_backend_is_runpod_pod", lambda: True)
    monkeypatch.setattr(guards, "ROOT", tmp_path)
    a = tmp_path / "output" / "runpod_smoke_stills" / "smoke_000.jpg"
    b = tmp_path / "output" / "runpod_smoke_stills" / "smoke_001.jpg"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_bytes(b"JPEGFAKE" + b"\0" * 200)
    b.write_bytes(b"JPEGFAKE" + b"\0" * 200)
    write_smoke_ok(
        stills_count=2,
        pod_id="p1",
        gpu="NVIDIA RTX A5000",
        cloud="COMMUNITY",
        paths=[str(a), str(b)],
        path=ops_paths["smoke"],
    )
    assert stills_smoke_ok(path=ops_paths["smoke"]) is True
    assert_stills_smoke_gate_for_farm(smoke_path=ops_paths["smoke"], force_check=True)


def test_write_smoke_ok_refuses_non_vps_paths(ops_paths, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(guards, "ROOT", tmp_path)
    with pytest.raises(RunPodGuardError, match="VPS path required|Still missing"):
        write_smoke_ok(
            stills_count=1,
            paths=["/tmp/not_in_workspace.jpg"],
            path=ops_paths["smoke"],
        )



class _FakeClient:
    def __init__(self):
        self.created = 0
        self.terminated: list[str] = []
        self._fail_at: str = ""

    def create_pod(self, **kwargs):  # noqa: ANN003
        self.created += 1
        return {"id": f"pod{self.created}", "desiredStatus": "RUNNING"}

    def wait_until_running(self, pod_id, timeout_s=600.0):  # noqa: ANN001
        if self._fail_at == "boot":
            raise RuntimeError(f"boot failed for {pod_id}")
        return {"id": pod_id, "desiredStatus": "RUNNING", "runtime": {"ports": []}}

    def proxy_base_url(self, pod, http_port=8188):  # noqa: ANN001
        return f"https://{pod['id']}-{http_port}.proxy.runpod.net"

    def wait_http_ready(self, base_url, **kwargs):  # noqa: ANN003
        if self._fail_at == "ready":
            raise RuntimeError(f"HTTP not ready at {base_url}")

    def terminate_pod(self, pod_id: str) -> None:
        self.terminated.append(pod_id)


def test_ttl_watchdog_fires(monkeypatch, ops_paths, tmp_path):
    """Background TTL must terminate even without client cooperation."""
    monkeypatch.setattr(
        "src.runpod.lifecycle.assert_circuit_allows_create", lambda **k: None
    )
    monkeypatch.setattr(
        "src.runpod.lifecycle.preflight_docker_args", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "src.runpod.lifecycle.record_create_success", lambda **k: {}
    )
    monkeypatch.setattr(
        "src.runpod.lifecycle.log_session_cost", lambda *a, **k: None
    )

    client = _FakeClient()
    spec = PodSpec(
        name_prefix="yt-stills",
        gpu_type_id="NVIDIA RTX A5000",
        cloud_type="COMMUNITY",
        image_name="yanwk/comfyui-boot:cu124-slim",
        docker_args=STILLS_BOOTSTRAP_DOCKER_ARGS,
        gpu_fallbacks=[],
        ready_timeout_s=30.0,
        boot_timeout_s=30.0,
        max_minutes=0.02,  # ~1.2 seconds
    )
    sess = EphemeralPodSession(spec, client=client, max_minutes=0.02)
    sess.__enter__()
    assert sess.pod_id
    # Wait for TTL timer
    deadline = time.time() + 5.0
    while time.time() < deadline and not client.terminated:
        time.sleep(0.05)
    assert client.terminated, "TTL watchdog must terminate pod"
    sess.__exit__(None, None, None)


def test_terminate_now_on_success(monkeypatch, ops_paths):
    monkeypatch.setattr(
        "src.runpod.lifecycle.assert_circuit_allows_create", lambda **k: None
    )
    monkeypatch.setattr(
        "src.runpod.lifecycle.preflight_docker_args", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "src.runpod.lifecycle.record_create_success", lambda **k: {}
    )
    monkeypatch.setattr(
        "src.runpod.lifecycle.log_session_cost", lambda *a, **k: None
    )
    client = _FakeClient()
    spec = PodSpec(
        name_prefix="yt-stills",
        gpu_type_id="NVIDIA RTX A5000",
        cloud_type="COMMUNITY",
        image_name="x",
        docker_args=STILLS_BOOTSTRAP_DOCKER_ARGS,
        gpu_fallbacks=[],
        max_minutes=60.0,
    )
    with EphemeralPodSession(spec, client=client, max_minutes=60.0) as sess:
        sess.terminate_now(reason="stills_on_local_disk")
        assert client.terminated == [sess.pod_id]
    # __exit__ must not double-fail; terminate list may grow by 0
    assert client.terminated[0].startswith("pod")


def test_session_refuses_when_circuit_armed(ops_paths, monkeypatch):
    record_create_failure(error="a", path=ops_paths["circuit"])
    record_create_failure(error="b", path=ops_paths["circuit"])
    monkeypatch.setattr(
        "src.runpod.lifecycle.assert_circuit_allows_create",
        lambda **k: assert_circuit_allows_create(path=ops_paths["circuit"]),
    )
    client = _FakeClient()
    spec = PodSpec(
        name_prefix="yt-stills",
        gpu_type_id="NVIDIA RTX A5000",
        cloud_type="COMMUNITY",
        image_name="x",
        docker_args=STILLS_BOOTSTRAP_DOCKER_ARGS,
        gpu_fallbacks=[],
    )
    with pytest.raises(RunPodGuardError):
        EphemeralPodSession(spec, client=client).__enter__()
    assert client.created == 0
