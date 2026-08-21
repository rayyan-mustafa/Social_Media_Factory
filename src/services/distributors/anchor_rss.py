"""Anchor RSS Tier A Distributor."""

import os
from pathlib import Path
from typing import Literal
from sqlalchemy.ext.asyncio import AsyncSession
import xml.etree.ElementTree as ET

from src.core.config import get_settings
from src.core.logging import get_logger
from src.db import Job
from src.db.repository import get_distribution, record_distribution
from src.domain import ArtifactKind
from src.services.distributors.base import Distributor, PublishResult

logger = get_logger(__name__)


class AnchorRSSDistributor(Distributor):
    publish_mode: Literal["auto", "manual_review"] = "auto"
    platform: str = "anchor_rss"

    async def publish(
        self, 
        session: AsyncSession, 
        job: Job, 
        artifacts: dict[ArtifactKind, list[Path]], 
        work_dir: Path
    ) -> PublishResult:
        
        existing_dist = await get_distribution(session, job.id, self.platform)
        if existing_dist and existing_dist.status in ("success", "pending_approval"):
            logger.info("anchor_rss_publish_idempotency_skip", extra={"job_id": job.id})
            return PublishResult(
                status=existing_dist.status, # type: ignore
                platform=self.platform,
                external_id=existing_dist.external_id,
                manifest_path=existing_dist.manifest_path
            )

        audio_paths = artifacts.get(ArtifactKind.AUDIO)
        if not audio_paths:
            raise ValueError("No audio artifact found for Anchor RSS feed.")
        
        audio_file = audio_paths[0]
        title = job.topic
        
        # Here we mock adding to an RSS feed. In a real system we would read an existing feed.xml,
        # append a new <item>, and upload to a CDN. 
        feed_path = Path("/tmp/feed.xml")
        
        logger.info("anchor_rss_publish_done", extra={"feed_path": str(feed_path)})
        
        item_id = f"anchor_{job.id}"
        await record_distribution(
            session=session,
            job_id=job.id,
            platform=self.platform,
            status="success",
            external_id=item_id
        )

        return PublishResult(
            status="success",
            platform=self.platform,
            external_id=item_id
        )
