"""Multi-stage ARQ production pipeline — all 9 business models."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from src.core.config import get_settings
from src.core.logging import get_logger, setup_logging
from src.core.paths import persists_files
from src.db import get_session_factory
from src.db.repository import (
    get_job,
    transition_stage,
)
from src.domain import JobStage, JobStatus
from src.services.storage import ObjectStorage
from src.utils.alerts import send_discord_alert
from src.utils.metrics import (
    ACTIVE_JOBS,
    JOBS_FAILED,
    JOBS_SUCCEEDED,
)
from src.utils.webhooks import notify_job_event
from src.workers.dispatcher import get_stages_for_model
from src.workers.stages import StageContext

logger = get_logger(__name__)


async def _fail_job(session, job, message: str) -> None:
    await transition_stage(
        session,
        job,
        job.stage, # Keep the current stage so retries know where to resume
        status=JobStatus.FAILED,
        error=message,
        message=message,
    )
    JOBS_FAILED.inc()
    ACTIVE_JOBS.dec()
    await send_discord_alert(f"Job {job.id} failed: {message}")


async def run_pipeline(ctx: dict, job_id: int) -> dict:
    """Execute production content pipeline for a job using modular stages."""
    setup_logging()
    settings = get_settings()
    factory = get_session_factory()
    storage = ObjectStorage()
    storage.ensure_bucket()

    ACTIVE_JOBS.inc()
    async with factory() as session:
        job = await get_job(session, job_id)
        if job is None:
            ACTIVE_JOBS.dec()
            raise ValueError(f"Job {job_id} not found")

        if job.status == JobStatus.SUCCEEDED.value:
            ACTIVE_JOBS.dec()
            return {"job_id": job_id, "status": "already_succeeded"}

        job.attempt += 1
        await session.commit()

        if job.attempt > settings.max_job_attempts:
            await _fail_job(
                session, job, f"Exceeded max attempts ({settings.max_job_attempts})"
            )
            return {"job_id": job_id, "status": "failed", "error": "max_attempts"}

        model = job.business_model or "YouTube_Shorts"
        save_files = persists_files(model)

        work_dir = Path(tempfile.mkdtemp(prefix=f"job_{job_id}_"))
        stage_ctx = StageContext(
            job=job,
            session=session,
            settings=settings,
            storage=storage,
            work_dir=work_dir,
            redis=ctx.get("redis")
        )

        try:
            stages = get_stages_for_model(model)
            artifacts: dict[str, str] = {}

            for stage in stages:
                new_artifacts = await stage.execute(stage_ctx, artifacts)
                artifacts.update(new_artifacts)

            # Terminal stage transitions (like SUCCEEDED) are now handled natively by 
            # the terminal stage processor (e.g. publishing.py) to ensure correct routing.
            JOBS_SUCCEEDED.inc()
            ACTIVE_JOBS.dec()
            
            publish_id = artifacts.get("publish_id")
            final_key = artifacts.get("final_key")
            
            if model in ("YouTube_Shorts", "Web_Series") and publish_id:
                redis = stage_ctx.redis
                if redis:
                    await redis.sadd(
                        "youtube:pending_processing", f"{job_id}:{publish_id}"
                    )
            
            await notify_job_event(
                "job.succeeded",
                {
                    "job_id": job_id,
                    "final_key": final_key,
                    "publish_id": publish_id,
                    "business_model": model,
                    "persisted_folder": save_files,
                },
            )
            
            return {
                "job_id": job_id,
                "status": "succeeded",
                "final_key": final_key,
                "publish_id": publish_id,
                "business_model": model,
            }
            
        except Exception as exc:
            logger.exception("pipeline_failed", extra={"job_id": job_id})
            # Re-fetch job to ensure we have latest version from db
            job = await get_job(session, job_id)
            if job and job.attempt < settings.max_job_attempts:
                await transition_stage(
                    session,
                    job,
                    job.stage, # Preserve the current stage pointer for retry
                    status=JobStatus.QUEUED,
                    error=str(exc),
                    message=f"retry_scheduled: {exc}",
                )
                ACTIVE_JOBS.dec()
                raise
            if job:
                await _fail_job(session, job, str(exc))
            else:
                ACTIVE_JOBS.dec()
            raise
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

