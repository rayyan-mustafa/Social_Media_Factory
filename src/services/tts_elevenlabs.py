"""Optional ElevenLabs TTS backend stub for A/B tests (option B/C)."""

from __future__ import annotations

import logging
from pathlib import Path

from src.domain.models import Scene
from src.services.settings import get_settings

logger = logging.getLogger(__name__)


class ElevenLabsTTSError(RuntimeError):
    pass


def synthesize_scene(
    scene: Scene,
    *,
    out_path: Path,
    voice_id: str | None = None,
    model: str | None = None,
) -> None:
    """Synthesize one scene via ElevenLabs API (not wired to full VoiceModule yet)."""
    s = get_settings()
    api_key = (s.elevenlabs_api_key or "").strip()
    if not api_key:
        raise ElevenLabsTTSError(
            "ELEVENLABS_API_KEY not set — use TTS_BACKEND=kokoro for production"
        )
    vid = voice_id or s.elevenlabs_voice_id
    if not vid:
        raise ElevenLabsTTSError("ELEVENLABS_VOICE_ID required for elevenlabs backend")
    _model = model or s.elevenlabs_model
    raise ElevenLabsTTSError(
        f"ElevenLabs backend stub: configure batch chapter synthesis separately "
        f"(model={_model}, voice={vid}, scene={scene.index}). "
        "Use TTS_BACKEND=kokoro for full pipeline runs."
    )
