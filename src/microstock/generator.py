"""Brief -> raster image, with a pluggable backend.

Default backend is **Nano Banana** (``gemini-2.5-flash-image``) over the Gemini
REST API, called with ``httpx`` rather than a new SDK — the house style
everywhere else in this repo (see ``src/services/openrouter.py``).

Important operational note: a Google AI Pro subscription covers the Gemini app
and AI Studio *UI*, not API quota. AI Studio free-tier keys are capped per minute
and per day. So this module treats quota exhaustion as a normal, expected
outcome: on sustained 429 it raises ``QuotaExhausted``, and the beat parks the
remaining briefs for the next run instead of burning retries or spinning.

Backends are selected by ``config -> generator.backend``:
  gemini   — Nano Banana (default, effectively free on the user's plan)
  seedream — WaveSpeed paid fallback, reuses the farm's existing poller
  mock     — deterministic local PNG, for offline tests
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from src.microstock import config, paths

logger = logging.getLogger(__name__)


class GenerateError(RuntimeError):
    """Image generation failed for a reason worth retrying later."""


class QuotaExhausted(GenerateError):
    """The backend's quota is spent. Park remaining work; do not keep retrying."""


@dataclass
class GeneratedImage:
    path: Path
    backend: str
    model: str
    cost_usd: float = 0.0
    prompt: str = ""


class _RateGate:
    """Process-wide minimum interval + 429 cooldown.

    Same shape as ``vision_judge._VisionRateGate``: one shared gate so parallel
    callers cannot collectively burn a per-minute quota.
    """

    def __init__(self, min_interval_s: float):
        self._lock = threading.Lock()
        self._next_ok = 0.0
        self.min_interval_s = float(min_interval_s)

    def acquire(self) -> None:
        with self._lock:
            wait = self._next_ok - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._next_ok = time.monotonic() + self.min_interval_s

    def penalise(self, seconds: float) -> None:
        with self._lock:
            self._next_ok = max(self._next_ok, time.monotonic() + max(0.0, seconds))


_GATES: dict[str, _RateGate] = {}
_GATES_LOCK = threading.Lock()


def _gate_for(name: str, min_interval_s: float) -> _RateGate:
    with _GATES_LOCK:
        gate = _GATES.get(name)
        if gate is None or gate.min_interval_s != min_interval_s:
            gate = _RateGate(min_interval_s)
            _GATES[name] = gate
        return gate


def _retry_after_seconds(response: httpx.Response, *, attempt: int) -> float:
    """Honour Retry-After when present, else exponential backoff capped at 60s."""
    header = response.headers.get("Retry-After", "").strip()
    if header:
        try:
            return min(60.0, max(1.0, float(header)))
        except ValueError:
            pass
    return min(60.0, 2.0 * (2**attempt))


def gemini_api_key() -> str:
    """Gemini key from the environment. Several names accepted for convenience."""
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_AI_STUDIO_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _extract_inline_image(payload: dict[str, Any]) -> bytes | None:
    """Pull the first inline image out of a generateContent response."""
    for candidate in payload.get("candidates") or []:
        parts = ((candidate.get("content") or {}).get("parts")) or []
        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data") or {}
            data = inline.get("data")
            if data:
                try:
                    return base64.b64decode(data)
                except (ValueError, TypeError):
                    continue
    return None


def _blocked_reason(payload: dict[str, Any]) -> str | None:
    feedback = payload.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        return str(feedback["blockReason"])
    for candidate in payload.get("candidates") or []:
        reason = candidate.get("finishReason")
        if reason and reason not in ("STOP", "MAX_TOKENS"):
            return str(reason)
    return None


