"""Ephemeral RunPod pod lifecycle: create → wait ready → work → terminate.

Usage::

    with EphemeralPodSession(spec) as session:
        session.client...  # session.base_url, session.pod_id

GPU strategy (max_concurrent=1, TTS then stills — two separate ephemeral pods):
  Voice primary: RTX 2000 Ada Secure (CosyVoice) → A40 Secure
  Stills primary: NVIDIA A40 Secure ONLY (no Community / A5000 / 3090 walk).
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from src.runpod.client import RunPodClient, RunPodClientError
from src.runpod.cost_log import log_session_cost, utcnow_iso
from src.runpod.guards import (
    RunPodGuardError,
    assert_circuit_allows_create,
    default_max_minutes,
    preflight_docker_args,
    record_create_failure,
    record_create_success,
)

logger = logging.getLogger(__name__)

# Stills factory lock: A40 Secure primary with no fallbacks.
# Empty tuple — do not rehydrate old A5000/3090 Community walk.
DEFAULT_STILLS_GPU_FALLBACKS: tuple[tuple[str, str], ...] = ()


def is_insufficient_balance_error(exc_or_msg: Any) -> bool:
    """True when RunPod refuses create due to empty account balance.

    Must abort all tiers immediately — retries burn nothing useful and
    walk other GPUs that will also fail with the same code.
    """
    msg = str(exc_or_msg or "").lower()
    if "insufficient_balance" in msg:
        return True
    if "account balance is too low" in msg:
        return True
    if "add funds to your account" in msg:
        return True
    if "add funds" in msg and "balance" in msg:
        return True
    return False


# Public Comfy-Org Flux Dev FP8 (~17GB). No netvol (deleted 2026-08-07) —
# re-downloaded onto each pod's container disk (~5-10 min, ~$0.03 on 3090).
_FLUX_CKPT_URL = (
    "https://huggingface.co/Comfy-Org/flux1-dev/resolve/main/flux1-dev-fp8.safetensors"
)

# Shared Comfy start — ALWAYS hand off to yanwk via ``bash`` (image CMD is
# ``bash /runner-scripts/entrypoint.sh``). Never require ``-x``; never create a
# fake /root/ComfyUI tree before the entrypoint installs ComfyUI.
_COMFY_START_SCRIPT = r"""
echo "BOOTSTRAP: starting Comfy via image entrypoint"
ls -la /runner-scripts 2>/dev/null || true
if [ -f /runner-scripts/entrypoint.sh ]; then
  exec bash /runner-scripts/entrypoint.sh
fi
if [ -f /start.sh ]; then
  exec bash /start.sh
fi
PYBIN="$(command -v python3 || command -v python || true)"
echo "PYBIN=$PYBIN"
if [ -n "$PYBIN" ] && [ -f /root/ComfyUI/main.py ]; then
  exec "$PYBIN" /root/ComfyUI/main.py --listen 0.0.0.0 --port 8188
fi
if [ -n "$PYBIN" ] && [ -f /ComfyUI/main.py ]; then
  exec "$PYBIN" /ComfyUI/main.py --listen 0.0.0.0 --port 8188
fi
echo "ERROR: no Comfy entrypoint (expected /runner-scripts/entrypoint.sh)"
find / -name 'entrypoint.sh' 2>/dev/null | head
find / -name 'main.py' 2>/dev/null | head
exit 1
""".strip()


def stills_netvol_bootstrap_script(*, flux_url: str = _FLUX_CKPT_URL) -> str:
    """Bash: download Flux onto /workspace, then exec yanwk entrypoint.

    Weights stay under /workspace (not a fake /root/ComfyUI tree). A pre-start
    hook links them into ComfyUI *after* the entrypoint installs ComfyUI.
    """
    return f"""
set -euo pipefail
CKPT="${{COMFY_CKPT_NAME:-flux1-dev-fp8.safetensors}}"
VOL_DIR=/workspace/models/checkpoints
mkdir -p "$VOL_DIR" /root/user-scripts
FOUND=
for p in "$VOL_DIR/$CKPT" \\
  "/workspace/ComfyUI/models/checkpoints/$CKPT" \\
  "/workspace/models/$CKPT" "/workspace/$CKPT"; do
  if [ -s "$p" ]; then
    FOUND="$p"
    break
  fi
