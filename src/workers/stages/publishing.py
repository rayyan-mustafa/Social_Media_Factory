"""Publishing stage for distribution to platforms."""

import asyncio
from pathlib import Path
from typing import Any

from src.core.logging import get_logger
from src.db.repository import transition_stage, get_artifacts
from src.domain import ArtifactKind, JobStage, JobStatus
from src.workers.stages import StageContext
from src.workers.stages.routing import get_next_stage_for_job, should_skip_stage

from src.services.distributors.base import Distributor
from src.services.distributors.youtube import YouTubeDistributor
from src.services.distributors.wordpress import WordPressDistributor
from src.services.distributors.anchor_rss import AnchorRSSDistributor
from src.services.distributors.tiktok import TikTokDistributor
from src.services.distributors.instagram import InstagramDistributor
from src.services.distributors.kindle_direct import KDPDistributor
from src.services.distributors.acx_uploader import ACXDistributor
from src.services.distributors.teachable import TeachableDistributor

logger = get_logger(__name__)

# Map Business Models to lists of Distributor instances
DISTRIBUTOR_MAP: dict[str, list[Distributor]] = {
    "YouTube_Shorts": [YouTubeDistributor(), TikTokDistributor(), InstagramDistributor()],
    "Web_Series": [YouTubeDistributor()],
    "Sleep_Stories": [YouTubeDistributor()],
    "Podcast_Audio": [AnchorRSSDistributor()],
    "Radio_FM": [AnchorRSSDistributor()],
    "SEO_Blogs": [WordPressDistributor()],
    "EBooks_KDP": [KDPDistributor()],
    "Audiobooks_ACX": [ACXDistributor()],
    "Online_Courses_Teachable": [TeachableDistributor()],
}

class PublishingStage:
    """Publishes the final artifacts to the target platform(s)."""
    
    async def execute(self, ctx: StageContext, artifacts: dict[str, Any]) -> dict[str, Any]:
        if should_skip_stage(ctx.job.stage, JobStage.UPLOADING, ctx.business_model):
            logger.info("stage_publishing_skipped", extra={"job_id": ctx.job.id, "reason": f"stage is already {ctx.job.stage}"})
            return {}

        logger.info("stage_publishing_start", extra={"job_id": ctx.job.id, "model": ctx.business_model})
        
        try:
            distributors = DISTRIBUTOR_MAP.get(ctx.business_model, [])
            if not distributors:
                logger.warning("No distributors configured for model", extra={"model": ctx.business_model})
                # Proceed to transition to SUCCEEDED safely
                distributors = []

            # 1. Fetch artifacts needed for publishing based on model
            loaded_artifacts = await self._fetch_artifacts(ctx, artifacts)

            results = []
            
            # 2. Distribute to all targets sequentially (or concurrently if preferred, keeping sequential for simplicity)
            for distributor in distributors:
                result = await distributor.publish(ctx.session, ctx.job, loaded_artifacts, ctx.work_dir)
                results.append(result)

            # 3. Rollup Logic
            has_failures = any(r.status == "failed" for r in results)
            has_pending = any(r.status == "pending_approval" for r in results)
            has_manual = any(d.publish_mode == "manual_review" for d in distributors)

            if has_failures:
                failed_platforms = [r.platform for r in results if r.status == "failed"]
                raise RuntimeError(f"Publishing failed for platforms: {', '.join(failed_platforms)}")

            # Terminal Format-Aware Transition
            next_stg = get_next_stage_for_job(ctx.business_model, JobStage.UPLOADING)
            
            if next_stg is not None:
                # If there's another stage in the route (which shouldn't happen for publishing)
                rollup_status = JobStatus.RUNNING
                rollup_stage = next_stg
            else:
                # Terminal Status logic
                rollup_stage = JobStage.SUCCEEDED
                if has_pending:
                    rollup_status = JobStatus.AWAITING_PLATFORM_APPROVAL
                elif has_manual:
                    rollup_status = JobStatus.AWAITING_MANUAL_PUBLISH
                else:
                    rollup_status = JobStatus.SUCCEEDED

            await transition_stage(
                ctx.session, ctx.job, 
                to_stage=rollup_stage, 
                status=rollup_status, 
                message="Publishing complete."
            )

            logger.info("stage_publishing_complete", extra={"job_id": ctx.job.id, "final_status": str(rollup_status)})
            
            return {"publish_results": [r.model_dump() for r in results]}

        except Exception as e:
            logger.exception("stage_publishing_failed", extra={"job_id": ctx.job.id})
            await transition_stage(
                ctx.session, ctx.job, 
                to_stage=ctx.job.stage,
                status=JobStatus.FAILED, 
                message=f"Publishing error: {str(e)}"
            )
            raise

    async def _fetch_artifacts(self, ctx: StageContext, incoming_artifacts: dict[str, Any]) -> dict[ArtifactKind, list[Path]]:
        """Consolidates and downloads all necessary artifacts for the distributors."""
        result: dict[ArtifactKind, list[Path]] = {}
        
        # Determine what artifact kinds this business model needs
        if ctx.business_model in ("YouTube_Shorts", "Web_Series", "Sleep_Stories", "Online_Courses_Teachable"):
            kinds = [ArtifactKind.FINAL_VIDEO]
        elif ctx.business_model in ("Podcast_Audio", "Audiobooks_ACX", "Radio_FM"):
            kinds = [ArtifactKind.PODCAST_AUDIO, ArtifactKind.AUDIO]
        elif ctx.business_model in ("SEO_Blogs", "EBooks_KDP"):
            kinds = [ArtifactKind.TEXT]
        else:
            kinds = []
            
        for kind in kinds:
            db_artifacts = await get_artifacts(ctx.session, ctx.job.id, kind)
            paths = []
            for dba in db_artifacts:
                local_path = ctx.work_dir / dba.s3_key.split("/")[-1]
                if not local_path.exists():
                    ctx.storage.download_file(dba.s3_key, local_path)
                paths.append(local_path)
            
            # If no DB artifacts but we got a direct pass-through (e.g., immediate previous stage)
            if not paths:
                if kind == ArtifactKind.FINAL_VIDEO and "video_path" in incoming_artifacts:
                    paths = [Path(incoming_artifacts["video_path"])]
                elif kind in (ArtifactKind.PODCAST_AUDIO, ArtifactKind.AUDIO) and "master_audio_path" in incoming_artifacts:
                    paths = [Path(incoming_artifacts["master_audio_path"])]
                elif kind == ArtifactKind.TEXT and "markdown_path" in incoming_artifacts:
                    paths = [Path(incoming_artifacts["markdown_path"])]
                    
            if paths:
                # Simplify mapping to domain kinds, standardizing to a few keys
                if kind == ArtifactKind.FINAL_VIDEO:
                    result[ArtifactKind.VIDEO] = paths
                else:
                    result[kind] = paths
                    
        return result
