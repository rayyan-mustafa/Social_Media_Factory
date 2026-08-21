"""RunPod serverless handler — CLIP/TTS/compose burst worker.

Deploy this image to a RunPod Serverless endpoint. The VPS control plane
triggers jobs via RunPodClient when RUNPOD_ENABLED=true.
"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path

import runpod

from src.domain import ScriptPayload
from src.services.composer import VideoComposer
from src.services.media_retrieval import MediaRetrievalService
from src.services.tts_kokoro import KokoroTTSService


async def _render(job_input: dict) -> dict:
    script = ScriptPayload.model_validate(job_input["script"])
    work = Path(tempfile.mkdtemp(prefix="runpod_"))
    tts = KokoroTTSService()
    media = MediaRetrievalService(use_clip=True)
    composer = VideoComposer()

    scene_videos: list[Path] = []
    for scene in script.scenes:
        aud = work / f"a_{scene.index}.wav"
        img = work / f"i_{scene.index}.jpg"
        vid = work / f"v_{scene.index}.mp4"
        try:
            tts.synthesize(scene.text, aud)
        except Exception:
            tts.synthesize_silence(aud, seconds=3.0)
        url = await media.fetch_best_image_url(scene.visual_query)
        if url:
            try:
                media.download_image(url, img)
            except Exception:
                media.placeholder_image(img)
        else:
            media.placeholder_image(img)
        composer.create_scene_video(img, aud, vid)
        scene_videos.append(vid)

    final = work / "final.mp4"
    composer.concatenate(scene_videos, final)
    encoded = base64.b64encode(final.read_bytes()).decode("ascii")
    return {"final_mp4_b64": encoded, "title": script.title}


async def handler(event: dict) -> dict:
    """Async handler — RunPod's worker already owns the event loop."""
    job_input = event.get("input") or {}
    return await _render(job_input)


runpod.serverless.start({"handler": handler})
