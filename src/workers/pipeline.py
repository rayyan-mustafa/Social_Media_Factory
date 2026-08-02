"""Multi-stage ARQ pipeline worker."""

from __future__ import annotations

import asyncio
import base64
import tempfile
import time
from pathlib import Path

from src.core.config import get_settings
from src.core.logging import get_logger, setup_logging
from src.db import get_session_factory
from src.db.repository import (
    add_artifact,
    get_job,
    record_youtube_upload,
    transition_stage,
)
from src.domain import ArtifactKind, JobStage, JobStatus, ScriptPayload
from src.services.composer import VideoComposer
from src.services.media_retrieval import MediaRetrievalService
from src.services.runpod_client import RunPodClient
from src.services.script_generator import ScriptGenerator
from src.services.storage import ObjectStorage
from src.services.tts_kokoro import KokoroTTSService
from src.services.youtube_uploader import YouTubeUploader
from src.utils.alerts import send_discord_alert
from src.utils.metrics import (
    ACTIVE_JOBS,
    JOBS_FAILED,
    JOBS_SUCCEEDED,
    STAGE_DURATION,
)

logger = get_logger(__name__)


async def _fail_job(session, job, message: str) -> None:
    await transition_stage(
        session,
        job,
        JobStage.FAILED,
        status=JobStatus.FAILED,
        error=message,
        message=message,
    )
    JOBS_FAILED.inc()
    ACTIVE_JOBS.dec()
    await send_discord_alert(f"Job {job.id} failed: {message}")


