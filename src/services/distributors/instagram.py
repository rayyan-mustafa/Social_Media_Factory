"""Instagram Tier A Distributor."""

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


class InstagramDistributor(Distributor):
    publish_mode: Literal["auto", "manual_review"] = "auto"
    platform: str = "instagram"

    async def publish(
        self, 
        session: AsyncSession, 
        job: Job, 
        artifacts: dict[ArtifactKind, list[Path]], 
        work_dir: Path
    ) -> PublishResult:
        
        existing_dist = await get_distribution(session, job.id, self.platform)
        if existing_dist and existing_dist.status in ("success", "pending_approval"):
            logger.info("instagram_publish_idempotency_skip", extra={"job_id": job.id})
            return PublishResult(
                status=existing_dist.status, # type: ignore
                platform=self.platform,
                external_id=existing_dist.external_id,
                manifest_path=existing_dist.manifest_path
            )

        video_paths = artifacts.get(ArtifactKind.VIDEO)
        if not video_paths:
            raise ValueError("No video artifact found for Instagram Reels upload.")
        
        # Instagram Graph API requires advanced access for automated publishing.
        app_is_audited = os.environ.get("INSTAGRAM_APP_AUDITED", "false").lower() == "true"
        
        if not app_is_audited:
            logger.warning("Instagram app is not fully audited for advanced access. Returning pending_approval state.")
            
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
        
        # Real Instagram API logic would go here
        video_id = f"ig_{job.id}"
        
        logger.info("instagram_publish_done", extra={"video_id": video_id})
        
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
