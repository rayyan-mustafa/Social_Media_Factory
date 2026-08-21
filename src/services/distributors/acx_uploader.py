"""ACX Tier B Distributor."""

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


class ACXDistributor(Distributor):
    publish_mode: Literal["auto", "manual_review"] = "manual_review"
    platform: str = "acx"

    async def publish(
        self, 
        session: AsyncSession, 
        job: Job, 
        artifacts: dict[ArtifactKind, list[Path]], 
        work_dir: Path
    ) -> PublishResult:
        
        # Tier B: writes manifest and files to outbox
        outbox_dir = Path("/outbox/acx") / str(job.id)
        outbox_dir.mkdir(parents=True, exist_ok=True)
        
        audio_paths = artifacts.get(ArtifactKind.AUDIO)
        if not audio_paths:
            raise ValueError("No audio artifact found for ACX.")
            
        audio_path = audio_paths[0]
        dest_content = outbox_dir / "final.wav"
        shutil.copy2(audio_path, dest_content)
        
        manifest = {
            "job_id": job.id,
            "topic": job.topic,
            "niche": job.niche,
            "business_model": job.business_model,
            "file": "final.wav",
            "instructions": "Manual upload to ACX portal."
        }
        
        manifest_path = outbox_dir / "manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
            
        logger.info("acx_outbox_prepared", extra={"job_id": job.id, "manifest": str(manifest_path)})

        return PublishResult(
            status="success",
            platform=self.platform,
            manifest_path=str(manifest_path)
        )
