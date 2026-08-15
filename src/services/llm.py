"""WaveSpeed / OpenAI-compatible chat client for ScriptModule.

Outline  → LLM_OUTLINE_MODEL (default deepseek/deepseek-v3.2)
Expand   → LLM_EXPAND_MODEL  (default anthropic/claude-3-haiku)
Auth     → WAVESPEED_API_KEY (fallback LLM_API_KEY)
Base URL → https://llm.wavespeed.ai/v1

Sleep JSON policy (Rayyan)
--------------------------
On parse failure for a model: **2 retries** (repair + re-call) on the same model
(3 attempts total). If still failing → switch to the other paid scripting model
(``LLM_OUTLINE_FALLBACK_MODEL`` / ``LLM_EXPAND_FALLBACK_MODEL``, defaulting to the
outline↔expand pair) and again **2 retries**. If fallback also fails → raise
``JSON parse failover exhausted`` so the farm HOLD the title (no infinite requeue).

``parse_json_object`` still strips fences, extracts objects, repairs commas/quotes,
and best-effort closes truncated JSON.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from src.services.settings import Settings

_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_SMART_QUOTES = {
    "\u201c": '"',
    "\u201d": '"',
    "\u2018": "'",
    "\u2019": "'",
    "\ufeff": "",
    "\u200b": "",
}

# Watchdog / HOLD marker — keep stable for classify + notes.
JSON_FAILOVER_EXHAUSTED = "JSON parse failover exhausted"


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, settings: Settings, *, default_model: str | None = None):
        self.settings = settings
        self.default_model = (
            default_model
            or settings.llm_outline_model
            or settings.llm_model
            or "deepseek/deepseek-v3.2"
        )
        key = (settings.wavespeed_api_key or settings.llm_api_key or "").strip()
        if not key or key == "replace_me":
            raise LLMError(
                "WAVESPEED_API_KEY (or LLM_API_KEY) is not set. "
                "Add it to .env for https://llm.wavespeed.ai/v1"
            )
        self.api_key = key
        self.base_url = (settings.llm_base_url or "https://llm.wavespeed.ai/v1").rstrip(
            "/"
        )

    def chat_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.7,
        timeout_s: float | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
        same_model_retries: int | None = None,
        parse_retries: int | None = None,
        json_response_format: bool | None = None,
    ) -> dict[str, Any]:
        """JSON call with same-model retries then optional model failover.

        Per model: 1 initial + ``same_model_retries`` (default 2) repair re-calls.
        If ``fallback_model`` is set and primary exhausts, switch and retry the
        same budget. Both exhausted → ``JSON parse failover exhausted``.

        ``parse_retries`` (legacy) overrides per-model attempt count when set
        and no failover is requested.
        """
        primary = (model or self.default_model).strip()
        fb_raw = (fallback_model or "").strip()
        fallback = fb_raw if fb_raw and fb_raw != primary else None

        retries = (
            same_model_retries
            if same_model_retries is not None
            else max(0, int(getattr(self.settings, "llm_json_same_model_retries", 2)))
        )
        if parse_retries is not None and fallback is None:
            per_model_attempts = max(1, int(parse_retries))
        else:
            per_model_attempts = 1 + max(0, int(retries))

        models: list[str] = [primary]
        if fallback:
            models.append(fallback)

        last_err: Exception | None = None
        attempt_log: list[str] = []
        for mi, use_model in enumerate(models):
            # DeepSeek / explicit flag → json_object; Haiku stays plain unless forced.
            if json_response_format is not None:
                use_fmt = bool(json_response_format)
            elif getattr(self.settings, "llm_json_response_format", False):
                use_fmt = True
            else:
                use_fmt = "deepseek" in use_model.lower()
            try:
                return self._chat_json_one_model(
                    system=system,
                    user=user,
                    temperature=temperature,
                    timeout_s=timeout_s,
                    model=use_model,
                    max_attempts=per_model_attempts,
                    json_response_format=use_fmt,
                )
            except LLMError as exc:
                last_err = exc
                attempt_log.append(f"{use_model}×{per_model_attempts}")
                if mi + 1 < len(models):
                    continue
                break

        detail = "; ".join(attempt_log) or primary
        raise LLMError(
            f"{JSON_FAILOVER_EXHAUSTED} ({detail}): {last_err}"
        ) from last_err

    def _chat_json_one_model(
        self,
        *,
        system: str,
        user: str,
        temperature: float,
        timeout_s: float | None,
        model: str,
        max_attempts: int,
        json_response_format: bool,
    ) -> dict[str, Any]:
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
                    json_response_format=json_response_format,
                )
            else:
                assert assistant is not None
                cont_assistant: dict[str, Any] = {
                    "role": "assistant",
                    "content": assistant.get("content"),
                }
                if assistant.get("reasoning_details") is not None:
                    cont_assistant["reasoning_details"] = assistant["reasoning_details"]
                messages = [
                    *messages,
                    cont_assistant,
                    {
                        "role": "user",
                        "content": (
                            "Your previous reply was not valid JSON (often truncated). "
                            "Return the COMPLETE JSON object from the start, matching the "
                            "required schema. ONLY JSON — no markdown fences, no commentary, "
                            "no trailing commas. Keep it compact enough to finish."
                        ),
                    },
                ]
                assistant = self._chat_message(
                    messages,
                    temperature=max(0.2, temperature - 0.15 * attempt),
                    timeout_s=timeout_s,
                    model=model,
                    json_response_format=json_response_format,
                )

            content = _message_text(assistant)
            try:
                return parse_json_object(content)
            except (LLMError, json.JSONDecodeError) as exc:
                last_err = exc
                continue

        raise LLMError(
            f"JSON parse failed after {max_attempts} attempt(s) on {model}: {last_err}"
        ) from last_err

    def chat_text(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.7,
        timeout_s: float | None = None,
        model: str | None = None,
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
            )
        )

    def _chat_message(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float,
        timeout_s: float | None,
        model: str | None,
        json_response_format: bool | None = None,
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
        }
        if self.settings.llm_send_temperature:
            payload["temperature"] = temperature
        if self.settings.llm_reasoning_enabled:
            payload["reasoning"] = {"enabled": True}
        use_json_fmt = (
            bool(json_response_format)
            if json_response_format is not None
            else bool(self.settings.llm_json_response_format)
        )
        if use_json_fmt:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://localhost/yt_long_factory",
            "X-Title": "yt_long_script_module",
        }

        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                with httpx.Client(timeout=timeout) as client:
                    resp = client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                last_err = LLMError(f"LLM request failed ({use_model}): {exc}")
                if attempt < retries:
                    time.sleep(1.0 + attempt)
                    continue
                raise last_err from exc

            if resp.status_code >= 400:
                last_err = LLMError(
                    f"LLM HTTP {resp.status_code} ({use_model}): {resp.text[:800]}"
                )
                if attempt < retries and resp.status_code in {429, 500, 502, 503, 504}:
                    time.sleep(1.5 + attempt)
                    continue
                raise last_err

            data = resp.json()
            try:
                message = data["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMError(
                    f"Unexpected LLM response shape ({use_model}): {data!r}"
                ) from exc

            if not isinstance(message, dict):
                raise LLMError(f"Unexpected message type: {message!r}")
            return message

        assert last_err is not None
        raise last_err


def _message_text(message: dict[str, Any]) -> str:
    """Prefer final content; some free/reasoning routes put text elsewhere."""
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        joined = "\n".join(p for p in parts if p.strip()).strip()
        if joined:
            return joined

    reasoning = message.get("reasoning")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning.strip()

    raise LLMError(f"LLM returned empty content: {message!r}")


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    fence = _JSON_FENCE.search(text)
    if fence:
        return fence.group(1).strip()
    return text


def _extract_json_blob(text: str) -> str:
    """Extract the first top-level ``{...}`` or ``[...]`` span."""
    start_obj, end_obj = text.find("{"), text.rfind("}")
    start_arr, end_arr = text.find("["), text.rfind("]")
    candidates: list[tuple[int, str]] = []
    if start_obj >= 0 and end_obj > start_obj:
        candidates.append((start_obj, text[start_obj : end_obj + 1]))
    if start_arr >= 0 and end_arr > start_arr:
        candidates.append((start_arr, text[start_arr : end_arr + 1]))
    if not candidates:
        return text
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _repair_llm_json_junk(text: str) -> str:
    """Light regex cleanup for common LLM JSON junk (trailing commas, quotes)."""
    for src, dst in _SMART_QUOTES.items():
        text = text.replace(src, dst)
    # Iteratively strip trailing commas before } or ]
    prev = None
    while prev != text:
        prev = text
        text = _TRAILING_COMMA.sub(r"\1", text)
    return text.strip()


def _close_truncated_json(text: str) -> str:
    """Best-effort close of truncated JSON (mid-string / missing braces).

    Used when outline models hit output limits. Prefers a parseable partial
    object over raising — callers still validate schema afterwards.
    """
    if not text or "{" not in text:
        return text
    start = text.find("{")
    s = text[start:]
    out: list[str] = []
    stack: list[str] = []
    in_str = False
    escape = False
    for ch in s:
        out.append(ch)
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack and stack[-1] == ch:
                stack.pop()
    if in_str:
        out.append('"')
    # Drop dangling partial key/value after last safe delimiter.
    closed = "".join(out).rstrip()
    while closed and closed[-1] in ",:":
        closed = closed[:-1].rstrip()
    # If we ended mid-key without a value, trim back to prior comma/brace.
    if closed.endswith('"'):
        # Could be complete string value — leave; trailing closers handle.
        pass
    for closer in reversed(stack):
        closed = closed.rstrip(",:")
        closed += closer
    return _repair_llm_json_junk(closed)


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from messy LLM output.

    Order: strip fences → loads → extract blob → regex repair → truncate-close
    → raise LLMError (never a bare JSONDecodeError — callers retry on LLMError).
    """
    original = text or ""
    stripped = _strip_markdown_fences(original)
    # Prefer longest prefix starting at first "{" when rfind("}") is missing
    # (truncated). Fall back to balanced extract when closers exist.
    if "{" in stripped and "}" not in stripped[stripped.find("{") :]:
        blob = stripped[stripped.find("{") :]
    else:
        blob = _extract_json_blob(stripped)
    repaired_stripped = _repair_llm_json_junk(stripped)
    repaired_blob = _repair_llm_json_junk(blob)
    candidates = [
        stripped,
        blob,
        repaired_stripped,
        repaired_blob,
        _close_truncated_json(stripped),
        _close_truncated_json(blob),
        _close_truncated_json(repaired_stripped),
        _close_truncated_json(repaired_blob),
    ]
    seen: set[str] = set()
    last_err: Exception | None = None
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_err = exc
            continue
        if isinstance(obj, dict):
            return obj
        last_err = LLMError("Model JSON root must be an object")

    raise LLMError(
        f"Could not parse JSON from model output:\n{original[:500]}"
    ) from last_err
