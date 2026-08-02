"""Kokoro TTS service — local/open voice synthesis."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from src.core.config import get_settings
from src.core.logging import get_logger

logger = get_logger(__name__)


class KokoroTTSService:
    """Synthesize narration with Kokoro. Lazy-loads the model."""

    def __init__(self) -> None:
        settings = get_settings()
        self.voice = settings.kokoro_voice
        self.lang = settings.kokoro_lang
        self._pipeline = None

    def _get_pipeline(self):
        if self._pipeline is None:
            from kokoro import KPipeline  # type: ignore

            self._pipeline = KPipeline(lang_code=self.lang)
            logger.info("kokoro_loaded", extra={"voice": self.voice})
        return self._pipeline

    def synthesize(self, text: str, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pipeline = self._get_pipeline()
        chunks: list[np.ndarray] = []
        for _gs, _ps, audio in pipeline(text, voice=self.voice):
            chunks.append(np.asarray(audio))
        if not chunks:
            raise RuntimeError("Kokoro produced empty audio")
        audio_out = np.concatenate(chunks)
        sf.write(str(output_path), audio_out, 24000)
        logger.info("tts_done", extra={"path": str(output_path), "samples": len(audio_out)})
        return output_path

    def synthesize_silence(self, output_path: Path, seconds: float = 2.0) -> Path:
        """Test/offline stub: write silent WAV."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        samples = np.zeros(int(24000 * seconds), dtype=np.float32)
        sf.write(str(output_path), samples, 24000)
        return output_path
