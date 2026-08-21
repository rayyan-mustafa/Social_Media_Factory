"""Optional Discord pager alerts (terminal failures only)."""

from __future__ import annotations

import httpx

from src.core.config import get_settings
from src.core.logging import get_logger

logger = get_logger(__name__)


async def send_discord_alert(message: str) -> None:
    settings = get_settings()
    url = settings.discord_webhook_url
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json={"content": f"**Pipeline alert**\n```{message[:1800]}```"})
    except Exception:
        logger.exception("discord_alert_failed")