async def run_pipeline(ctx: dict, job_id: int) -> dict:
    """Execute full content pipeline for a job."""
    setup_logging()
    settings = get_settings()
    factory = get_session_factory()
    storage = ObjectStorage()
    storage.ensure_bucket()

    ACTIVE_JOBS.inc()
    async with factory() as session:
        job = await get_job(session, job_id)
        if job is None:
            ACTIVE_JOBS.dec()
            raise ValueError(f"Job {job_id} not found")

        # Idempotency: already done
        if job.status == JobStatus.SUCCEEDED.value:
            ACTIVE_JOBS.dec()
            return {"job_id": job_id, "status": "already_succeeded"}

        job.attempt += 1
        await session.commit()

        if job.attempt > settings.max_job_attempts:
            await _fail_job(session, job, f"Exceeded max attempts ({settings.max_job_attempts})")
            return {"job_id": job_id, "status": "failed", "error": "max_attempts"}

        work_dir = Path(tempfile.mkdtemp(prefix=f"job_{job_id}_"))
        try:
            await transition_stage(
                session, job, JobStage.SCRIPTING, status=JobStatus.RUNNING, message="scripting"
            )

            # --- SCRIPT ---
            t0 = time.perf_counter()
            generator = ScriptGenerator()
            try:
                if settings.llm_api_key:
                    script = generator.generate(job.topic, job.niche, job.business_model)
                else:
                    logger.warning("llm_key_missing_using_fallback")
                    script = generator.generate_fallback(job.topic, job.niche, job.business_model)
            except Exception as exc:
                logger.exception("script_failed")
                script = generator.generate_fallback(job.topic, job.niche, job.business_model)
                logger.warning("script_fallback_used", extra={"error": str(exc)})

            job.script_json = script.model_dump()
            await session.commit()
            script_key = f"jobs/{job_id}/script.json"
            storage.upload_bytes(
                script.model_dump_json(indent=2).encode("utf-8"),
                script_key,
                content_type="application/json",
            )
            await add_artifact(
                session,
                job_id,
                ArtifactKind.SCRIPT,
                script_key,
                meta={"scenes": len(script.scenes) if script.scenes else 0},
            )
            STAGE_DURATION.labels(stage="scripting").observe(time.perf_counter() - t0)

            youtube_id = None
            final_key = ""

            if job.business_model in ("EBooks_KDP", "SEO_Blogs"):
                # --- TEXT ONLY ---
                text_content = script.content or f"# {script.title}\n\n{script.description}"
                
                if job.business_model == "EBooks_KDP":
                    from fpdf import FPDF
                    import markdown
                    
                    html_content = markdown.markdown(text_content)
                    pdf = FPDF()
                    pdf.add_page()
                    # Add a font that supports utf-8 if possible, but default arial is standard. 
                    # FPDF2 write_html supports basic html.
                    pdf.write_html(html_content)
                    
                    final_path = work_dir / "document.pdf"
                    pdf.output(str(final_path))
                    final_key = f"jobs/{job_id}/document.pdf"
                    content_type = "application/pdf"
                    kind = ArtifactKind.EBOOK_DOCUMENT
                else:
                    final_path = work_dir / "document.md"
                    final_path.write_text(text_content, encoding="utf-8")
                    final_key = f"jobs/{job_id}/document.md"
                    content_type = "text/markdown"
                    kind = ArtifactKind.BLOG_POST
                
                final_checksum = storage.checksum_file(final_path)
                storage.upload_file(final_path, final_key, content_type=content_type)
                await add_artifact(
                    session,
                    job_id,
                    kind,
                    final_key,
                    checksum=final_checksum,
                    meta={"title": script.title},
                )
            elif job.business_model in ("Podcast_Audio", "Radio_FM", "Audiobooks_ACX"):
                # --- AUDIO ONLY ---
                await transition_stage(session, job, JobStage.TTS, message="tts")
                t0 = time.perf_counter()
                tts = KokoroTTSService()
                combined_text = " ".join(scene.text for scene in script.scenes) if script.scenes else script.description
                final_path = work_dir / "final_audio.wav"
                try:
                    tts.synthesize(combined_text, final_path)
                except Exception:
                    logger.exception("kokoro_unavailable_silence")
                    tts.synthesize_silence(final_path, seconds=5.0)
                
                final_key = f"jobs/{job_id}/final_audio.wav"
                final_checksum = storage.checksum_file(final_path)
                storage.upload_file(final_path, final_key, content_type="audio/wav")
                await add_artifact(
                    session,
                    job_id,
                    ArtifactKind.PODCAST_AUDIO,
                    final_key,
                    checksum=final_checksum,
                    meta={"title": script.title},
                )
                STAGE_DURATION.labels(stage="tts").observe(time.perf_counter() - t0)
                
                redis = ctx.get("redis")
                if redis:
                    await redis.lpush("worker:metrics:tts", time.perf_counter() - t0)
            else:
                # --- VIDEO ---
                if settings.runpod_enabled:
                    await transition_stage(
                        session, job, JobStage.COMPOSING, status=JobStatus.RUNNING, message="runpod_rendering"
                    )
                    t0 = time.perf_counter()
                    client = RunPodClient()
                    resp = await client.trigger_render({"script": script.model_dump()})
                    req_id = resp["id"]
                    
                    while True:
                        status_resp = await client.status(req_id)
                        status_str = status_resp.get("status")
                        if status_str == "COMPLETED":
                            output = status_resp.get("output", {})
                            if "final_mp4_b64" in output:
                                final_path = work_dir / "final.mp4"
                                final_path.write_bytes(base64.b64decode(output["final_mp4_b64"]))
                                break
                            else:
                                raise RuntimeError(f"RunPod job missing output: {status_resp}")
                        elif status_str in ("FAILED", "CANCELLED"):
                            raise RuntimeError(f"RunPod job failed: {status_resp}")
                        await asyncio.sleep(5)

                    final_key = f"jobs/{job_id}/final.mp4"
                    final_checksum = storage.checksum_file(final_path)
                    storage.upload_file(final_path, final_key, content_type="video/mp4")
                    await add_artifact(
                        session,
                        job_id,
                        ArtifactKind.FINAL_VIDEO,
                        final_key,
                        checksum=final_checksum,
                        meta={"title": script.title},
                    )
                    STAGE_DURATION.labels(stage="composing").observe(time.perf_counter() - t0)
                    
                    redis = ctx.get("redis")
                    if redis:
                        await redis.lpush("worker:metrics:runpod", time.perf_counter() - t0)
                else:
                    # --- TTS ---
                    await transition_stage(session, job, JobStage.TTS, message="tts")
                    t0 = time.perf_counter()
                    tts = KokoroTTSService()
                    audio_paths: list[Path] = []
                    for scene in script.scenes:
                        aud_path = work_dir / f"scene_{scene.index}.wav"
                        try:
                            tts.synthesize(scene.text, aud_path)
                        except Exception:
                            logger.exception("kokoro_unavailable_silence")
                            tts.synthesize_silence(aud_path, seconds=3.0)
                        key = f"jobs/{job_id}/audio/scene_{scene.index}.wav"
                        checksum = storage.checksum_file(aud_path)
                        storage.upload_file(aud_path, key, content_type="audio/wav")
                        await add_artifact(
                            session,
                            job_id,
                            ArtifactKind.AUDIO,
                            key,
                            checksum=checksum,
                            meta={"scene": scene.index},
                        )
                        audio_paths.append(aud_path)
                    STAGE_DURATION.labels(stage="tts").observe(time.perf_counter() - t0)
                    
                    redis = ctx.get("redis")
                    if redis:
                        await redis.lpush("worker:metrics:tts", time.perf_counter() - t0)

                    # --- MEDIA ---
                    await transition_stage(session, job, JobStage.MEDIA, message="media")
                    t0 = time.perf_counter()
                    media = MediaRetrievalService(use_clip=True)
                    image_paths: list[Path] = []
                    for scene in script.scenes:
                        img_path = work_dir / f"scene_{scene.index}.jpg"
                        url = None
                        try:
                            url = await media.fetch_best_image_url(scene.visual_query or job.topic)
                        except Exception:
                            logger.exception("media_fetch_failed")
                        if url:
                            try:
                                media.download_image(url, img_path)
                            except Exception:
                                media.placeholder_image(img_path)
                                url = None
                        else:
                            media.placeholder_image(img_path)
                        key = f"jobs/{job_id}/images/scene_{scene.index}.jpg"
                        checksum = storage.checksum_file(img_path)
                        storage.upload_file(img_path, key, content_type="image/jpeg")
                        await add_artifact(
                            session,
                            job_id,
                            ArtifactKind.IMAGE,
                            key,
                            checksum=checksum,
                            meta={"scene": scene.index, "source_url": url},
                        )
                        image_paths.append(img_path)
                    STAGE_DURATION.labels(stage="media").observe(time.perf_counter() - t0)

                    # --- COMPOSE ---
                    await transition_stage(session, job, JobStage.COMPOSING, message="composing")
                    t0 = time.perf_counter()
                    composer = VideoComposer()
                    scene_videos: list[Path] = []
                    for scene, aud, img in zip(script.scenes, audio_paths, image_paths, strict=True):
                        vid_path = work_dir / f"scene_{scene.index}.mp4"
                        composer.create_scene_video(img, aud, vid_path)
                        key = f"jobs/{job_id}/scenes/scene_{scene.index}.mp4"
                        checksum = storage.checksum_file(vid_path)
                        storage.upload_file(vid_path, key, content_type="video/mp4")
                        await add_artifact(
                            session,
                            job_id,
                            ArtifactKind.SCENE_VIDEO,
                            key,
                            checksum=checksum,
                            meta={"scene": scene.index},
                        )
                        scene_videos.append(vid_path)

                    final_path = work_dir / "final.mp4"
                    composer.concatenate(scene_videos, final_path)
                    final_key = f"jobs/{job_id}/final.mp4"
                    final_checksum = storage.checksum_file(final_path)
                    storage.upload_file(final_path, final_key, content_type="video/mp4")
                    await add_artifact(
                        session,
                        job_id,
                        ArtifactKind.FINAL_VIDEO,
                        final_key,
                        checksum=final_checksum,
                        meta={"title": script.title},
                    )
                    STAGE_DURATION.labels(stage="composing").observe(time.perf_counter() - t0)
                    
                    redis = ctx.get("redis")
                    if redis:
                        await redis.lpush("worker:metrics:ffmpeg", time.perf_counter() - t0)

                # --- UPLOAD (optional) ---
                if job.upload_to_youtube:
                    await transition_stage(session, job, JobStage.UPLOADING, message="uploading")
                    t0 = time.perf_counter()
                    uploader = YouTubeUploader()
                    youtube_id = uploader.upload(
                        final_path,
                        title=script.title,
                        description=script.description,
                        tags=script.tags,
                    )
                    await record_youtube_upload(
                        session,
                        job_id,
                        youtube_id,
                        settings.youtube_privacy_status,
                    )
                    STAGE_DURATION.labels(stage="uploading").observe(time.perf_counter() - t0)
                    
                    # Push to Redis for Watchdog monitoring
                    redis = ctx.get("redis")
                    if redis and youtube_id:
                        await redis.sadd("youtube:pending_processing", f"{job_id}:{youtube_id}")
                else:
                    await transition_stage(
                        session, job, JobStage.UPLOADING, message="upload_skipped"
                    )

            await transition_stage(
                session,
                job,
                JobStage.SUCCEEDED,
                status=JobStatus.SUCCEEDED,
                message="done",
            )
            JOBS_SUCCEEDED.inc()
            ACTIVE_JOBS.dec()
            return {
                "job_id": job_id,
                "status": "succeeded",
                "final_key": final_key,
                "youtube_video_id": youtube_id,
            }
        except Exception as exc:
            logger.exception("pipeline_failed", extra={"job_id": job_id})
            job = await get_job(session, job_id)
            if job and job.attempt < settings.max_job_attempts:
                await transition_stage(
                    session,
                    job,
                    JobStage.QUEUED,
                    status=JobStatus.QUEUED,
                    error=str(exc),
                    message=f"retry_scheduled: {exc}",
                )
                ACTIVE_JOBS.dec()
                raise
            if job:
                await _fail_job(session, job, str(exc))
            else:
                ACTIVE_JOBS.dec()
            raise
        finally:
            # Best-effort cleanup of temp files
            try:
                for p in work_dir.rglob("*"):
                    if p.is_file():
                        p.unlink(missing_ok=True)
                work_dir.rmdir()
            except Exception:
                pass


# Silence unused import warning for type checkers referencing ScriptPayload in artifacts
_ = ScriptPayload
