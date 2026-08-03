"""Teachable Tier B Distributor."""

import json
import shutil
from pathlib import Path
from typing import Literal
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.db import Job
from src.domain import ArtifactKind
from src.services.distributors.base import Distributor, PublishResult

logger = get_logger(__name__)


class TeachableDistributor(Distributor):
    publish_mode: Literal["auto", "manual_review"] = "manual_review"
    platform: str = "teachable"

    async def publish(
        self, 
        session: AsyncSession, 
        job: Job, 
        artifacts: dict[ArtifactKind, list[Path]], 
        work_dir: Path
    ) -> PublishResult:
        
        # Tier B: writes manifest and files to outbox
        # Based on research, Teachable API does not expose course/lesson creation.
        outbox_dir = Path("/outbox/teachable") / str(job.id)
        outbox_dir.mkdir(parents=True, exist_ok=True)
        
        video_paths = artifacts.get(ArtifactKind.VIDEO)
        if not video_paths:
            raise ValueError("No video artifact found for Teachable.")
            
        video_path = video_paths[0]
        dest_content = outbox_dir / "course_video.mp4"
        shutil.copy2(video_path, dest_content)
        
        manifest = {
            "job_id": job.id,
            "topic": job.topic,
            "niche": job.niche,
            "business_model": job.business_model,
            "file": "course_video.mp4",
            "instructions": "Manual upload to Teachable course builder."
        }
        
        manifest_path = outbox_dir / "manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
            
        logger.info("teachable_outbox_prepared", extra={"job_id": job.id, "manifest": str(manifest_path)})

        return PublishResult(
            status="success",
            platform=self.platform,
            manifest_path=str(manifest_path)
        )
