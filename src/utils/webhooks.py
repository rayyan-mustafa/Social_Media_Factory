"""Outbound job lifecycle webhooks (compliance-friendly event notify)."""

from __future__ import annotations

from typing import Any

import httpx

from src.core.config import get_settings
from src.core.logging import get_logger

logger = get_logger(__name__)


async def notify_job_event(event: str, payload: dict[str, Any]) -> None:
    settings = get_settings()
    url = settings.job_webhook_url.strip()
    if not url:
        return
    body = {"event": event, **payload}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json=body)
    except Exception:
        logger.exception("job_webhook_failed", extra={"event": event})
