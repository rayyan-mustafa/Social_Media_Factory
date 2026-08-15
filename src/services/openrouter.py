"""OpenRouter chat client for YouTube metadata generation."""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from src.services.llm import LLMError, _message_text, parse_json_object
from src.services.settings import Settings

_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterError(RuntimeError):
    pass


class OpenRouterClient:
    def __init__(self, settings: Settings, *, default_model: str | None = None):
        self.settings = settings
        self.default_model = (
            default_model or settings.openrouter_model or "openrouter/free"
        ).strip()
        key = (settings.openrouter_api_key or "").strip()
        if not key or key == "replace_me":
            raise OpenRouterError(
                "OPENROUTER_API_KEY is not set. Add it to .env for YouTube metadata generation."
            )
        self.api_key = key
        self.base_url = OPENROUTER_BASE_URL.rstrip("/")

    def chat_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.7,
        timeout_s: float | None = None,
        model: str | None = None,
        parse_retries: int | None = None,
    ) -> dict[str, Any]:
        """JSON call with local repair + 2–3 LLM retries on parse failure."""
        max_attempts = (
            parse_retries
            if parse_retries is not None
            else max(1, int(getattr(self.settings, "llm_json_parse_retries", 3)))
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        last_err: Exception | None = None
        assistant: dict[str, Any] | None = None

        for attempt in range(max_attempts):
            if attempt == 0:
                assistant = self._chat_message(
                    messages,
                    temperature=temperature,
                    timeout_s=timeout_s,
                    model=model,
                )
            else:
                assert assistant is not None
                messages = [
                    *messages,
                    {"role": "assistant", "content": assistant.get("content")},
                    {
                        "role": "user",
                        "content": (
                            "Your previous reply was not valid JSON. "
                            "Return ONLY a single JSON object matching the required schema. "
                            "No markdown fences, no commentary, no trailing commas."
                        ),
                    },
                ]
                assistant = self._chat_message(
                    messages,
                    temperature=temperature,
                    timeout_s=timeout_s,
                    model=model,
                )

            content = _message_text(assistant)
            try:
                return parse_json_object(content)
            except (LLMError, json.JSONDecodeError) as exc:
                last_err = exc
                continue

        raise OpenRouterError(
            f"JSON parse failed after {max_attempts} attempt(s): {last_err}"
        ) from last_err

    def chat_text(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.7,
        timeout_s: float | None = None,
        model: str | None = None,
        reasoning_enabled: bool | None = None,
    ) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return _message_text(
            self._chat_message(
                messages,
                temperature=temperature,
                timeout_s=timeout_s,
                model=model,
                reasoning_enabled=reasoning_enabled,
            )
        )

    def _chat_message(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float,
        timeout_s: float | None,
        model: str | None,
        reasoning_enabled: bool | None = None,
    ) -> dict[str, Any]:
        url = self.base_url + "/chat/completions"
        use_model = (model or self.default_model).strip()
        timeout = float(
            timeout_s
            if timeout_s is not None
            else getattr(self.settings, "llm_timeout_s", 120.0)
        )
        retries = max(0, int(getattr(self.settings, "llm_max_retries", 2)))

        payload: dict[str, Any] = {
            "model": use_model,
            "messages": messages,
            "temperature": temperature,
        }
        use_reasoning = (
            self.settings.openrouter_reasoning_enabled
            if reasoning_enabled is None
            else reasoning_enabled
        )
        if use_reasoning:
            payload["reasoning"] = {"enabled": True}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://localhost/yt_long_factory",
            "X-Title": "yt_long_youtube_meta",
        }

        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                with httpx.Client(timeout=timeout) as client:
                    resp = client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                last_err = OpenRouterError(
                    f"OpenRouter request failed ({use_model}): {exc}"
                )
                if attempt < retries:
                    time.sleep(1.0 + attempt)
                    continue
                raise last_err from exc

            if resp.status_code >= 400:
                last_err = OpenRouterError(
                    f"OpenRouter HTTP {resp.status_code} ({use_model}): {resp.text[:800]}"
                )
                if attempt < retries and resp.status_code in {429, 500, 502, 503, 504}:
                    time.sleep(1.5 + attempt)
                    continue
                raise last_err

            data = resp.json()
            try:
                message = data["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as exc:
                raise OpenRouterError(
                    f"Unexpected OpenRouter response shape ({use_model}): {data!r}"
                ) from exc

            if not isinstance(message, dict):
                raise OpenRouterError(f"Unexpected message type: {message!r}")
            return message

        assert last_err is not None
        raise last_err
