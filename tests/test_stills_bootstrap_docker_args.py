"""Offline syntax checks for stills RunPod dockerArgs (no pods, no downloads)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.runpod.lifecycle import (
    STILLS_BOOTSTRAP_DOCKER_ARGS,
    STILLS_COMFY_DOCKER_ARGS,
    EphemeralPodSession,
    PodSpec,
    docker_args_from_script,
    extract_script_from_docker_args,
    stills_container_bootstrap_script,
    stills_netvol_bootstrap_script,
    stills_pod_spec_from_settings,
)


def _bash_n(script: str) -> None:
    """Run ``bash -n`` syntax check; fail with stderr on parse errors."""
    proc = subprocess.run(
        ["bash", "-n"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"bash -n failed (rc={proc.returncode}):\n{proc.stderr}\n--- script ---\n{script}"
    )


def test_docker_args_is_single_line_base64_wrapper():
    for args in (STILLS_BOOTSTRAP_DOCKER_ARGS, STILLS_COMFY_DOCKER_ARGS):
        assert "\n" not in args
        assert args.startswith("bash -lc 'printf %s ")
        assert "| base64 -d | bash'" in args
        # No nested multiline if in the wrapper itself
        assert "if [" not in args


def test_bootstrap_docker_args_bash_n_wrapper():
    """The dockerArgs string itself must parse under bash -n."""
    _bash_n(STILLS_BOOTSTRAP_DOCKER_ARGS)
    _bash_n(STILLS_COMFY_DOCKER_ARGS)


def test_bootstrap_script_decodes_and_bash_n():
    """Decode embedded base64 and syntax-check the real bootstrap script."""
    for args, builder in (
        (STILLS_BOOTSTRAP_DOCKER_ARGS, stills_container_bootstrap_script),
        (STILLS_COMFY_DOCKER_ARGS, stills_netvol_bootstrap_script),
    ):
        script = extract_script_from_docker_args(args)
        assert script == builder()
        assert "exec bash /runner-scripts/entrypoint.sh" in script
        assert "[ -x /runner-scripts/entrypoint.sh ]" not in script
        assert "flux1-dev-fp8" in script
        # Must not stage a fake ComfyUI tree before yanwk entrypoint installs it.
        if builder is stills_container_bootstrap_script:
            assert "/root/models-staging" in script or "STAGE=/workspace" in script
            assert "NOT under /root/ComfyUI" in script
            assert "mkdir -p \"$CKPT_DIR\"" not in script  # old broken pattern
            assert "comfy-aimdo" in script
        _bash_n(script)


def test_docker_args_from_script_roundtrip():
    raw = stills_container_bootstrap_script()
    wrapped = docker_args_from_script(raw)
    assert extract_script_from_docker_args(wrapped) == raw
    _bash_n(wrapped)
    _bash_n(raw)


def test_empty_network_volume_skips_attach(monkeypatch):
    """Empty RUNPOD_STILLS_NETWORK_VOLUME_ID → no volume attach + bootstrap args."""
    from src.services import settings as settings_mod

    settings_mod.get_settings.cache_clear()

    class FakeSettings:
        runpod_stills_volume_gb = 20
        runpod_stills_network_volume_id = ""  # deleted / empty
        runpod_stills_gpu_fallbacks = ""
        runpod_stills_data_centers = ""
        runpod_stills_ready_timeout_s = 1200
        runpod_pod_boot_timeout_s = 600
        runpod_stills_container_disk_gb = 80
        runpod_stills_template_id = "0hlycynxue"
        runpod_stills_ready_path = "/system_stats"
        runpod_stills_gpu_type_id = "NVIDIA RTX A5000"
        runpod_stills_cloud_type = "COMMUNITY"
        runpod_stills_image = "yanwk/comfyui-boot:cu124-slim"
        runpod_stills_volume_mount = "/workspace"
        runpod_stills_ports = "8188/http"
        runpod_stills_http_port = 8188
        comfy_ckpt_name = "flux1-dev-fp8.safetensors"
        runpod_pod_max_minutes = 60.0

    monkeypatch.setattr(settings_mod, "get_settings", lambda: FakeSettings())
    spec = stills_pod_spec_from_settings()
    assert spec.network_volume_id is None
    assert spec.require_network_volume is False
    assert spec.template_id is None  # so dockerArgs apply
    assert spec.docker_args == STILLS_BOOTSTRAP_DOCKER_ARGS
    assert spec.container_disk_gb >= 80
    _bash_n(spec.docker_args)
    _bash_n(extract_script_from_docker_args(spec.docker_args))


class _FakeClient:
    def __init__(self):
        self.created = 0
        self.terminated: list[str] = []
        self._fail_at: str = "ready"

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


def test_kill_on_error_terminates_on_ready_failure(tmp_path, monkeypatch):
    """Ready failure must terminate immediately — no crash-loop billing."""
    import src.runpod.guards as g
    import src.runpod.lifecycle as life

    circuit = tmp_path / "circuit.json"
    monkeypatch.setattr(g, "CIRCUIT_PATH", circuit)
    monkeypatch.setattr(g, "OPS_DIR", tmp_path)
    monkeypatch.setattr(
        life,
        "assert_circuit_allows_create",
        lambda **k: g.assert_circuit_allows_create(path=circuit),
    )
    monkeypatch.setattr(
        life,
        "record_create_failure",
        lambda **k: g.record_create_failure(path=circuit, **{kk: vv for kk, vv in k.items() if kk != "path"}),
    )
    monkeypatch.setattr(life, "record_create_success", lambda **k: {})
    monkeypatch.setattr(life, "log_session_cost", lambda *a, **k: None)

    client = _FakeClient()
    client._fail_at = "ready"
    spec = PodSpec(
        name_prefix="yt-stills",
        gpu_type_id="NVIDIA GeForce RTX 3090",
        cloud_type="COMMUNITY",
        image_name="yanwk/comfyui-boot:cu124-slim",
        docker_args=STILLS_BOOTSTRAP_DOCKER_ARGS,
        gpu_fallbacks=[],
        ready_timeout_s=1.0,
        boot_timeout_s=1.0,
    )
    sess = EphemeralPodSession(spec, client=client, kill_on_error=True)
    with pytest.raises(Exception):
        sess.__enter__()
    assert client.terminated, "pod must be killed on ready failure"
    assert all(t.startswith("pod") for t in client.terminated)
    assert sess.pod is None


def test_kill_on_error_terminates_on_boot_failure(tmp_path, monkeypatch):
    import src.runpod.guards as g
    import src.runpod.lifecycle as life

    circuit = tmp_path / "circuit.json"
    monkeypatch.setattr(g, "CIRCUIT_PATH", circuit)
    monkeypatch.setattr(g, "OPS_DIR", tmp_path)
    monkeypatch.setattr(
        life,
        "assert_circuit_allows_create",
        lambda **k: g.assert_circuit_allows_create(path=circuit),
    )
    monkeypatch.setattr(
        life,
        "record_create_failure",
        lambda **k: g.record_create_failure(
            path=circuit,
            **{kk: vv for kk, vv in k.items() if kk != "path"},
        ),
    )
    monkeypatch.setattr(life, "record_create_success", lambda **k: {})
    monkeypatch.setattr(life, "log_session_cost", lambda *a, **k: None)

    client = _FakeClient()
    client._fail_at = "boot"
    spec = PodSpec(
        name_prefix="yt-stills",
        gpu_type_id="NVIDIA GeForce RTX 3090",
        cloud_type="COMMUNITY",
        image_name="yanwk/comfyui-boot:cu124-slim",
        docker_args=STILLS_BOOTSTRAP_DOCKER_ARGS,
        gpu_fallbacks=[],
        ready_timeout_s=1.0,
        boot_timeout_s=1.0,
    )
    sess = EphemeralPodSession(spec, client=client, kill_on_error=True)
    with pytest.raises(Exception):
        sess.__enter__()
    assert client.terminated, "pod must be killed on first boot error"
    assert sess.pod is None


def test_wrapper_survives_runpod_style_requote(tmp_path: Path):
    """Simulate RunPod wrapping dockerArgs; bash -n must still pass."""
    # Some RunPod paths put dockerArgs inside an outer bash -c "..."
    outer = f'bash -c "{STILLS_BOOTSTRAP_DOCKER_ARGS}"'
    # That form breaks if dockerArgs contains raw double-quotes — ours must not.
    assert '"' not in STILLS_BOOTSTRAP_DOCKER_ARGS
    _bash_n(outer)
    script_path = tmp_path / "docker_args.sh"
    script_path.write_text(STILLS_BOOTSTRAP_DOCKER_ARGS + "\n", encoding="utf-8")
    proc = subprocess.run(
        ["bash", "-n", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
