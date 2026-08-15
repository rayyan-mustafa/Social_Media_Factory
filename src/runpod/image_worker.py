"""
RunPod serverless worker handler.

Contract (RunPod serverless):
- RunPod calls `handler(job)` with a dict that includes `job["input"]`.
- The client sends `{ "input": { ... } }`.
- Whatever we return becomes the job output.

v2 behavior:
- If you set env vars for a real RunPod diffusion endpoint, we forward requests to it.
- Otherwise we fall back to the mock generator (current testing behavior).

Supported input (inside `input{...}`):
- Still task (default): { "prompt": "...", "width": 1280, "height": 720 }
- Optional mode/task switch: { "task": "still" | "video" }
- Video task (optional): { "task":"video", "prompt":"...", "duration_s": 5,
    "reference_image_base64":"... (optional)" }

Output:
- Still: output[0].image is base64 JPEG
- Video: output[0].video_base64 is base64 MP4 (field names may vary by upstream endpoint)
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
from typing import Any

import runpod
import httpx
from PIL import Image, ImageDraw


def _mock_generate_jpeg_base64(prompt: str, width: int, height: int) -> str:
    # Deterministic "gallery-like" pattern from prompt.
    seed = int(hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16], 16)

    bg = (seed % 256, (seed // 7) % 256, (seed // 13) % 256)
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    # Tiles: avoids "solid color only" outputs
    tiles = 32
    tile_w = max(1, width // tiles)
    tile_h = max(1, height // tiles)
    for ty in range(tiles):
        for tx in range(tiles):
            v = (seed + tx * 999 + ty * 777) % 1000
            color = ((v * 3) % 256, (v * 5) % 256, (v * 7) % 256)
            if (tx + ty) % 2 == 0:
                x0, y0 = tx * tile_w, ty * tile_h
                x1 = min(width, x0 + tile_w + 1)
                y1 = min(height, y0 + tile_h + 1)
                draw.rectangle([x0, y0, x1, y1], fill=color)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return b64


def _strip_data_prefix(b64: str) -> str:
    # Handles "data:image/png;base64,AAAA..." style responses.
    if not isinstance(b64, str):
        return ""
    if "base64," in b64:
        return b64.split("base64,", 1)[1]
    return b64


def _extract_still_base64_from_runpod(data: dict[str, Any]) -> str:
    """
    Different RunPod endpoints return different shapes.
    We try common ones:
      - { output: [ { image: "<b64>" } ] }
      - { output: [ { image_base64: "<b64>" } ] }
      - { output: { image_url: "https://..." } }
      - { output: { image_base64: "<b64>" } }
    """
    output = data.get("output")
    if isinstance(output, list) and output:
        first = output[0] or {}
        for k in ("image", "image_base64", "image_b64"):
            v = first.get(k)
            if isinstance(v, str) and v.strip():
                return _strip_data_prefix(v)

    if isinstance(output, dict):
        for k in ("image", "image_base64", "image_b64"):
            v = output.get(k)
            if isinstance(v, str) and v.strip():
                return _strip_data_prefix(v)
        if isinstance(output.get("image_url"), str) and output["image_url"].strip():
            url = output["image_url"]
            return base64.b64encode(httpx.get(url, timeout=120.0).content).decode("utf-8")

    raise ValueError(f"Could not extract still base64 from upstream response: {data!r}")


def _extract_video_base64_from_runpod(data: dict[str, Any]) -> str:
    output = data.get("output")
    if isinstance(output, list) and output:
        first = output[0] or {}
        for k in ("video_base64", "video_b64", "video", "mp4_base64"):
            v = first.get(k)
            if isinstance(v, str) and v.strip():
                return _strip_data_prefix(v)

    if isinstance(output, dict):
        for k in ("video_base64", "video_b64", "video", "mp4_base64"):
            v = output.get(k)
            if isinstance(v, str) and v.strip():
                return _strip_data_prefix(v)

    raise ValueError(f"Could not extract video base64 from upstream response: {data!r}")


def _forward_runsync(*, endpoint_id: str, api_key: str, input_payload: dict[str, Any]) -> dict[str, Any]:
    url = f"https://api.runpod.ai/v2/{endpoint_id}/runsync"
    headers = {
        "authorization": f"Bearer {api_key}",
        "content-type": "application/json",
        "accept": "application/json",
    }
    resp = httpx.post(url, headers=headers, json={"input": input_payload}, timeout=600.0)
    resp.raise_for_status()
    return resp.json()


def handler(job: dict[str, Any]) -> dict[str, Any]:
    job_input = job.get("input") or {}
    prompt = str(job_input.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("Missing input.prompt")

    width = int(job_input.get("width") or 1280)
    height = int(job_input.get("height") or 720)

    task = (job_input.get("task") or job_input.get("mode") or "still").lower()

    # ---- Still forwarding (preferred) ----
    still_endpoint_id = (os.getenv("STILLS_DIFFUSION_ENDPOINT_ID") or "").strip()
    still_api_key = (os.getenv("STILLS_DIFFUSION_API_KEY") or os.getenv("RUNPOD_API_KEY") or "").strip()
    if task in ("still", "image") and still_endpoint_id and still_api_key:
        still_prompt_field = os.getenv("STILLS_PROMPT_FIELD", "prompt")
        still_width_field = os.getenv("STILLS_WIDTH_FIELD", "width")
        still_height_field = os.getenv("STILLS_HEIGHT_FIELD", "height")

        upstream_input = {
            still_prompt_field: prompt,
            still_width_field: width,
            still_height_field: height,
        }
        upstream = _forward_runsync(
            endpoint_id=still_endpoint_id,
            api_key=still_api_key,
            input_payload=upstream_input,
        )
        image_b64 = _extract_still_base64_from_runpod(upstream)
    else:
        image_b64 = _mock_generate_jpeg_base64(prompt, width, height)

    # ---- Optional video forwarding ----
    if task == "video":
        video_endpoint_id = (os.getenv("VIDEO_DIFFUSION_ENDPOINT_ID") or "").strip()
        video_api_key = (os.getenv("VIDEO_DIFFUSION_API_KEY") or os.getenv("RUNPOD_API_KEY") or "").strip()
        if not video_endpoint_id or not video_api_key:
            raise ValueError("task=video but VIDEO_DIFFUSION_ENDPOINT_ID / VIDEO_DIFFUSION_API_KEY not set")

        duration_s = float(job_input.get("duration_s") or 5)
        video_prompt_field = os.getenv("VIDEO_PROMPT_FIELD", "prompt")
        video_width_field = os.getenv("VIDEO_WIDTH_FIELD", "width")
        video_height_field = os.getenv("VIDEO_HEIGHT_FIELD", "height")
        video_duration_field = os.getenv("VIDEO_DURATION_FIELD", "duration_s")
        ref_field = os.getenv("VIDEO_REFERENCE_IMAGE_FIELD", "reference_image_base64")

        upstream_input: dict[str, Any] = {
            video_prompt_field: prompt,
            video_width_field: width,
            video_height_field: height,
            video_duration_field: duration_s,
        }
        # optional I2V
        if isinstance(job_input.get(ref_field), str) and job_input[ref_field].strip():
            upstream_input[ref_field] = job_input[ref_field]

        upstream = _forward_runsync(
            endpoint_id=video_endpoint_id,
            api_key=video_api_key,
            input_payload=upstream_input,
        )
        video_b64 = _extract_video_base64_from_runpod(upstream)
        return {
            "output": [
                {
                    "video_base64": video_b64,
                    "mime_type": "video/mp4",
                    "width": width,
                    "height": height,
                    "duration_s": duration_s,
                }
            ]
        }

    # Keep output shape compatible with common RunPod examples:
    # client decode often looks at output.images[0].image or output[0].image.
    return {
        "output": [
            {"image": image_b64, "mime_type": "image/jpeg", "width": width, "height": height}
        ]
    }


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})

