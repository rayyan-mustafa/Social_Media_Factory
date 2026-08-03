"""Thumbnail generation stage processor."""

import time
from typing import Any

from src.core.logging import get_logger
from src.core.paths import artifact_key
from src.db.repository import add_artifact, transition_stage, get_artifacts
from src.domain import ArtifactKind, JobStage, JobStatus, ScriptPayload
from src.services.media_retrieval import MediaRetrievalService
from src.utils.metrics import STAGE_DURATION
from src.workers.stages import StageContext
from src.workers.stages.routing import get_next_stage_for_job, should_skip_stage

logger = get_logger(__name__)

class ThumbnailStage:
    """Retrieves or generates a thumbnail for the video."""
    
    async def execute(self, ctx: StageContext, artifacts: dict[str, Any]) -> dict[str, Any]:
        if should_skip_stage(ctx.job.stage, JobStage.THUMBNAIL, ctx.business_model):
            logger.info("stage_thumbnail_skipped", extra={"job_id": ctx.job.id, "reason": f"stage is already {ctx.job.stage}"})
            db_artifacts = await get_artifacts(ctx.session, ctx.job.id, ArtifactKind.THUMBNAIL)
            if db_artifacts:
                local_path = ctx.work_dir / db_artifacts[0].s3_key.split("/")[-1]
                if not local_path.exists():
                    ctx.storage.download_file(db_artifacts[0].s3_key, local_path)
                return {"thumbnail_path": str(local_path)}
            return {}

        logger.info("stage_thumbnail_start", extra={"job_id": ctx.job.id})
        
        try:
            t0 = time.perf_counter()
            
            # 1. Load Prerequisites
            script: ScriptPayload | None = artifacts.get("script")
            if not script and ctx.job.script_json:
                script = ScriptPayload(**ctx.job.script_json)

            if not script:
                raise ValueError("Thumbnail stage requires 'script' artifact")

            # 2. Execute Business Logic
            media_service = MediaRetrievalService(use_clip=False) # Thumbnails don't necessarily need clip
            
            # Just use topic or title for thumbnail prompt
            prompt = script.title or ctx.job.topic
            output_path = ctx.work_dir / "thumbnail.jpg"
            
            try:
                url = await media_service.fetch_best_image_url(f"YouTube thumbnail for {prompt}")
                if url:
                    media_service.download_image(url, output_path)
                else:
                    media_service.placeholder_image(output_path)
            except Exception as e:
                logger.error("thumbnail_fetch_failed", extra={"error": str(e)})
                media_service.placeholder_image(output_path)
                
            # 3. Persist Artifacts
            if ctx.save_files and output_path.exists():
                key = artifact_key(ctx.business_model, ctx.job.id, "thumbnail.jpg")
                checksum = ctx.storage.checksum_file(output_path)
                ctx.storage.upload_file(output_path, key, content_type="image/jpeg")
                await add_artifact(
                    ctx.session,
                    ctx.job.id,
                    ArtifactKind.THUMBNAIL,
                    key,
                    checksum=checksum,
                )
            
            STAGE_DURATION.labels(stage="thumbnail").observe(time.perf_counter() - t0)
            
            # 4. Format-Aware Transition
            next_stg = get_next_stage_for_job(ctx.business_model, JobStage.THUMBNAIL)
            if next_stg is None:
                raise RuntimeError(f"Routing bug: Job {ctx.job.id} at stage {JobStage.THUMBNAIL} has no next stage in route.")
                
            await transition_stage(
                ctx.session, ctx.job, 
                to_stage=next_stg, 
                status=JobStatus.RUNNING, 
                message="Thumbnail complete."
            )
            logger.info("stage_thumbnail_complete", extra={"job_id": ctx.job.id})

            return {"thumbnail_path": str(output_path)}

        except Exception as e:
            logger.exception("stage_thumbnail_failed", extra={"job_id": ctx.job.id})
            await transition_stage(
                ctx.session, ctx.job, 
                to_stage=ctx.job.stage,
                status=JobStatus.FAILED, 
                message=f"Thumbnail error: {str(e)}"
            )
            raise
