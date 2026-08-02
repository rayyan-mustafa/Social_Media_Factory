"""Content Multiplier — fan-out helper aligned with BUSINESS_MODELS.

Optional: call after a successful master script to draft sibling formats.
Does not auto-enqueue jobs; callers decide whether to create jobs.
"""

from __future__ import annotations

import json
import re
from typing import Any

from openai import AsyncOpenAI

from src.core.config import get_settings
from src.core.logging import get_logger
from src.domain import BUSINESS_MODELS, ScriptPayload

logger = get_logger(__name__)

MULTIPLIER_PROMPT = """You are the Content Multiplier for a 9-line content factory.
You receive a master documentary script (JSON). Return ONLY valid JSON with keys
exactly matching these business models (plus vertical_shorts for social cuts):

{
  "YouTube_Shorts": {"title": "...", "description": "...", "scenes": [{"index": 1, "text": "...", "visual_query": "..."}]},
  "Podcast_Audio": {"title": "...", "description": "...", "content": "full conversational narration"},
  "SEO_Blogs": {"title": "...", "description": "...", "content": "markdown blog with H1/H2"},
  "Web_Series": {"title": "...", "description": "...", "scenes": [{"index": 1, "text": "...", "visual_query": "..."}]},
  "Radio_FM": {"title": "...", "description": "...", "content": "2-minute energetic FM read"},
  "EBooks_KDP": {"title": "...", "description": "...", "content": "chapter outline + body markdown"},
  "Audiobooks_ACX": {"title": "...", "description": "...", "content": "ACX-style narration script"},
  "Sleep_Stories": {"title": "...", "description": "...", "scenes": [{"index": 1, "text": "...", "visual_query": "..."}]},
  "Online_Courses_Teachable": {"title": "...", "description": "...", "scenes": [{"index": 1, "text": "...", "visual_query": "..."}]},
  "vertical_shorts": [
    {"platform": "tiktok", "script": "..."},
    {"platform": "reels", "script": "..."},
    {"platform": "youtube_shorts", "script": "..."}
  ]
}
"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise
        return json.loads(match.group(0))


class ContentMultiplier:
    def __init__(self) -> None:
        settings = get_settings()
        self._model = settings.llm_model
        self._client = AsyncOpenAI(
            api_key=settings.wavespeed_api_key or "missing",
            base_url=settings.llm_base_url,
        )

    async def multiply_script(self, master_script: ScriptPayload) -> dict[str, Any]:
        """Draft sibling formats for all factory business models."""
        logger.info("multiplier_start", extra={"title": master_script.title})
        master_text = " ".join(scene.text for scene in master_script.scenes)

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": MULTIPLIER_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Master Script Title: {master_script.title}\n"
                            f"Content: {master_text}\n"
                            f"Required models: {', '.join(BUSINESS_MODELS)}"
                        ),
                    },
                ],
                temperature=0.7,
            )
            raw = response.choices[0].message.content or "{}"
            payload = _extract_json(raw)
            logger.info("multiplier_ok", extra={"keys": list(payload.keys())})
            return payload
        except Exception:
            logger.exception("multiplier_failed")
            raise
