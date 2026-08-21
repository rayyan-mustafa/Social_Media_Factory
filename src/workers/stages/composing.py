"""Video composition stage."""

import time
from pathlib import Path
from typing import Any

from src.core.logging import get_logger
from src.core.paths import artifact_key
from src.db.repository import add_artifact, transition_stage, get_artifacts
from src.domain import ArtifactKind, JobStage, JobStatus
from src.services.runpod_client import runpod_dispatch
from src.utils.metrics import STAGE_DURATION
from src.workers.stages import StageContext
from src.workers.stages.routing import get_next_stage_for_job, should_skip_stage
from src.services.financial.cost_tracker import record_cost, calculate_runpod_cost

logger = get_logger(__name__)

class ComposingStage:
    """Composes video from audio and image scenes."""
    
    async def execute(self, ctx: StageContext, artifacts: dict[str, Any]) -> dict[str, Any]:
        if should_skip_stage(ctx.job.stage, JobStage.COMPOSING, ctx.business_model):
            logger.info("stage_composing_skipped", extra={"job_id": ctx.job.id, "reason": f"stage is already {ctx.job.stage}"})
            db_artifacts = await get_artifacts(ctx.session, ctx.job.id, ArtifactKind.FINAL_VIDEO)
            if db_artifacts:
                local_path = ctx.work_dir / db_artifacts[0].s3_key.split("/")[-1]
                if not local_path.exists():
                    ctx.storage.download_file(db_artifacts[0].s3_key, local_path)
                return {"video_path": local_path}
            return {}

        logger.info("stage_composing_start", extra={"job_id": ctx.job.id})
        
        try:
            t0 = time.perf_counter()
            
            # 1. Load Prerequisites
            audio_paths = artifacts.get("audio_paths", [])
            media_paths = artifacts.get("media_paths", [])
            
            if not audio_paths:
                db_audio = await get_artifacts(ctx.session, ctx.job.id, ArtifactKind.AUDIO)
                for db_art in db_audio:
                    local_path = ctx.work_dir / db_art.s3_key.split("/")[-1]
                    if not local_path.exists():
                        ctx.storage.download_file(db_art.s3_key, local_path)
                    audio_paths.append(local_path)
                audio_paths.sort()
                
            if not media_paths:
                db_media = await get_artifacts(ctx.session, ctx.job.id, ArtifactKind.IMAGE)
                for db_art in db_media:
                    local_path = ctx.work_dir / db_art.s3_key.split("/")[-1]
                    if not local_path.exists():
                        ctx.storage.download_file(db_art.s3_key, local_path)
                    media_paths.append(local_path)
                media_paths.sort()
            
            if not audio_paths or not media_paths:
                raise ValueError(f"Composing stage requires audio and media artifacts. Got {len(audio_paths)} audio, {len(media_paths)} media.")
                
            if len(audio_paths) != len(media_paths):
                logger.warning("composing_stage_mismatch", extra={"audio_count": len(audio_paths), "media_count": len(media_paths)})

            # 2. Execute Business Logic via RunPod dispatch
            render_type = "vertical"
            if ctx.business_model in ("YouTube_Shorts"):
                render_type = "vertical"
            elif ctx.business_model in ("Web_Series"):
                render_type = "horizontal"
            elif ctx.business_model in ("Online_Courses_Teachable"):
                render_type = "educational"
                
            output_path = ctx.work_dir / "final.mp4"
            
            await runpod_dispatch(
                render_type=render_type,
                audio_paths=[str(p) for p in audio_paths],
                media_paths=[str(p) for p in media_paths],
                output_path=str(output_path),
                storage=ctx.storage,
                job_id=ctx.job.id,
                business_model=ctx.business_model,
                settings=ctx.settings
            )

            # 3. Persist Artifacts
            if ctx.save_files and output_path.exists():
                key = artifact_key(ctx.business_model, ctx.job.id, "final.mp4")
                checksum = ctx.storage.checksum_file(output_path)
                ctx.storage.upload_file(output_path, key, content_type="video/mp4")
                await add_artifact(
                    ctx.session,
                    ctx.job.id,
                    ArtifactKind.FINAL_VIDEO,
                    key,
                    checksum=checksum,
                )
            
            STAGE_DURATION.labels(stage="composing").observe(time.perf_counter() - t0)
            
            # Record RunPod GPU cost (e.g. 5 minutes)
            runpod_cost = calculate_runpod_cost(5 * 60)
            await record_cost(ctx.session, ctx.job.id, "runpod", runpod_cost)
            
            # 4. Format-Aware Transition
            next_stg = get_next_stage_for_job(ctx.business_model, JobStage.COMPOSING)
            if next_stg is None:
                raise RuntimeError(f"Routing bug: Job {ctx.job.id} at stage {JobStage.COMPOSING} has no next stage in route.")
                
            await transition_stage(
                ctx.session, ctx.job, 
                to_stage=next_stg, 
                status=JobStatus.RUNNING, 
                message="Composing complete."
            )
            logger.info("stage_composing_complete", extra={"job_id": ctx.job.id})

            return {"video_path": output_path}

        except Exception as e:
            logger.exception("stage_composing_failed", extra={"job_id": ctx.job.id})
            await transition_stage(
                ctx.session, ctx.job, 
                to_stage=ctx.job.stage,
                status=JobStatus.FAILED, 
                message=f"Composing error: {str(e)}"
            )
            raise
