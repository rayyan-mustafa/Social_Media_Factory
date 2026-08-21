"""Kindle Direct Publishing Tier B Distributor."""

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


class KDPDistributor(Distributor):
    publish_mode: Literal["auto", "manual_review"] = "manual_review"
    platform: str = "kdp"

    async def publish(
        self, 
        session: AsyncSession, 
        job: Job, 
        artifacts: dict[ArtifactKind, list[Path]], 
        work_dir: Path
    ) -> PublishResult:
        
        # Tier B: writes manifest and files to outbox
        outbox_dir = Path("/outbox/kdp") / str(job.id)
        outbox_dir.mkdir(parents=True, exist_ok=True)
        
        text_paths = artifacts.get(ArtifactKind.TEXT)
        if not text_paths:
            raise ValueError("No text artifact found for KDP.")
            
        content_path = text_paths[0]
        dest_content = outbox_dir / "content.pdf" # Assuming it was converted to PDF or epub
        shutil.copy2(content_path, dest_content)
        
        manifest = {
            "job_id": job.id,
            "topic": job.topic,
            "niche": job.niche,
            "business_model": job.business_model,
            "file": "content.pdf",
            "instructions": "Manual upload to KDP portal."
        }
        
        manifest_path = outbox_dir / "manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
            
        logger.info("kdp_outbox_prepared", extra={"job_id": job.id, "manifest": str(manifest_path)})

        # Note: Tier B manual drops return 'success' but trigger an AWAITING_MANUAL_PUBLISH rollup
        # at the publishing stage level because of their `publish_mode`.
        return PublishResult(
            status="success",
            platform=self.platform,
            manifest_path=str(manifest_path)
        )
