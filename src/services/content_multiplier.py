"""Content Multiplier Strategy (Phase 2).

Slices a long-form master script into multiple formats:
- Vertical Shorts/Reels/TikTok
- Audio-only Podcast RSS feeds
- SEO-optimized Blogs (WordPress)
- Episodic Web Series (ebseries) scripts
- Radio FM broadcast scripts
- Newsletter Funnel (Monetization)
- Viral X/Twitter Thread
- Dynamic Sponsorship Injection
"""

import json
import re
from typing import Dict, Any

from openai import AsyncOpenAI
from src.core.config import get_settings
from src.core.logging import get_logger
from src.domain import ScriptPayload

logger = get_logger(__name__)

MULTIPLIER_PROMPT = """You are an elite Content Multiplier Engine. 
You will receive a master documentary script (JSON). You must repackage it into a single JSON object containing multiple specific formats tailored for high-traffic and high-monetization.

Return ONLY valid JSON matching this exact schema:
{
  "vertical_shorts": [
    {"platform": "tiktok", "script": "High-retention 60s script with a highly viral, demand-driven hook."},
    {"platform": "reels", "script": "60s script with a highly viral, demand-driven hook."},
    {"platform": "youtube_shorts", "script": "60s punchy script with a highly viral, demand-driven hook."}
  ],
  "podcast_audio": "Full conversational audio script, no visual cues.",
  "seo_blog": "HTML formatted blog post with H1, H2, and SEO keywords.",
  "ebseries": "A multi-part web series breakdown (e.g. Episode 1, 2, 3 summaries ready for TikTok Series or YouTube Playlists).",
  "radio_fm": "A snappy, energetic 2-minute FM radio broadcast script.",
  "ebook_kdp": "A chapter-by-chapter outline and formatted text suitable for Amazon KDP.",
  "audiobook_acx": "A script formatted specifically for audiobook narration (ACX standards).",
  "sleep_story": "A calm, slow-paced, deeply descriptive version of the script designed for sleep channels.",
  "newsletter_funnel": "A high-converting email newsletter with a cliffhanger to build an owned audience.",
  "twitter_thread": ["Tweet 1/10...", "Tweet 2/10...", "..."],
  "dynamic_sponsorship": "A 30-second native ad-read injected contextually based on the topic."
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
    def __init__(self):
        settings = get_settings()
        self._model = settings.llm_model
        # Use AsyncOpenAI to avoid blocking the main event loop
        self._client = AsyncOpenAI(
            api_key=settings.llm_api_key or "missing",
            base_url=settings.llm_base_url,
        )

    async def multiply_script(self, master_script: ScriptPayload) -> Dict[str, Any]:
        """Slices the master script into all multiplier formats."""
        logger.info("multiplier_start", extra={"title": master_script.title})
        
        # Combine scenes to feed to the LLM
        master_text = " ".join([scene.text for scene in master_script.scenes])
        
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": MULTIPLIER_PROMPT},
                    {
                        "role": "user",
                        "content": f"Master Script Title: {master_script.title}\nContent: {master_text}\nMultiply this content."
                    }
                ],
                temperature=0.7,
            )
            
            content = response.choices[0].message.content or ""
            data = _extract_json(content)
            
            logger.info("multiplier_success", extra={
                "formats_generated": list(data.keys())
            })
            return data
            
        except Exception as e:
            logger.error("multiplier_failed", extra={"error": str(e)})
            return {}

if __name__ == "__main__":
    import asyncio
    from src.domain import SceneScript

    async def test():
        # Dummy master script
        scenes = [
            SceneScript(index=0, text="Rome was a massive empire.", visual_query="rome"),
            SceneScript(index=1, text="It fell due to internal corruption and invasions.", visual_query="barbarians")
        ]
        master = ScriptPayload(title="The Fall of Rome", description="A doc.", tags=[], scenes=scenes)
        
        multiplier = ContentMultiplier()
        result = await multiplier.multiply_script(master)
        print("Generated Keys:", result.keys())

    asyncio.run(test())
