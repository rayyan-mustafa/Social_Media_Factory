"""Thumbnail image generation via WaveSpeed Seedream v5.0 Lite."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

from src.services.settings import ROOT, Settings, get_settings

DEFAULT_SUBMIT_URL = (
    "https://api.wavespeed.ai/api/v3/bytedance/seedream-v5.0-lite"
)
YOUTUBE_THUMB_SIZE = (1280, 720)
GENERATION_SIZE = "2560*1440"


class ThumbnailImageError(RuntimeError):
    pass


def generate_thumbnail_image(
    prompt: str,
    out_path: Path | str,
    *,
    settings: Settings | None = None,
    skip_if_exists: bool = True,
) -> Path:
    """Generate a YouTube thumbnail JPEG via WaveSpeed Seedream v5.0 Lite."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if skip_if_exists and out_path.exists() and out_path.stat().st_size > 5000:
        return out_path

    s = settings or get_settings()
    api_key = (s.wavespeed_api_key or s.llm_api_key or "").strip()
    if not api_key or api_key == "replace_me":
        raise ThumbnailImageError(
            "WAVESPEED_API_KEY missing in .env (required for Seedream thumbnail generation)"
        )

    submit_url = (s.thumbnail_image_submit_url or DEFAULT_SUBMIT_URL).strip()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "prompt": prompt.strip(),
        "size": GENERATION_SIZE,
        "output_format": "jpeg",
    }

    with httpx.Client(timeout=120.0) as client:
        resp = client.post(submit_url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise ThumbnailImageError(
                f"Seedream submit HTTP {resp.status_code}: {resp.text[:600]}"
            )
        body = resp.json()
        data = body.get("data", body) if isinstance(body, dict) else {}
        if not isinstance(data, dict):
            raise ThumbnailImageError(f"Unexpected submit response: {body!r}")

        task_id = data.get("id")
        result_url = None
        urls = data.get("urls")
        if isinstance(urls, dict) and urls.get("get"):
            result_url = str(urls["get"])
        elif task_id:
            result_url = (
                f"https://api.wavespeed.ai/api/v3/predictions/{task_id}/result"
            )
        else:
            raise ThumbnailImageError(f"No task id in Seedream response: {body!r}")

        image_url = _poll_seedream_result(client, result_url, headers=headers)
        raw = client.get(image_url, timeout=180.0).content
        if len(raw) < 1000:
            raise ThumbnailImageError(f"Downloaded thumbnail too small ({len(raw)} bytes)")

    _save_youtube_jpeg(raw, out_path)
    return out_path


def _poll_seedream_result(
    client: httpx.Client,
    result_url: str,
    *,
    headers: dict[str, str],
    timeout_s: float = 600.0,
) -> str:
    deadline = time.monotonic() + timeout_s
    poll_interval = 2.0
    last_status = "unknown"

    while time.monotonic() < deadline:
        resp = client.get(result_url, headers=headers, timeout=60.0)
        if resp.status_code >= 400:
            raise ThumbnailImageError(
                f"Seedream poll HTTP {resp.status_code}: {resp.text[:400]}"
            )
        body = resp.json()
        if isinstance(body, dict) and body.get("code") not in (None, 200):
            raise ThumbnailImageError(
                f"Seedream poll error: {body.get('message') or body}"
            )
        data = body.get("data", body) if isinstance(body, dict) else {}
        if not isinstance(data, dict):
            raise ThumbnailImageError(f"Unexpected poll response: {body!r}")

        status = str(data.get("status") or "").lower()
        last_status = status or last_status

        if status == "completed":
            outputs = data.get("outputs") or []
            if not outputs:
                raise ThumbnailImageError("Seedream completed but outputs empty")
            url = outputs[0]
            if not isinstance(url, str) or not url.startswith("http"):
                raise ThumbnailImageError(f"Unexpected output URL: {url!r}")
            return url

        if status in {"failed", "cancelled", "timeout"}:
            raise ThumbnailImageError(
                f"Seedream task {status}: {data.get('error') or data}"
            )

        if status not in {"created", "processing", ""}:
            raise ThumbnailImageError(f"Unexpected Seedream status: {status}")

        time.sleep(poll_interval)
        poll_interval = min(10.0, poll_interval + 1.0)

    raise ThumbnailImageError(
        f"Seedream poll timed out (last status={last_status})"
    )


def _save_youtube_jpeg(raw: bytes, out_path: Path) -> None:
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(raw)) as im:
        im = im.convert("RGB")
        target = YOUTUBE_THUMB_SIZE
        if im.size != target:
            # Same 16:9 aspect — direct resize; cover-crop if needed
            src_w, src_h = im.size
            target_ratio = target[0] / target[1]
            src_ratio = src_w / src_h if src_h else target_ratio
            if abs(src_ratio - target_ratio) > 0.02:
                if src_ratio > target_ratio:
                    new_w = int(src_h * target_ratio)
                    left = (src_w - new_w) // 2
                    im = im.crop((left, 0, left + new_w, src_h))
                else:
                    new_h = int(src_w / target_ratio)
                    top = (src_h - new_h) // 2
                    im = im.crop((0, top, src_w, top + new_h))
            im = im.resize(target, Image.Resampling.LANCZOS)
        im.save(out_path, format="JPEG", quality=92)
