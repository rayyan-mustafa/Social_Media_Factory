"""Channel-scoped OpenRouter free-first router for Weird Human Biology.

Eligible WHB LLM stages (not scripting, not vision judge) try ``openrouter/free``
up to two times. Parseable success = the stage ``parse`` callback returns.
After two failures, call the already-wired fallback (WaveSpeed model).

Vision judge stays locked in ``src.services.vision_judge`` (no paid VL fallback).
Script writer does not use this router.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from src.services.openrouter import OpenRouterClient
from src.services.settings import Settings, get_settings

T = TypeVar("T")

FREE_MODEL = "openrouter/free"
FREE_ATTEMPTS = 2
WAVESPEED_FALLBACK_MODEL = "google/gemini-2.5-flash-lite"

WHB_CHANNEL = "weird_human_biology"
WHB_CHANNELS = frozenset(
    {
        "weird_human_biology",
        "weird_biology",
        "whb",
    }
)

STAGE_SHOT_PLAN = "shot_plan"
STAGE_VALIDATE = "validate"
STAGE_CLAIM_FIX = "claim_fix"
STAGE_SCRIPT = "script"

EXCLUDED_STAGES = frozenset(
    {
        "script",
        "scripting",
        "script_writer",
        "beat_sheet",
        "script_beat_sheet",
    }
)

_SAFETY_LINE_RE = re.compile(
    r"^\s*(?:User Safety|Response Safety):\s*(safe|unsafe)\s*$",
    re.IGNORECASE,
)
_SAFETY_CAT_RE = re.compile(r"^\s*Safety Categories:\s*.*$", re.IGNORECASE)


class WhbLlmRouterError(RuntimeError):
    pass


@dataclass
class WhbLlmCallMeta:
    model_used: str
    provider: str
    used_fallback: bool
    skipped_free: bool = False
    attempts: list[dict[str, Any]] = field(default_factory=list)


def normalize_channel(channel: str | None) -> str:
    return (channel or "").strip().lower().replace("-", "_").replace(" ", "_")


def channel_uses_free_first(channel: str | None) -> bool:
    return normalize_channel(channel) in WHB_CHANNELS


def stage_uses_free_first(channel: str | None, stage: str | None) -> bool:
    if not channel_uses_free_first(channel):
        return False
    key = (stage or "").strip().lower()
    if not key:
        return True
    return key not in EXCLUDED_STAGES


def strip_openrouter_safety_preamble(text: str) -> tuple[str, str | None]:
    """Strip free-router ``User Safety: safe|unsafe`` lines (vision_judge pattern)."""
    lines = (text or "").strip().splitlines()
    safety: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = _SAFETY_LINE_RE.match(line)
        if m:
            safety = m.group(1).lower()
            i += 1
            if i < len(lines) and _SAFETY_CAT_RE.match(lines[i].strip()):
                i += 1
            continue
        break
    rest = "\n".join(lines[i:]).strip()
    return rest, safety


class WhbFreeFirstRouter:
    """Try openrouter/free ×2 for WHB stages, then the wired WaveSpeed model."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        channel: str = WHB_CHANNEL,
        openrouter: OpenRouterClient | None = None,
    ):
        self.settings = settings or get_settings()
        self.channel = channel
        self._openrouter = openrouter

    def _get_openrouter(self) -> OpenRouterClient:
        if self._openrouter is None:
            self._openrouter = OpenRouterClient(
                self.settings, default_model=FREE_MODEL
            )
        return self._openrouter

    def call(
        self,
        *,
        system: str,
        user: str,
        parse: Callable[[str], T],
        fallback: Callable[[], T],
        fallback_model: str,
        stage: str = "",
        temperature: float = 0.3,
        timeout_s: float = 120.0,
        reasoning_enabled: bool = False,
    ) -> tuple[T, WhbLlmCallMeta]:
        """Return ``(parsed, meta)``. ``parse`` raises if the free-model output is unusable."""
        attempts: list[dict[str, Any]] = []
        if not stage_uses_free_first(self.channel, stage):
            value = fallback()
            attempts.append(
                {
                    "provider": "wavespeed",
                    "model": fallback_model,
                    "n": 1,
                    "ok": True,
                    "skipped_free": True,
                }
            )
            return value, WhbLlmCallMeta(
                model_used=fallback_model,
                provider="wavespeed",
                used_fallback=True,
                skipped_free=True,
                attempts=attempts,
            )

        last_err: Exception | None = None
        for i in range(FREE_ATTEMPTS):
            attempt: dict[str, Any] = {
                "provider": "openrouter",
                "model": FREE_MODEL,
                "n": i + 1,
            }
            try:
                text = self._get_openrouter().chat_text(
                    system=system,
                    user=user,
                    temperature=temperature,
                    model=FREE_MODEL,
                    timeout_s=timeout_s,
                    reasoning_enabled=reasoning_enabled,
                )
                text, _safety = strip_openrouter_safety_preamble(text)
                attempt["chars"] = len(text)
                value = parse(text)
                attempt["ok"] = True
                attempts.append(attempt)
                return value, WhbLlmCallMeta(
                    model_used=FREE_MODEL,
                    provider="openrouter",
                    used_fallback=False,
                    attempts=attempts,
                )
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                attempt["ok"] = False
                attempt["error"] = str(exc)[:400]
                attempts.append(attempt)
                continue

        attempt_fb: dict[str, Any] = {
            "provider": "wavespeed",
            "model": fallback_model,
            "n": 1,
        }
        try:
            value = fallback()
            attempt_fb["ok"] = True
            attempts.append(attempt_fb)
            return value, WhbLlmCallMeta(
                model_used=fallback_model,
                provider="wavespeed",
                used_fallback=True,
                attempts=attempts,
            )
        except Exception as exc:  # noqa: BLE001
            attempt_fb["ok"] = False
            attempt_fb["error"] = str(exc)[:400]
            attempts.append(attempt_fb)
            raise WhbLlmRouterError(
                f"{stage or 'whb'} failed after openrouter×{FREE_ATTEMPTS} "
                f"({FREE_MODEL}) and {fallback_model}: {exc}; prior={last_err}"
            ) from exc
