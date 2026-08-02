#!/usr/bin/env python3
"""Production preflight — validates .env readiness without printing secret values."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main() -> int:
    env = {**_load_dotenv(ROOT / ".env"), **os.environ}
    required = [
        "POSTGRES_PASSWORD",
        "S3_SECRET_KEY",
        "RUNPOD_API_KEY",
        "RUNPOD_ENDPOINT_ID",
        "WAVESPEED_API_KEY",
    ]
    recommended = ["API_KEY", "SECRET_ENCRYPTION_KEY"]
    placeholders = {
        "",
        "change_me",
        "change_me_strong_password",
        "minioadmin_change_me",
    }
    ok = True
    for key in required:
        val = env.get(key, "")
        if val in placeholders:
            print(f"FAIL  {key} missing or placeholder")
            ok = False
        else:
            print(f"OK    {key} set ({len(val)} chars)")

    runpod = env.get("RUNPOD_ENABLED", "false").lower() in {"1", "true", "yes"}
    print(f"{'OK' if runpod else 'FAIL'}    RUNPOD_ENABLED={env.get('RUNPOD_ENABLED', '')}")
    if not runpod:
        ok = False

    for key in recommended:
        val = env.get(key, "")
        if not val or val.startswith("change_me") or val.startswith("dev-only"):
            print(f"WARN  {key} not set for production")
        else:
            print(f"OK    {key} set")

    secrets = [
        ROOT / "secrets" / "client_secret.json",
        ROOT / "secrets" / "youtube_token.json",
    ]
    for path in secrets:
        if path.is_file():
            print(f"OK    {path.relative_to(ROOT)}")
        else:
            print(f"FAIL  missing {path.relative_to(ROOT)}")
            ok = False

    if ok:
        print("PREFLIGHT PASS — ready for docker compose production up")
        return 0
    print("PREFLIGHT FAIL — fix items above before VPS deploy")
    return 1


if __name__ == "__main__":
    sys.exit(main())
