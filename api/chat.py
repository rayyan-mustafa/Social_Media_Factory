"""Vercel serverless function — chat proxy.

Wraps scripts/demo_chat_proxy.py logic in a Vercel Python handler.
The Anthropic/OpenRouter API key is stored as a Vercel environment variable
(ANTHROPIC_API_KEY or OPENROUTER_API_KEY) — never committed to the repo.

Request (POST /api/chat):
  Content-Type: application/json
  Body: {model, max_tokens, system, messages}

Response:
  {id, type, role, content: [{type: "text", text: "..."}], model}
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler
from urllib import error, request as url_request

# ── Vercel puts the repo root on sys.path, but add it explicitly just in case
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

OPENROUTER = (os.getenv("OPENROUTER_API_KEY") or "").strip()
ANTHROPIC  = (os.getenv("ANTHROPIC_API_KEY")  or "").strip()
OR_MODEL   = (os.getenv("DEMO_CHAT_MODEL") or "google/gemini-2.5-flash").strip()


def _forward_openrouter(payload: dict) -> dict:
    messages = []
    system = payload.get("system") or ""
    if system:
        messages.append({"role": "system", "content": system})
    for m in payload.get("messages") or []:
        messages.append({"role": m.get("role"), "content": m.get("content")})
    body = {
        "model": OR_MODEL,
        "max_tokens": int(payload.get("max_tokens") or 300),
        "messages": messages,
    }
    req = url_request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENROUTER}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://your-portfolio.vercel.app",
            "X-Title": "agency-demo-proxy",
        },
        method="POST",
    )
    with url_request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
    return {
        "id": data.get("id") or "or",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": OR_MODEL,
    }


def _forward_anthropic(payload: dict) -> dict:
    body = {
        "model": payload.get("model") or "claude-sonnet-4-6",
        "max_tokens": int(payload.get("max_tokens") or 300),
        "system": payload.get("system") or "",
        "messages": payload.get("messages") or [],
    }
    req = url_request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": ANTHROPIC,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with url_request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8"))


def handler(request, response):
    """Vercel Python serverless handler signature."""
    # CORS preflight
    cors_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }

    if request.method == "OPTIONS":
        response.status_code = 204
        for k, v in cors_headers.items():
            response.headers[k] = v
        return response

    if request.method != "POST":
        response.status_code = 405
        response.body = json.dumps({"error": "Method not allowed"})
        return response

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        response.status_code = 400
        response.body = json.dumps({"error": "Invalid JSON"})
        return response

    try:
        if OPENROUTER:
            out = _forward_openrouter(payload)
        elif ANTHROPIC:
            out = _forward_anthropic(payload)
        else:
            raise RuntimeError(
                "No API key configured. Set OPENROUTER_API_KEY or ANTHROPIC_API_KEY "
                "as a Vercel environment variable."
            )

        response.status_code = 200
        for k, v in cors_headers.items():
            response.headers[k] = v
        response.headers["Content-Type"] = "application/json"
        response.body = json.dumps(out)
        return response

    except error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")[:800]
        response.status_code = exc.code
        for k, v in cors_headers.items():
            response.headers[k] = v
        response.headers["Content-Type"] = "application/json"
        response.body = json.dumps({"error": err})
        return response

    except Exception as exc:  # noqa: BLE001
        response.status_code = 500
        for k, v in cors_headers.items():
            response.headers[k] = v
        response.headers["Content-Type"] = "application/json"
        response.body = json.dumps({"error": str(exc)[:400]})
        return response
