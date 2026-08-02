"""RunPod serverless client for on-demand CLIP/TTS/render bursts."""

from __future__ import annotations

from typing import Any

import httpx

from src.core.config import get_settings
from src.core.logging import get_logger

logger = get_logger(__name__)


class RunPodClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.enabled = settings.runpod_enabled
        self.api_key = settings.runpod_api_key
        self.endpoint_id = settings.runpod_endpoint_id

    async def trigger_render(self, job_payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("RunPod is disabled (RUNPOD_ENABLED=false)")
        if not self.api_key or not self.endpoint_id:
            raise RuntimeError("RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID required")
        url = f"https://api.runpod.ai/v2/{self.endpoint_id}/run"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, headers=headers, json={"input": job_payload})
            resp.raise_for_status()
            data = resp.json()
        logger.info("runpod_triggered", extra={"id": data.get("id")})
        return data

    async def status(self, request_id: str) -> dict[str, Any]:
        url = f"https://api.runpod.ai/v2/{self.endpoint_id}/status/{request_id}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
