"""CosyVoice / Fish TTS via ephemeral RunPod GPU pod.

Default image: neosun/cosyvoice (Fun-CosyVoice3-0.5B) — CosyVoice2-class quality,
fits RTX 2000 Ada 16GB (~8GB+ recommended). OpenAI-compatible /v1/audio/speech.

Enable with TTS_BACKEND=cosyvoice_runpod (keep kokoro as default until tested).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from src.domain.models import Scene
from src.runpod.client import RunPodClientError
from src.runpod.lifecycle import EphemeralPodSession, voice_pod_spec_from_settings
from src.services.settings import get_settings

logger = logging.getLogger(__name__)


class CosyVoiceRunPodError(RuntimeError):
    pass


class CosyVoiceHttpClient:
    def __init__(self, base_url: str, *, voice: str | None = None, timeout_s: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.voice = voice or get_settings().cosyvoice_voice_id or "default"
        self.timeout_s = timeout_s

    def health(self) -> dict[str, Any]:
        r = httpx.get(f"{self.base_url}/health", timeout=30.0)
        if r.status_code >= 400:
            raise CosyVoiceRunPodError(f"health HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except Exception:  # noqa: BLE001
            return {"status": "ok", "raw": r.text[:200]}

    def synthesize(self, text: str, *, voice: str | None = None) -> bytes:
        payload = {
            "input": text,
            "voice": voice or self.voice,
            "response_format": "wav",
        }
        r = httpx.post(
            f"{self.base_url}/v1/audio/speech",
            json=payload,
            timeout=self.timeout_s,
        )
        if r.status_code >= 400:
            raise CosyVoiceRunPodError(
                f"/v1/audio/speech HTTP {r.status_code}: {r.text[:500]}"
            )
        if not r.content or len(r.content) < 44:
            raise CosyVoiceRunPodError("CosyVoice returned empty/short audio")
        return r.content

    def synthesize_to_path(
        self, text: str, out_path: Path | str, *, voice: str | None = None
    ) -> Path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(self.synthesize(text, voice=voice))
        return out


def synthesize_scene(scene: Scene, *, out_path: Path) -> Path:
    """One-shot: spin voice pod → synthesize scene → terminate.

    Prefer EphemeralVoiceSession for multi-scene jobs (one pod for whole script).
    """
    with EphemeralVoiceSession() as sess:
        return sess.synthesize_to_path(scene.text or "", out_path)


class EphemeralVoiceSession:
    """Hold one CosyVoice pod for many scenes, terminate on exit."""

    def __init__(self):
        self._session: EphemeralPodSession | None = None
        self.http: CosyVoiceHttpClient | None = None

    def __enter__(self) -> EphemeralVoiceSession:
        spec = voice_pod_spec_from_settings()
        self._session = EphemeralPodSession(spec)
        self._session.__enter__()
        assert self._session.base_url
        s = get_settings()
        self.http = CosyVoiceHttpClient(
            self._session.base_url,
            voice=s.cosyvoice_voice_id or None,
            timeout_s=float(s.runpod_voice_synth_timeout_s),
        )
        try:
            self.http.health()
        except Exception as exc:  # noqa: BLE001
            logger.warning("CosyVoice /health not ready yet: %s", exc)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        if self._session:
            self._session.__exit__(exc_type, exc, tb)

    def synthesize_to_path(self, text: str, out_path: Path | str) -> Path:
        if not self.http:
            raise CosyVoiceRunPodError("session not started")
        return self.http.synthesize_to_path(text, out_path)

    @property
    def pod_id(self) -> str | None:
        return self._session.pod_id if self._session else None


# Alias for fish_runpod flag — same HTTP OpenAI speech contract if image exposes it.
FishRunPodSession = EphemeralVoiceSession
