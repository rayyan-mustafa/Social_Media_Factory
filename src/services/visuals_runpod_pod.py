"""Ephemeral A40 Secure Comfy/Flux stills session for VisualModule."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.runpod.capacity import (
    CapacityDeferError,
    apply_secure_only_to_spec,
    assert_farm_capacity_green,
)
from src.runpod.comfy_pod import ComfyPodClient
from src.runpod.lifecycle import EphemeralPodSession, stills_pod_spec_from_settings

logger = logging.getLogger(__name__)


class EphemeralStillsSession:
    def __init__(self, *, skip_capacity_probe: bool = False):
        self._session: EphemeralPodSession | None = None
        self.comfy: ComfyPodClient | None = None
        self.skip_capacity_probe = skip_capacity_probe
        self.capacity_result: Any = None

    def __enter__(self) -> EphemeralStillsSession:
        # Same GREEN-only gate as farm spawn / capacity watchdog — never create
        # on RED/YELLOW; park happens upstream via CapacityDeferError → capacity_out.
        try:
            self.capacity_result = assert_farm_capacity_green(
                skip=self.skip_capacity_probe
            )
        except CapacityDeferError:
            raise
        spec = stills_pod_spec_from_settings()
        # A40 Secure-only lock: never invent fallbacks. Mixed factories may still
        # rewrite to Secure-only on proceed_secure_only; empty fallbacks stay empty.
        if (
            self.capacity_result is not None
            and getattr(self.capacity_result, "decision", None) == "proceed_secure_only"
        ):
            apply_secure_only_to_spec(spec)
            logger.warning(
                "capacity YELLOW → Secure-only GPU chain: %s + %s",
                spec.gpu_type_id,
                spec.gpu_fallbacks,
            )
        # Hard policy: empty fallbacks + out-of-stock = wait for GREEN (no create retries).
        if not list(spec.gpu_fallbacks or []):
            logger.info(
                "stills pod candidates: primary=%s/%s fallbacks=[] "
                "(no walk — RED/out-of-stock waits for next GREEN)",
                spec.gpu_type_id,
                spec.cloud_type,
            )
        else:
            logger.info(
                "stills pod candidates: primary=%s/%s fallbacks=%s",
                spec.gpu_type_id,
                spec.cloud_type,
                spec.gpu_fallbacks,
            )
        self._session = EphemeralPodSession(spec)
        self._session.__enter__()
        assert self._session.base_url
        self.comfy = ComfyPodClient(self._session.base_url)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        if self._session:
            self._session.__exit__(exc_type, exc, tb)

    def generate_to_path(self, workflow: dict[str, Any], out_path: Path | str) -> Path:
        if not self.comfy:
            raise RuntimeError("stills session not started")
        return self.comfy.generate_to_path(workflow, out_path)

    @property
    def pod_id(self) -> str | None:
        return self._session.pod_id if self._session else None
