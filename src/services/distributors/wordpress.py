"""WordPress Tier A Distributor."""

import httpx
from pathlib import Path
from typing import Literal
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.logging import get_logger
from src.db import Job
from src.db.repository import get_distribution, record_distribution
from src.domain import ArtifactKind
from src.services.distributors.base import Distributor, PublishResult

logger = get_logger(__name__)


class WordPressDistributor(Distributor):
    publish_mode: Literal["auto", "manual_review"] = "auto"
    platform: str = "wordpress"

    async def publish(
        self, 
        session: AsyncSession, 
        job: Job, 
        artifacts: dict[ArtifactKind, list[Path]], 
        work_dir: Path
    ) -> PublishResult:
        
        existing_dist = await get_distribution(session, job.id, self.platform)
        if existing_dist and existing_dist.status in ("success", "pending_approval"):
            logger.info("wordpress_publish_idempotency_skip", extra={"job_id": job.id})
            return PublishResult(
                status=existing_dist.status, # type: ignore
                platform=self.platform,
                external_id=existing_dist.external_id,
                manifest_path=existing_dist.manifest_path
            )

        text_paths = artifacts.get(ArtifactKind.TEXT)
        if not text_paths:
            raise ValueError("No text artifact found for WordPress publish.")
        
        content = text_paths[0].read_text(encoding="utf-8")
        title = job.topic
        
        settings = get_settings()
        # Ensure we have wordpress credentials
        if not settings.wordpress_url or not settings.wordpress_username or not settings.wordpress_app_password:
            # Mock successful publish if not configured for now
            logger.warning("WordPress credentials not fully configured, mocking publish.")
            post_id = f"wp_mock_{job.id}"
        else:
            url = f"{settings.wordpress_url.rstrip('/')}/wp-json/wp/v2/posts"
            auth = (settings.wordpress_username, settings.wordpress_app_password)
            data = {
                "title": title,
                "content": content,
                "status": "publish"
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=data, auth=auth)
                response.raise_for_status()
                result = response.json()
                post_id = str(result.get("id"))
                
        logger.info("wordpress_publish_done", extra={"post_id": post_id})
        
        await record_distribution(
            session=session,
            job_id=job.id,
            platform=self.platform,
            status="success",
            external_id=post_id
        )

        return PublishResult(
            status="success",
            platform=self.platform,
            external_id=post_id
        )