done
TARGET="$VOL_DIR/$CKPT"
URL="{flux_url}"
if [ -z "${{FOUND}}" ]; then
  echo "BOOTSTRAP: downloading $CKPT onto /workspace (~17GB)"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq aria2 curl ca-certificates || true
  fi
  if command -v aria2c >/dev/null 2>&1; then
    aria2c -x 16 -s 16 -k 1M -c --file-allocation=none -d "$VOL_DIR" -o "${{CKPT}}.partial" "$URL"
    mv "$VOL_DIR/${{CKPT}}.partial" "$TARGET"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$TARGET.partial" "$URL"
    mv "$TARGET.partial" "$TARGET"
  else
    curl -L --fail --retry 5 --retry-delay 2 -o "$TARGET.partial" "$URL"
    mv "$TARGET.partial" "$TARGET"
  fi
  FOUND="$TARGET"
else
  echo "FOUND_CKPT=$FOUND"
  if [ "$FOUND" != "$TARGET" ]; then
    ln -sfn "$FOUND" "$TARGET" || true
  fi
fi
ls -lh "$TARGET"
# Link into ComfyUI only AFTER yanwk entrypoint has installed it.
cat > /root/user-scripts/pre-start.sh <<PRE
#!/bin/bash
set -e
CKPT="${{COMFY_CKPT_NAME:-flux1-dev-fp8.safetensors}}"
SRC="/workspace/models/checkpoints/$CKPT"
mkdir -p /root/ComfyUI/models/checkpoints
test -s "$SRC"
ln -sfn "$SRC" "/root/ComfyUI/models/checkpoints/$CKPT"
ls -lh "/root/ComfyUI/models/checkpoints/$CKPT"
test -f /root/ComfyUI/main.py
python3 -c "import comfy_aimdo" 2>/dev/null || pip install --no-cache-dir 'comfy-aimdo>=0.4.9' || true
python3 -c "import comfy_aimdo" || {{
  echo "ERROR: comfy_aimdo missing after pip - refuse crash-loop"
  exit 1
}}
echo "PRE-START OK: Flux linked; ComfyUI + comfy_aimdo ready"
PRE
chmod +x /root/user-scripts/pre-start.sh
{_COMFY_START_SCRIPT}
""".strip()


def stills_container_bootstrap_script(*, flux_url: str = _FLUX_CKPT_URL) -> str:
    """Bash: download Flux onto container disk outside ComfyUI, then exec yanwk.

    Critical: do NOT mkdir /root/ComfyUI before entrypoint — that breaks
    yanwk's git clone / bundle copy. Stage weights under /workspace (or
    /root/models-staging), link from pre-start.sh after Comfy exists.
    """
    return f"""
set -euo pipefail
STAGE=/workspace/models/checkpoints
FALLBACK_STAGE=/root/models-staging/checkpoints
mkdir -p "$STAGE" "$FALLBACK_STAGE" /root/user-scripts
CKPT=flux1-dev-fp8.safetensors
TARGET="$STAGE/$CKPT"
URL="{flux_url}"
if [ ! -s "$TARGET" ] && [ -s "$FALLBACK_STAGE/$CKPT" ]; then
  TARGET="$FALLBACK_STAGE/$CKPT"
fi
if [ ! -s "$TARGET" ]; then
  echo "BOOTSTRAP downloading flux1-dev-fp8 onto staging disk (~17GB) — NOT under /root/ComfyUI"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq aria2 curl ca-certificates || true
  fi
  # Prefer /workspace (20GB vol may be tight for 17GB — fall back to container disk staging).
  DL_DIR="$STAGE"
  if ! mkdir -p "$DL_DIR" 2>/dev/null; then
    DL_DIR="$FALLBACK_STAGE"
    mkdir -p "$DL_DIR"
  fi
  # 17GB + headroom: if workspace is the small vol, use container-disk staging.
  FREE_KB=$(df -Pk "$DL_DIR" 2>/dev/null | awk 'NR==2{{print $4}}' || echo 0)
  if [ "${{FREE_KB:-0}}" -lt 20000000 ]; then
    echo "BOOTSTRAP: $DL_DIR low free (${{FREE_KB}}KB) — using $FALLBACK_STAGE"
    DL_DIR="$FALLBACK_STAGE"
    mkdir -p "$DL_DIR"
  fi
  TARGET="$DL_DIR/$CKPT"
  if command -v aria2c >/dev/null 2>&1; then
    aria2c -x 16 -s 16 -k 1M -c --file-allocation=none -d "$DL_DIR" -o ${{CKPT}}.partial "$URL"
    mv "$DL_DIR/${{CKPT}}.partial" "$TARGET"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$TARGET.partial" "$URL"
    mv "$TARGET.partial" "$TARGET"
  else
    curl -L --fail --retry 5 --retry-delay 2 -o "$TARGET.partial" "$URL"
    mv "$TARGET.partial" "$TARGET"
  fi
