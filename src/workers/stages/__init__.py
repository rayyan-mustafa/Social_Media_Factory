"""Pipeline stage base classes and interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings
from src.db import Job
from src.services.storage import ObjectStorage


@dataclass
class StageContext:
    """Context passed to every pipeline stage."""
    
    job: Job
    session: AsyncSession
    settings: Settings
    storage: ObjectStorage
    work_dir: Path
    
    redis: Any | None = None
    
    # Pre-calculated flags based on the business model and job config
    business_model: str = field(init=False)
    save_files: bool = field(init=False)
    
    def __post_init__(self):
        self.business_model = self.job.business_model
        
        from src.core.paths import persists_files
        self.save_files = persists_files(self.business_model)


class StageProcessor(Protocol):
    """Protocol for a pipeline stage processor."""
    
    async def execute(self, ctx: StageContext, artifacts: dict[str, Any]) -> dict[str, Any]:
        """Execute this stage of the pipeline.
        
        Args:
            ctx: The execution context containing session, job, and config.
            artifacts: The artifacts produced by previous stages.
            
        Returns:
            A dictionary containing the results or artifacts produced by this stage.
            This dictionary will be passed along to subsequent stages if needed.
            
        Raises:
            Exception: If the stage fails and needs to be retried or aborted.
        """
        ...
