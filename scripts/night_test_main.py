#!/usr/bin/env python3
"""Night-test harness: YouTube_Shorts E2E (direct upload) + create smoke for all models.

Imports BUSINESS_MODELS from src.domain (single source of truth).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

from src.domain import BUSINESS_MODELS

BASE = "http://localhost:8000"
HEADERS = {"Content-Type": "application/json"}


def _req(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"detail": raw}
        return exc.code, payload


def smoke_factory_labels() -> bool:
    ok = True
    for model in BUSINESS_MODELS:
        if model == "YouTube_Shorts":
            continue
        code, body = _req(
            "POST",
            "/v1/jobs",
            {
                "topic": f"Factory smoke {model}",
                "niche": "documentary",
                "business_model": model,
                "upload_to_youtube": False,
            },
        )
        if code != 202 or body.get("business_model") != model:
            print(f"FAIL smoke {model}: {code} {body}")
            ok = False
        else:
            print(f"OK smoke {model} -> job {body.get('id')} queue={model}")
    return ok


def run_main_shorts() -> bool:
    code, body = _req(
        "POST",
        "/v1/jobs",
        {
            "topic": "The Fall of Constantinople",
            "niche": "history",
            "business_model": "YouTube_Shorts",
        },
    )
    if code != 202:
        print(f"FAIL create Shorts: {code} {body}")
        return False
    job_id = body["id"]
    print(f"Shorts job {job_id} queued (direct YouTube upload)")
    deadline = time.time() + 45 * 60
    while time.time() < deadline:
        sc, jb = _req("GET", f"/v1/jobs/{job_id}")
        if sc != 200:
            print(f"poll error {sc} {jb}")
            time.sleep(10)
            continue
        status = jb.get("status")
        stage = jb.get("stage")
        yt = jb.get("youtube_video_id")
        print(f"  status={status} stage={stage} youtube={yt}")
        if status == "succeeded":
            if not yt:
                print("FAIL succeeded without youtube_video_id")
                return False
            print(f"OK uploaded https://www.youtube.com/watch?v={yt}")
            return True
        if status == "failed":
            print(f"FAIL job: {jb.get('error')}")
            return False
        time.sleep(15)
    print("FAIL timeout waiting for Shorts")
    return False


def main() -> int:
    hc, health = _req("GET", "/health")
    if hc != 200 or health.get("status") != "ok":
        print(f"FAIL health: {hc} {health}")
        return 1
    print("health ok")
    if not smoke_factory_labels():
        return 1
    if not run_main_shorts():
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