fi
ls -lh "$TARGET"
# Remember absolute path for pre-start (Comfy not installed yet).
echo "$TARGET" > /root/user-scripts/flux_ckpt_path.txt
cat > /root/user-scripts/pre-start.sh <<'PRE'
#!/bin/bash
set -e
CKPT=flux1-dev-fp8.safetensors
SRC="$(cat /root/user-scripts/flux_ckpt_path.txt)"
test -s "$SRC"
mkdir -p /root/ComfyUI/models/checkpoints
ln -sfn "$SRC" "/root/ComfyUI/models/checkpoints/$CKPT"
# Mirror under /workspace only when SRC is elsewhere (avoid ln same-file noise).
mkdir -p /workspace/models/checkpoints
if [ "$SRC" != "/workspace/models/checkpoints/$CKPT" ]; then
  ln -sfn "$SRC" "/workspace/models/checkpoints/$CKPT" || true
fi
ls -lh "/root/ComfyUI/models/checkpoints/$CKPT"
test -f /root/ComfyUI/main.py
# cu124-slim git-clones newest ComfyUI which needs comfy-aimdo; image may lack it.
python3 -c "import comfy_aimdo" 2>/dev/null || pip install --no-cache-dir 'comfy-aimdo>=0.4.9' || true
python3 -c "import comfy_aimdo" || {{
  echo "ERROR: comfy_aimdo missing after pip - refuse crash-loop"
  exit 1
}}
echo "PRE-START OK: Flux linked; ComfyUI main.py + comfy_aimdo present"
PRE
chmod +x /root/user-scripts/pre-start.sh
{_COMFY_START_SCRIPT}
""".strip()


def docker_args_from_script(script: str) -> str:
    """Wrap a multiline bash script as RunPod dockerArgs via base64.

    Nested quotes / multiline ``if`` break when RunPod re-wraps dockerArgs
    (``syntax error: unexpected end of file from 'if' command``). Encoding the
    script as base64 keeps dockerArgs a single flat line with no nested quotes.
    """
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    # base64 alphabet has no single quotes — safe inside bash -lc '...'
    return f"bash -lc 'printf %s {b64} | base64 -d | bash'"


def extract_script_from_docker_args(docker_args: str) -> str:
    """Decode the bootstrap script embedded in dockerArgs (for tests)."""
    m = re.search(r"printf %s ([A-Za-z0-9+/=]+) \| base64 -d", docker_args)
    if not m:
        raise ValueError(f"dockerArgs is not base64-wrapped: {docker_args[:120]!r}")
    return base64.b64decode(m.group(1)).decode("utf-8")


# Stable constants built via base64 (safe under RunPod dockerArgs wrapping).
STILLS_COMFY_DOCKER_ARGS = docker_args_from_script(stills_netvol_bootstrap_script())
STILLS_BOOTSTRAP_DOCKER_ARGS = docker_args_from_script(
    stills_container_bootstrap_script()
)

# After primary RTX 2000 Ada Secure (~$0.24). Skip flaky A40 Community.
DEFAULT_VOICE_GPU_FALLBACKS: tuple[tuple[str, str], ...] = (
    ("NVIDIA A40", "SECURE"),  # ~$0.44 when Ada empty
)


@dataclass(frozen=True)
class GpuCandidate:
    gpu_type_id: str
    cloud_type: str = "COMMUNITY"

    def key(self) -> tuple[str, str]:
        return (self.gpu_type_id, self.cloud_type.upper())


@dataclass
class PodSpec:
    name_prefix: str
    gpu_type_id: str
    cloud_type: str = "COMMUNITY"
    image_name: str | None = None
    template_id: str | None = None
    container_disk_gb: int = 40
    volume_gb: int | None = 20
    volume_mount_path: str = "/workspace"
    ports: list[str] = field(default_factory=lambda: ["8188/http"])
    env: dict[str, str] = field(default_factory=dict)
    data_center_ids: list[str] | None = None
    network_volume_id: str | None = None
    http_port: int = 8188
    ready_path: str = "/"
    ready_timeout_s: float = 900.0
    boot_timeout_s: float = 600.0
    # Extra (gpu_type_id, cloud_type) tried after primary on stock/create failure.
    gpu_fallbacks: list[tuple[str, str]] = field(default_factory=list)
    wait_http: bool = True
    # If a network volume is configured, do not accept a pod without it.
    # (None configured since 2026-08-07 — weights re-download per pod.)
    require_network_volume: bool = False
    # Optional container command override (e.g. download ckpt then start Comfy).
    docker_args: str | None = None
    # Hard wall-clock TTL (minutes). None → RUNPOD_POD_MAX_MINUTES / defaults.
    max_minutes: float | None = None


@dataclass
class EphemeralPodSession:
    """Context manager that always terminates the pod on exit.

    Kill-on-error: any create/boot/ready failure terminates the pod immediately
    so a crash-looping container cannot keep billing. ``__enter__`` never leaves
    a live pod behind when it raises.

    Hard TTL: a background timer terminates the pod after ``max_minutes`` even
    if the client hangs (crash-loop / stuck HTTP wait).
    """

    spec: PodSpec
    client: RunPodClient | None = None
    pod: dict[str, Any] | None = None
    base_url: str | None = None
    terminate_on_exit: bool = True
    kill_on_error: bool = True
    selected_gpu_type_id: str | None = None
    selected_cloud_type: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    # Session start (fallback for cost logging when pod payload lacks createdAt).
    created_at_utc: str = field(default_factory=utcnow_iso)
    # Override PodSpec.max_minutes / env; smoke proof uses 60 min healthy ceiling.
    max_minutes: float | None = None
    # Internal TTL state
    _ttl_deadline_monotonic: float | None = field(default=None, repr=False)
    _ttl_timer: threading.Timer | None = field(default=None, repr=False)
    _ttl_fired: bool = field(default=False, repr=False)
    _enter_recorded_failure: bool = field(default=False, repr=False)
    _preflight_done: bool = field(default=False, repr=False)

    def _resolved_max_minutes(self) -> float:
        if self.max_minutes is not None:
            # Allow sub-minute for tests; production uses 60. Floor = 1 second.
            return max(1.0 / 60.0, float(self.max_minutes))
        if self.spec.max_minutes is not None:
            return max(1.0 / 60.0, float(self.spec.max_minutes))
        prefix = (self.spec.name_prefix or "").lower()
        kind = "voice" if "voice" in prefix else "stills"
        return default_max_minutes(kind=kind)

    def _start_ttl_watchdog(self, pod_id: str) -> None:
        """Background timer — MUST kill even if client hangs."""
        self._cancel_ttl_watchdog()
        minutes = self._resolved_max_minutes()
        seconds = minutes * 60.0
        self._ttl_deadline_monotonic = time.monotonic() + seconds
        self._ttl_fired = False

        def _fire() -> None:
            self._ttl_fired = True
            logger.error(
                "HARD TTL FIRED: terminating pod %s after %.1f wall minutes "
                "(RUNPOD_POD_MAX_MINUTES / session max)",
                pod_id,
                minutes,
            )
            try:
                client = self.client or RunPodClient()
                client.terminate_pod(str(pod_id))
            except Exception as exc:  # noqa: BLE001
                logger.error("TTL terminate failed for %s: %s", pod_id, exc)

        timer = threading.Timer(seconds, _fire)
        timer.daemon = True
        timer.name = f"runpod-ttl-{pod_id}"
        self._ttl_timer = timer
        timer.start()
        logger.info(
            "Hard pod TTL armed: %.1f min for pod %s (deadline wall)",
            minutes,
            pod_id,
        )

    def _cancel_ttl_watchdog(self) -> None:
        timer = self._ttl_timer
        self._ttl_timer = None
        self._ttl_deadline_monotonic = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:  # noqa: BLE001
                pass

    def assert_ttl_ok(self) -> None:
        """Raise if hard TTL already fired or deadline passed (check-loop hook)."""
        if self._ttl_fired:
            raise RunPodClientError(
                f"hard pod TTL exceeded for {self.pod_id} — terminated"
            )
        deadline = self._ttl_deadline_monotonic
        if deadline is not None and time.monotonic() >= deadline:
            pid = self.pod_id
            self._kill_pod_now(pid, reason="ttl_check_loop")
            raise RunPodClientError(
                f"hard pod TTL exceeded for {pid} — terminated"
            )

    def __enter__(self) -> EphemeralPodSession:
        self.client = self.client or RunPodClient()
        errors: list[str] = []
        # Cost guards BEFORE any create (no money on preflight/circuit refuse).
        try:
            assert_circuit_allows_create()
            if not self._preflight_done:
                preflight_docker_args(self.spec.docker_args)
                self._preflight_done = True
        except RunPodGuardError:
            raise
        # Per GPU tier: boot retries then walk. Stills A40 Secure-only
        # (empty fallbacks) = 1 create attempt; supply-out → stop and wait
        # for next GREEN (no fallback walk, no same-tier retry).
        # INSUFFICIENT_BALANCE aborts immediately and force-arms the circuit.
        try:
            candidates = self._candidates()
            no_fallback_walk = len(candidates) <= 1
            max_boot_fails = 1 if no_fallback_walk else 2
            for idx, cand in enumerate(candidates):
                boot_fails = 0
                while boot_fails < max_boot_fails:
                    supply_out = False
                    supply_exc: RunPodClientError | None = None
                    for variant in self._create_variants(cand):
                        try:
                            return self._try_create(cand, variant, errors)
                        except RunPodClientError as exc:
                            msg = str(exc).lower()
                            if is_insufficient_balance_error(exc):
                                errors.append(
                                    f"{cand.gpu_type_id}/{cand.cloud_type}/"
                                    f"{variant.get('label')}: {exc}"
                                )
                                self._record_balance_abort(exc, cand)
                                raise RunPodClientError(
                                    "INSUFFICIENT_BALANCE — aborting all GPU tiers "
                                    "(circuit force-armed): "
                                    + " | ".join(errors)
                                ) from exc
                            if (
                                "supply_constraint" in msg
                                or "no longer any instances" in msg
                                or "no instances currently available" in msg
                                or "could not find any pods" in msg
                                or "network volume" in msg and "not found" in msg
                            ):
                                supply_out = True
                                supply_exc = exc
                                errors.append(
                                    f"{cand.gpu_type_id}/{cand.cloud_type}/"
                                    f"{variant.get('label')}: {exc}"
                                )
                                break
                            boot_fails += 1
                            logger.warning(
                                "Boot fail %s/%s for %s/%s — %s",
                                boot_fails,
                                max_boot_fails,
                                cand.gpu_type_id,
                                cand.cloud_type,
                                "retrying same tier"
                                if boot_fails < max_boot_fails
                                else (
                                    "no fallbacks — abort"
                                    if no_fallback_walk
                                    else "walking to next tier"
                                ),
                            )
                            break  # retry same GPU or walk
                    if supply_out:
                        more = idx + 1 < len(candidates)
                        if no_fallback_walk or not more:
                            logger.warning(
                                "Supply out for %s/%s — no fallbacks; "
                                "wait for next GREEN window (no retry)",
                                cand.gpu_type_id,
                                cand.cloud_type,
                            )
                            raise RunPodClientError(
                                "OUT_OF_STOCK — "
                                f"{cand.gpu_type_id}/{cand.cloud_type} unavailable; "
                                "no fallbacks; wait for next GREEN window "
                                "(do not retry): "
                                + " | ".join(errors)
                            ) from supply_exc
                        break  # next GPU only when fallbacks exist
                    if boot_fails == 0:
                        # No variants / unexpected — avoid infinite loop
                        break

            raise RunPodClientError(
                "all stills/voice GPU candidates failed: " + " | ".join(errors)
            )
        except RunPodGuardError:
            raise
        except Exception as exc:
            # Safety net: never leave a billing pod if enter fails mid-flight.
            self._kill_pod_now(self.pod_id, reason="enter_failed")
            self.pod = None
            self.base_url = None
            self._cancel_ttl_watchdog()
            if not self._enter_recorded_failure:
                self._enter_recorded_failure = True
                try:
                    record_create_failure(
                        error=str(exc),
                        pod_id=None,
                        gpu=self.selected_gpu_type_id or self.spec.gpu_type_id,
                        cloud=self.selected_cloud_type or self.spec.cloud_type,
                    )
                except Exception as circ_exc:  # noqa: BLE001
                    logger.error("circuit record_create_failure failed: %s", circ_exc)
            raise

    def _record_balance_abort(self, exc: BaseException, cand: GpuCandidate) -> None:
        """Force-arm circuit on INSUFFICIENT_BALANCE — never retry or walk tiers."""
        logger.error(
            "INSUFFICIENT_BALANCE on %s/%s — force-arming circuit, abort session",
            cand.gpu_type_id,
            cand.cloud_type,
        )
        self._enter_recorded_failure = True
        try:
            record_create_failure(
                error=str(exc),
                pod_id=None,
                gpu=cand.gpu_type_id,
                cloud=cand.cloud_type,
                force_arm=True,
            )
        except Exception as circ_exc:  # noqa: BLE001
            logger.error("circuit record_create_failure (balance) failed: %s", circ_exc)

    def _kill_pod_now(self, pod_id: str | None, *, reason: str) -> None:
        """Terminate immediately on boot/ready failure — no crash-loop billing."""
        if not self.kill_on_error or not pod_id or not self.client:
            return
        try:
            logger.warning(
                "Kill-on-error: terminating pod %s (%s)", pod_id, reason
            )
            self.client.terminate_pod(str(pod_id))
        except Exception as term_exc:  # noqa: BLE001
            logger.error(
                "Failed to terminate failed pod %s (%s): %s",
                pod_id,
                reason,
                term_exc,
            )
        finally:
            self._cancel_ttl_watchdog()

    def _create_variants(
        self, cand: GpuCandidate
    ) -> list[dict[str, Any]]:
        """Try with network volume first; on failure retry without it (volume DC
        may have no stock for this GPU — e.g. A40 Secure is often outside US-IL-1).
        """
        base_dcs = list(self.spec.data_center_ids or [])
        variants: list[dict[str, Any]] = [
            {
                "network_volume_id": self.spec.network_volume_id,
                "volume_gb": self.spec.volume_gb,
                "data_center_ids": base_dcs or None,
                "label": "with_netvol" if self.spec.network_volume_id else "default",
            }
        ]
        if self.spec.network_volume_id and not self.spec.require_network_volume:
            no_vol_dcs = base_dcs
            gpu_u = cand.gpu_type_id.upper()
            if "A40" in gpu_u:
                no_vol_dcs = ["CA-MTL-1", "EU-SE-1"]
            elif "3090" in gpu_u:
                # Often stock outside the Flux volume DC.
                no_vol_dcs = []
            variants.append(
                {
                    "network_volume_id": None,
                    "volume_gb": self.spec.volume_gb
                    if self.spec.volume_gb
                    else 20,
                    "data_center_ids": no_vol_dcs or None,
                    "label": "no_netvol",
                }
            )
        return variants

    def _try_create(
        self,
        cand: GpuCandidate,
        variant: dict[str, Any],
        errors: list[str],
    ) -> EphemeralPodSession:
        assert self.client is not None
        # Re-check before every create (another process may have tripped the circuit).
        assert_circuit_allows_create()
        name = f"{self.spec.name_prefix}-{uuid.uuid4().hex[:8]}"
        attempt: dict[str, Any] = {
            "gpu_type_id": cand.gpu_type_id,
            "cloud_type": cand.cloud_type,
            "name": name,
            "variant": variant.get("label"),
        }
        logger.info(
            "Creating ephemeral pod %s gpu=%s cloud=%s variant=%s",
            name,
            cand.gpu_type_id,
            cand.cloud_type,
            variant.get("label"),
        )
        try:
            self.pod = self.client.create_pod(
                name=name,
                gpu_type_id=cand.gpu_type_id,
                image_name=self.spec.image_name,
                template_id=self.spec.template_id,
                cloud_type=cand.cloud_type,
                container_disk_gb=self.spec.container_disk_gb,
                volume_gb=variant.get("volume_gb"),
                volume_mount_path=self.spec.volume_mount_path,
                ports=self.spec.ports,
                env=self.spec.env or None,
                data_center_ids=variant.get("data_center_ids"),
                network_volume_id=variant.get("network_volume_id"),
                docker_args=self.spec.docker_args,
            )
            pod_id = self.pod_id
            if not pod_id:
                raise RunPodClientError(f"create pod returned no id: {self.pod}")
            attempt["pod_id"] = pod_id
            # Arm hard TTL immediately after create — covers hung boot/ready waits.
            self._start_ttl_watchdog(str(pod_id))
            # Cap waits so we cannot outlive the TTL (leave ~30s for terminate).
            ttl_s = self._resolved_max_minutes() * 60.0
            boot_cap = max(60.0, ttl_s - 30.0)
            boot_timeout = min(float(self.spec.boot_timeout_s), boot_cap)
            self.pod = self.client.wait_until_running(pod_id, timeout_s=boot_timeout)
            self.assert_ttl_ok()
            self.base_url = self.client.proxy_base_url(
                self.pod, http_port=self.spec.http_port
            )
            if not self.base_url:
                raise RunPodClientError(
                    f"no HTTP proxy URL for pod {pod_id}: {self.pod}"
                )
            if self.spec.wait_http:
                # Remaining TTL budget for HTTP ready.
                remaining = boot_cap
                if self._ttl_deadline_monotonic is not None:
                    remaining = max(
                        30.0, self._ttl_deadline_monotonic - time.monotonic()
                    )
                ready_timeout = min(float(self.spec.ready_timeout_s), remaining)
                self.client.wait_http_ready(
                    self.base_url,
                    path=self.spec.ready_path,
                    timeout_s=ready_timeout,
                )
            self.assert_ttl_ok()
            self.selected_gpu_type_id = cand.gpu_type_id
            self.selected_cloud_type = cand.cloud_type
            attempt["ok"] = True
            self.attempts.append(attempt)
            try:
                record_create_success(
                    pod_id=pod_id,
                    gpu=cand.gpu_type_id,
                    cloud=cand.cloud_type,
                )
            except Exception as circ_exc:  # noqa: BLE001
                logger.error("circuit record_create_success failed: %s", circ_exc)
            logger.info(
                "Pod %s ready at %s (gpu=%s cloud=%s)",
                pod_id,
                self.base_url,
                cand.gpu_type_id,
                cand.cloud_type,
            )
            return self
        except RunPodGuardError:
            pid = attempt.get("pod_id") or self.pod_id
            self._kill_pod_now(str(pid) if pid else None, reason="guard_refuse")
            self.pod = None
            self.base_url = None
            raise
        except Exception as exc:  # noqa: BLE001
            attempt["ok"] = False
            attempt["error"] = str(exc)
            self.attempts.append(attempt)
            errors.append(
                f"{cand.gpu_type_id}/{cand.cloud_type}/{variant.get('label')}: {exc}"
            )
            logger.warning(
                "Pod create/ready failed for %s/%s (%s) — kill + next: %s",
                cand.gpu_type_id,
                cand.cloud_type,
                variant.get("label"),
                exc,
            )
            pid = attempt.get("pod_id") or self.pod_id
            self._kill_pod_now(str(pid) if pid else None, reason="ready_or_boot_error")
            self.pod = None
            self.base_url = None
            raise RunPodClientError(str(exc)) from exc

    def terminate_now(self, *, reason: str = "kill_now") -> None:
        """Immediate terminate (success-after-download or operator kill)."""
        pid = self.pod_id
        if pid and self.client:
            try:
                logger.warning("terminate_now: pod %s (%s)", pid, reason)
                self.client.terminate_pod(str(pid))
            except Exception as exc:  # noqa: BLE001
                logger.error("terminate_now failed for %s: %s", pid, exc)
            finally:
                self._cancel_ttl_watchdog()
                # Prevent double-terminate noise in __exit__; cost log still runs.
                self.pod = {**(self.pod or {}), "id": pid, "_terminated_early": True}
        else:
            self._cancel_ttl_watchdog()

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        try:
            if not self.terminate_on_exit:
                return
            pid = self.pod_id
            if not pid or not self.client:
                return
            already = bool((self.pod or {}).get("_terminated_early"))
            try:
                if already:
                    logger.info(
                        "Pod %s already terminated early — cost log only", pid
                    )
                else:
                    reason = (
                        "session_exit_error"
                        if exc_type
                        else "session_exit_success_kill"
                    )
                    logger.info("Terminating ephemeral pod %s (%s)", pid, reason)
                    self.client.terminate_pod(pid)
            except Exception as term_exc:  # noqa: BLE001
                logger.error("Failed to terminate pod %s: %s", pid, term_exc)
            finally:
                log_session_cost(self)
        finally:
            self._cancel_ttl_watchdog()

    @property
    def pod_id(self) -> str | None:
        if not self.pod:
            return None
        return self.pod.get("id") or self.pod.get("podId")

    def _candidates(self) -> list[GpuCandidate]:
        out: list[GpuCandidate] = []
        seen: set[tuple[str, str]] = set()
        primary = GpuCandidate(self.spec.gpu_type_id, self.spec.cloud_type)
        for raw_gpu, raw_cloud in [
            (primary.gpu_type_id, primary.cloud_type),
            *list(self.spec.gpu_fallbacks or []),
        ]:
            cand = GpuCandidate(str(raw_gpu).strip(), str(raw_cloud or "COMMUNITY").strip())
            if not cand.gpu_type_id:
                continue
            key = cand.key()
            if key in seen:
                continue
            seen.add(key)
            out.append(cand)
        return out


def parse_gpu_fallback_csv(raw: str) -> list[tuple[str, str]]:
    """Parse 'GPU ID:CLOUD,GPU ID:CLOUD' (cloud defaults to COMMUNITY)."""
    out: list[tuple[str, str]] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            gpu, cloud = part.rsplit(":", 1)
            out.append((gpu.strip(), (cloud or "COMMUNITY").strip().upper()))
        else:
            out.append((part, "COMMUNITY"))
    return out


def voice_pod_spec_from_settings() -> PodSpec:
    from src.services.settings import get_settings

    s = get_settings()
    vol_gb = int(s.runpod_voice_volume_gb) if s.runpod_voice_volume_gb else None
    net = s.runpod_voice_network_volume_id or None
    if net:
        vol_gb = None
    fallbacks = parse_gpu_fallback_csv(getattr(s, "runpod_voice_gpu_fallbacks", "") or "")
    if not fallbacks:
        fallbacks = list(DEFAULT_VOICE_GPU_FALLBACKS)
    return PodSpec(
        name_prefix="yt-voice",
        gpu_type_id=s.runpod_voice_gpu_type_id,
        cloud_type=s.runpod_voice_cloud_type,
        image_name=s.runpod_voice_image or None,
        template_id=s.runpod_voice_template_id or None,
        container_disk_gb=int(s.runpod_voice_container_disk_gb),
        volume_gb=vol_gb,
        volume_mount_path=s.runpod_voice_volume_mount,
        ports=[p.strip() for p in s.runpod_voice_ports.split(",") if p.strip()],
        env={"PORT": str(s.runpod_voice_http_port), "MODEL_DIR": s.cosyvoice_model_dir},
        data_center_ids=(
            [x.strip() for x in s.runpod_voice_data_centers.split(",") if x.strip()]
            or None
        ),
        network_volume_id=net,
        http_port=int(s.runpod_voice_http_port),
        ready_path=s.runpod_voice_ready_path,
        ready_timeout_s=float(s.runpod_voice_ready_timeout_s),
        boot_timeout_s=float(s.runpod_pod_boot_timeout_s),
        gpu_fallbacks=fallbacks,
        max_minutes=float(
            getattr(s, "runpod_pod_max_minutes", None)
            or default_max_minutes(kind="voice")
        ),
    )


def stills_pod_spec_from_settings() -> PodSpec:
    from src.services.settings import get_settings

    s = get_settings()
    vol_gb = int(s.runpod_stills_volume_gb) if s.runpod_stills_volume_gb else None
    net = (s.runpod_stills_network_volume_id or "").strip() or None
    # Deleted / stale volume IDs must not be sent to RunPod.
    if net in {"u00o3y8qzi", "none", "null", "deleted"}:
        net = None
    if net:
        vol_gb = None
    # Empty env = primary only. Do NOT rehydrate DEFAULT_STILLS_GPU_FALLBACKS
    # (intentionally empty under A40 Secure-only lock).
    fallbacks = parse_gpu_fallback_csv(getattr(s, "runpod_stills_gpu_fallbacks", "") or "")
    dcs = [x.strip() for x in s.runpod_stills_data_centers.split(",") if x.strip()]
    # Flux network volume lives in US-IL-1 — pin when env leaves DCs empty.
    if not dcs and net:
        dcs = ["US-IL-1"]
    # Flux download (~17GB) + image pull — never give up mid-download.
    ready_s = max(float(s.runpod_stills_ready_timeout_s), 1800.0)
    boot_s = max(float(s.runpod_pod_boot_timeout_s), 1800.0)
    disk_gb = max(80, int(s.runpod_stills_container_disk_gb or 40))
    if net:
        docker_args = STILLS_COMFY_DOCKER_ARGS
        template_id = s.runpod_stills_template_id or None
        ready_path = s.runpod_stills_ready_path or "/system_stats"
    else:
        # No netvol: bootstrap onto container disk; skip template so dockerArgs apply.
        docker_args = STILLS_BOOTSTRAP_DOCKER_ARGS
        template_id = None
        ready_path = "/"
        vol_gb = vol_gb if vol_gb and vol_gb > 0 else 20
    return PodSpec(
        name_prefix="yt-stills",
        gpu_type_id=s.runpod_stills_gpu_type_id,
        cloud_type=s.runpod_stills_cloud_type,
        image_name=s.runpod_stills_image or None,
        template_id=template_id,
        container_disk_gb=disk_gb,
        volume_gb=vol_gb,
        volume_mount_path=s.runpod_stills_volume_mount,
        ports=[p.strip() for p in s.runpod_stills_ports.split(",") if p.strip()],
        env={
            "COMFY_CKPT_NAME": s.comfy_ckpt_name,
            "CLI_ARGS": "--listen 0.0.0.0 --port 8188",
        },
        data_center_ids=dcs or None,
        network_volume_id=net,
        http_port=int(s.runpod_stills_http_port),
        ready_path=ready_path,
        ready_timeout_s=ready_s,
        boot_timeout_s=boot_s,
        gpu_fallbacks=fallbacks,
        require_network_volume=bool(net),
        docker_args=docker_args,
        max_minutes=float(
            getattr(s, "runpod_pod_max_minutes", None)
            or default_max_minutes(kind="stills")
        ),
    )
