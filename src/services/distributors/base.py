"""Base interface for Distribution Hub."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal
from pydantic import BaseModel
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession

from src.db import Job
from src.domain import ArtifactKind


class PublishResult(BaseModel):
    status: Literal["success", "pending_approval", "failed"]
    platform: str
    external_id: str | None = None
    manifest_path: str | None = None


class Distributor(ABC):
    """
    Base class for all distributors (Tier A and Tier B).
    """

    # Every distributor must declare whether it is fully automated via API (Tier A)
    # or manual outbox manifest-drop (Tier B).
    publish_mode: Literal["auto", "manual_review"]
    platform: str

    @abstractmethod
    async def publish(
        self, 
        session: AsyncSession, 
        job: Job, 
        artifacts: dict[ArtifactKind, list[Path]], 
        work_dir: Path
    ) -> PublishResult:
        """
        Publishes the job's artifacts to the target platform.
        
        Tier A ("auto") implementations MUST natively enforce idempotency by checking the `distributions`
        table to see if a successful or pending publish already exists for this job+platform, and skip
        re-uploading if it does.
        
        Tier B ("manual_review") implementations should write a manifest.json and copy required assets
        to /outbox/{platform}/{job_id}/ and return a success PublishResult with `manifest_path` set.
        """
        pass
