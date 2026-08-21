#!/usr/bin/env python3
"""Local chat proxy for agency demos — keeps API keys off the browser.

Usage:
  .venv/bin/python scripts/demo_chat_proxy.py
  Open client-chatbot-demo.html / sales-chatbot-portfolio.html
  (they POST to http://127.0.0.1:8787/v1/messages)

Uses OPENROUTER_API_KEY from .env (preferred) or ANTHROPIC_API_KEY.
Never expose this port publicly without auth.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load .env lightly
env_path = ROOT / ".env"
if env_path.is_file():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

HOST = "127.0.0.1"
PORT = 8787
OPENROUTER = (os.getenv("OPENROUTER_API_KEY") or "").strip()
ANTHROPIC = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
# Cheap reliable text model on OpenRouter for demos
OR_MODEL = (os.getenv("DEMO_CHAT_MODEL") or "openrouter/free").strip()


def forward_openrouter(payload: dict) -> dict:
    model_name = (payload.get("model") or OR_MODEL).strip() or "openrouter/free"
    messages = []
    system = payload.get("system") or ""
    if system:
        messages.append({"role": "system", "content": system})
    for m in payload.get("messages") or []:
        messages.append({"role": m.get("role"), "content": m.get("content")})
    body = {
        "model": model_name,
        "max_tokens": int(payload.get("max_tokens") or 300),
        "messages": messages,
        "reasoning": {"enabled": True},
    }
    req = request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENROUTER}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://127.0.0.1:8787",
            "X-Title": "agency-demo-proxy",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
    return {
        "id": data.get("id") or "or",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": OR_MODEL,
    }


def forward_anthropic(payload: dict) -> dict:
    body = {
        "model": payload.get("model") or "claude-sonnet-4-6",
        "max_tokens": int(payload.get("max_tokens") or 300),
        "system": payload.get("system") or "",
        "messages": payload.get("messages") or [],
    }
    req = request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": ANTHROPIC,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/", "/health"}:
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            mode = "openrouter" if OPENROUTER else ("anthropic" if ANTHROPIC else "none")
            self.wfile.write(json.dumps({"ok": True, "mode": mode}).encode())
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/messages":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return
        try:
            if OPENROUTER:
                out = forward_openrouter(payload)
            elif ANTHROPIC:
                out = forward_anthropic(payload)
            else:
                raise RuntimeError("Set OPENROUTER_API_KEY or ANTHROPIC_API_KEY in .env")
            body = json.dumps(out).encode("utf-8")
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        except error.HTTPError as exc:
            err = exc.read().decode("utf-8", errors="replace")[:800]
            self.send_response(exc.code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": err}).encode())
        except Exception as exc:  # noqa: BLE001
            self.send_response(500)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(exc)[:400]}).encode())

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("proxy: " + (fmt % args) + "\n")


def main() -> None:
    if not OPENROUTER and not ANTHROPIC:
        print("ERROR: need OPENROUTER_API_KEY or ANTHROPIC_API_KEY in .env", file=sys.stderr)
        sys.exit(1)
    mode = "openrouter" if OPENROUTER else "anthropic"
    print(f"Demo chat proxy on http://{HOST}:{PORT}/v1/messages  mode={mode}")
    print("Point demo HTML API_BASE at this URL. Ctrl+C to stop.")
    HTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