def _generate_gemini(prompt: str, out_path: Path, cfg: dict[str, Any]) -> GeneratedImage:
    """Nano Banana via the Gemini generateContent REST endpoint."""
    api_key = gemini_api_key()
    if not api_key:
        raise GenerateError(
            "GEMINI_API_KEY is not set. Add it to .env — get a key from "
            "https://aistudio.google.com/apikey"
        )

    model = str(cfg.get("model") or "gemini-2.5-flash-image")
    base_url = str(cfg.get("base_url") or "https://generativelanguage.googleapis.com/v1beta")
    timeout_s = float(cfg.get("timeout_s") or 120)
    max_retries = int(cfg.get("max_retries") or 3)
    gate = _gate_for(f"gemini:{model}", float(cfg.get("min_interval_s") or 2.0))

    generation_config: dict[str, Any] = {}
    modalities = cfg.get("response_modalities")
    if modalities:
        generation_config["responseModalities"] = list(modalities)
    aspect = cfg.get("aspect_ratio")
    if aspect:
        generation_config["imageConfig"] = {"aspectRatio": str(aspect)}

    body: dict[str, Any] = {"contents": [{"parts": [{"text": prompt}]}]}
    if generation_config:
        body["generationConfig"] = generation_config

    url = f"{base_url.rstrip('/')}/models/{model}:generateContent"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

    last_error = ""
    for attempt in range(max_retries):
        gate.acquire()
        try:
            with httpx.Client(timeout=timeout_s) as client:
                response = client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            last_error = f"transport error: {exc}"
            time.sleep(min(30.0, 2.0 * (2**attempt)))
            continue

        if response.status_code == 429:
            delay = _retry_after_seconds(response, attempt=attempt)
            gate.penalise(delay)
            last_error = "rate limited (429)"
            logger.warning("gemini 429 — backing off %.1fs (attempt %d)", delay, attempt + 1)
            if attempt == max_retries - 1:
                raise QuotaExhausted(
                    "Gemini quota exhausted after "
                    f"{max_retries} attempts. Remaining briefs parked for the next beat."
                )
            time.sleep(delay)
            continue

        if response.status_code in (500, 502, 503, 504):
            last_error = f"server error {response.status_code}"
            time.sleep(min(30.0, 2.0 * (2**attempt)))
            continue

        if response.status_code >= 400:
            detail = response.text[:300]
            # A quota failure can also arrive as 400/403 with an explicit message.
            if "quota" in detail.lower() or "exhausted" in detail.lower():
                raise QuotaExhausted(f"Gemini quota error {response.status_code}: {detail}")
            raise GenerateError(f"Gemini HTTP {response.status_code}: {detail}")

        payload = response.json()
        image_bytes = _extract_inline_image(payload)
        if not image_bytes:
            reason = _blocked_reason(payload)
            if reason:
                raise GenerateError(f"Gemini returned no image (reason: {reason})")
            last_error = "response contained no inline image"
            continue

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(image_bytes)
        logger.info("nano banana generated %s (%d bytes)", out_path.name, len(image_bytes))
        return GeneratedImage(
            path=out_path, backend="gemini", model=model, cost_usd=0.0, prompt=prompt
        )

    raise GenerateError(f"Gemini generation failed after {max_retries} attempts: {last_error}")


def _generate_seedream(prompt: str, out_path: Path, cfg: dict[str, Any]) -> GeneratedImage:
    """Paid fallback reusing the farm's existing WaveSpeed Seedream poller."""
    from src.services.settings import get_settings
    from src.services.thumbnail_image import (
        DEFAULT_SUBMIT_URL,
        _poll_seedream_result,
    )

    get_settings.cache_clear()
    settings = get_settings()
    api_key = (settings.wavespeed_api_key or "").strip()
    if not api_key:
        raise GenerateError("WAVESPEED_API_KEY is not set — cannot use the seedream backend")

    payload = {
        "prompt": prompt.strip(),
        "size": str(cfg.get("size") or "1024*1024"),
        "output_format": "jpeg",
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    with httpx.Client(timeout=120) as client:
        response = client.post(DEFAULT_SUBMIT_URL, headers=headers, json=payload)
        if response.status_code >= 400:
            raise GenerateError(f"Seedream HTTP {response.status_code}: {response.text[:200]}")
        raw = _poll_seedream_result(client, response.json(), headers)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw)
    return GeneratedImage(
        path=out_path, backend="seedream", model="seedream-v5.0-lite",
        cost_usd=float(cfg.get("usd_per_image") or 0.035), prompt=prompt,
    )


def _generate_mock(prompt: str, out_path: Path, cfg: dict[str, Any]) -> GeneratedImage:
    """Deterministic offline placeholder — flat colour blocks derived from the prompt."""
    from PIL import Image, ImageDraw

    seed = hashlib.sha256(prompt.encode("utf-8")).digest()
    canvas = int(config.section("cleaner").get("canvas") or 1024)
    img = Image.new("RGB", (canvas, canvas), (250, 250, 248))
    draw = ImageDraw.Draw(img)
    for i in range(6):
        off = i * 3
        colour = (seed[off] , seed[off + 1], seed[off + 2])
        x0 = int(seed[off] / 255 * canvas * 0.6)
        y0 = int(seed[off + 1] / 255 * canvas * 0.6)
        span = int(canvas * (0.18 + (seed[off + 2] / 255) * 0.22))
        draw.rectangle([x0, y0, x0 + span, y0 + span], fill=colour)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return GeneratedImage(
        path=out_path, backend="mock", model="mock", cost_usd=0.0, prompt=prompt
    )


_BACKENDS = {
    "gemini": _generate_gemini,
    "seedream": _generate_seedream,
    "mock": _generate_mock,
}


def generate(
    prompt: str,
    out_path: str | Path | None = None,
    *,
    backend: str | None = None,
    asset_id: str | None = None,
) -> GeneratedImage:
    """Generate one raster from a prompt using the configured backend."""
    generator_cfg = config.section("generator")
    name = (backend or generator_cfg.get("backend") or "gemini").strip()
    if name not in _BACKENDS:
        raise GenerateError(f"unknown generator backend {name!r}; expected one of {list(_BACKENDS)}")

    if out_path:
        dest = Path(out_path)
    else:
        stem = asset_id or hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        dest = paths.RAW_PNG_DIR / f"{stem}.png"

    if dest.exists() and dest.stat().st_size > 0:
        logger.info("generate: reusing existing %s", dest.name)
        return GeneratedImage(
            path=dest, backend=name, model=str(generator_cfg.get(name, {}).get("model", name)),
            cost_usd=0.0, prompt=prompt,
        )

    backend_cfg = dict(generator_cfg.get(name) or {})
    return _BACKENDS[name](prompt, dest, backend_cfg)
