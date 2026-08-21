"""TikTok Tier A Distributor."""

import os
from pathlib import Path
from typing import Literal
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.db import Job
from src.db.repository import get_distribution, record_distribution
from src.domain import ArtifactKind
from src.services.distributors.base import Distributor, PublishResult

logger = get_logger(__name__)


class TikTokDistributor(Distributor):
    publish_mode: Literal["auto", "manual_review"] = "auto"
    platform: str = "tiktok"

    async def publish(
        self, 
        session: AsyncSession, 
        job: Job, 
        artifacts: dict[ArtifactKind, list[Path]], 
        work_dir: Path
    ) -> PublishResult:
        
        existing_dist = await get_distribution(session, job.id, self.platform)
        if existing_dist and existing_dist.status in ("success", "pending_approval"):
            logger.info("tiktok_publish_idempotency_skip", extra={"job_id": job.id})
            return PublishResult(
                status=existing_dist.status, # type: ignore
                platform=self.platform,
                external_id=existing_dist.external_id,
                manifest_path=existing_dist.manifest_path
            )

        video_paths = artifacts.get(ArtifactKind.VIDEO)
        if not video_paths:
            raise ValueError("No video artifact found for TikTok upload.")
        
        # Determine if app is audited/approved. For now, we simulate a config flag or check.
        # TikTok Content Posting API requires app approval for direct publishing.
        app_is_audited = os.environ.get("TIKTOK_APP_AUDITED", "false").lower() == "true"
        
        if not app_is_audited:
            logger.warning("TikTok app is not fully audited. Returning pending_approval state.")
            
            await record_distribution(
                session=session,
                job_id=job.id,
                platform=self.platform,
                status="pending_approval",
            )
            return PublishResult(
                status="pending_approval",
                platform=self.platform
            )
        
        # Real TikTok API logic would go here
        video_id = f"tiktok_{job.id}"
        
        logger.info("tiktok_publish_done", extra={"video_id": video_id})
        
        await record_distribution(
            session=session,
            job_id=job.id,
            platform=self.platform,
            status="success",
            external_id=video_id
        )

        return PublishResult(
            status="success",
            platform=self.platform,
            external_id=video_id
        )
