"""Media retrieval stage processor."""

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

class MediaStage:
    """Retrieves media (images) for each scene in the script."""
    
    async def execute(self, ctx: StageContext, artifacts: dict[str, Any]) -> dict[str, Any]:
        if should_skip_stage(ctx.job.stage, JobStage.MEDIA, ctx.business_model):
            logger.info("stage_media_skipped", extra={"job_id": ctx.job.id, "reason": f"stage is already {ctx.job.stage}"})
            
            # Fetch already completed artifacts to pass downstream
            media_paths = []
            db_artifacts = await get_artifacts(ctx.session, ctx.job.id, ArtifactKind.IMAGE)
            for db_art in db_artifacts:
                local_path = ctx.work_dir / db_art.s3_key.split("/")[-1]
                if not local_path.exists():
                    ctx.storage.download_file(db_art.s3_key, local_path)
                media_paths.append(local_path)
            media_paths.sort()
            return {"media_paths": media_paths}

        logger.info("stage_media_start", extra={"job_id": ctx.job.id})
        
        try:
            t0 = time.perf_counter()
            media_service = MediaRetrievalService(use_clip=True)
            
            # 1. Load Prerequisites
            script: ScriptPayload | None = artifacts.get("script")
            if not script and ctx.job.script_json:
                script = ScriptPayload(**ctx.job.script_json)

            if not script:
                raise ValueError("Media stage requires 'script' artifact")

            # 2. Execute Business Logic
            media_paths = []
            for scene in script.scenes:
                # Scene visual_query handles what visual to retrieve
                visual_prompt = scene.visual_query or ctx.job.topic
                
                output_path = ctx.work_dir / f"scene_{scene.index:03d}.jpg"
                try:
                    url = await media_service.fetch_best_image_url(visual_prompt)
                    if url:
                        media_service.download_image(url, output_path)
                    else:
                        media_service.placeholder_image(output_path)
                except Exception as e:
                    logger.error("media_fetch_failed", extra={"scene_idx": scene.index, "error": str(e)})
                    media_service.placeholder_image(output_path)
                    
                media_paths.append(output_path)
                
                # 3. Persist Artifacts
                if ctx.save_files:
                    key = artifact_key(ctx.business_model, ctx.job.id, f"images/scene_{scene.index:03d}.jpg")
                    checksum = ctx.storage.checksum_file(output_path)
                    ctx.storage.upload_file(output_path, key, content_type="image/jpeg")
                    await add_artifact(
                        ctx.session,
                        ctx.job.id,
                        ArtifactKind.IMAGE,
                        key,
                        checksum=checksum,
                        meta={"scene": scene.index},
                    )
            
            STAGE_DURATION.labels(stage="media").observe(time.perf_counter() - t0)
            
            # 4. Format-Aware Transition
            next_stg = get_next_stage_for_job(ctx.business_model, JobStage.MEDIA)
            if next_stg is None:
                raise RuntimeError(f"Routing bug: Job {ctx.job.id} at stage {JobStage.MEDIA} has no next stage in route.")
                
            await transition_stage(
                ctx.session, ctx.job, 
                to_stage=next_stg, 
                status=JobStatus.RUNNING, 
                message="Media complete."
            )
            logger.info("stage_media_complete", extra={"job_id": ctx.job.id})

            return {"media_paths": media_paths}

        except Exception as e:
            logger.exception("stage_media_failed", extra={"job_id": ctx.job.id})
            await transition_stage(
                ctx.session, ctx.job, 
                to_stage=ctx.job.stage,
                status=JobStatus.FAILED, 
                message=f"Media error: {str(e)}"
            )
            raise
