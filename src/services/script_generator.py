"""LLM script generation via OpenAI-compatible API (WaveSpeed / OpenRouter)."""

from __future__ import annotations

import json
import re

from openai import OpenAI

from src.core.config import get_settings
from src.core.logging import get_logger
from src.domain import SceneScript, ScriptPayload

logger = get_logger(__name__)

def get_system_prompt(business_model: str) -> str:
    if business_model in ("EBooks_KDP", "SEO_Blogs"):
        return """You are an expert writer.
Return ONLY valid JSON matching this schema:
{
  "title": "string",
  "description": "string",
  "tags": ["string"],
  "content": "markdown formatted text (very long and detailed)"
}
Write an engaging, long-form text document appropriate for the format requested."""
    elif business_model in ("Podcast_Audio", "Radio_FM", "Audiobooks_ACX"):
        return """You are an expert audio scriptwriter.
Return ONLY valid JSON matching this schema:
{
  "title": "string",
  "description": "string",
  "tags": ["string"],
  "scenes": [
    {"index": 0, "text": "spoken narration", "visual_query": "none"}
  ]
}
Write a compelling audio-only script broken into sequential segments (scenes)."""
    elif business_model == "Online_Courses_Teachable":
        return """You are an expert course creator.
Return ONLY valid JSON matching this schema:
{
  "title": "string",
  "description": "string",
  "tags": ["string"],
  "scenes": [
    {"index": 0, "text": "lesson narration", "visual_query": "slide or visual concept"}
  ]
}
Write 3-5 comprehensive educational lessons (scenes). Narration must be authoritative and clear."""
    else:
        # Default Video prompt
        return """You are an expert documentary YouTube scriptwriter.
Return ONLY valid JSON matching this schema:
{
  "title": "string",
  "description": "string",
  "tags": ["string"],
  "scenes": [
    {"index": 0, "text": "narration 80-180 words", "visual_query": "image search query"}
  ]
}
Write 3 scenes. Narration must be spoken-word friendly.
visual_query must be concrete for Wikimedia search."""


def _extract_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise
        return json.loads(match.group(0))


class ScriptGenerator:
    def __init__(self) -> None:
        settings = get_settings()
        self._model = settings.llm_model
        self._client = OpenAI(
            api_key=settings.wavespeed_api_key or "missing",
            base_url=settings.llm_base_url,
            timeout=120.0,
            max_retries=2,
        )

    def generate(self, topic: str, niche: str, business_model: str = "YouTube_Shorts") -> ScriptPayload:
        logger.info("script_generate_start", extra={"topic": topic, "niche": niche, "model": business_model})
        
        system_prompt = get_system_prompt(business_model)
        user_prompt = f"Create content about '{topic}' for the '{niche}' niche. Format specifically for {business_model}."
        
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
            temperature=0.7,
        )
        content = response.choices[0].message.content or ""
        data = _extract_json(content)
        payload = ScriptPayload.model_validate(data)
        
        from src.services.scene_splitter import split_into_scenes
        channel_type = "sleep_story" if "Sleep" in business_model else "history"
        
        # Merge LLM text and re-split using our dynamic pacing
        full_text = payload.content if payload.content else " ".join(s.text for s in payload.scenes)
        if not full_text:
            full_text = "Missing content."
            
        new_scene_texts = split_into_scenes(full_text, channel_type)
        
        scenes = [
            SceneScript(index=i, text=text, visual_query=f"{topic} visualization")
            for i, text in enumerate(new_scene_texts)
        ]
        
        result = ScriptPayload(
            title=payload.title,
            description=payload.description,
            tags=payload.tags,
            scenes=scenes,
            content=payload.content,
        )
        logger.info("script_generate_done", extra={"scenes": len(result.scenes), "has_content": bool(result.content)})
        return result

    def generate_fallback(self, topic: str, niche: str, business_model: str = "YouTube_Shorts") -> ScriptPayload:
        """Deterministic fallback when LLM is unavailable (tests / offline)."""
        if business_model in ("EBooks_KDP", "SEO_Blogs"):
            return ScriptPayload(
                title=f"{topic}: A {niche.title()} Guide",
                description=f"An automated text document exploring {topic}.",
                tags=[topic, niche, "document"],
                content=f"# {topic}\n\nWelcome to this comprehensive guide about {topic} in the {niche} space.",
            )
        from src.services.scene_splitter import split_into_scenes
        channel_type = "sleep_story" if "Sleep" in business_model else "history"
        
        full_text = (
            f"In this part of our {niche} documentary on {topic}, "
            f"we explore a key chapter of the story. "
            f"It covers the historical context and why it still matters today. " * 3
        )
        
        new_scene_texts = split_into_scenes(full_text, channel_type)
        
        scenes = [
            SceneScript(
                index=i,
                text=text,
                visual_query=f"{topic} historical photograph",
            )
            for i, text in enumerate(new_scene_texts)
        ]
        
        return ScriptPayload(
            title=f"{topic}: A {niche.title()} Documentary",
            description=f"An automated documentary exploring {topic}.",
            tags=[topic, niche, "documentary", "history"],
            scenes=scenes,
        )
