"""Job persistence helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db import Job, JobArtifact, JobEvent, YouTubeUpload
from src.domain import DIRECT_UPLOAD_MODELS, ArtifactKind, JobStage, JobStatus


async def get_job(session: AsyncSession, job_id: int) -> Job | None:
    return await session.get(Job, job_id)


async def get_job_by_idempotency(session: AsyncSession, key: str) -> Job | None:
    result = await session.execute(select(Job).where(Job.idempotency_key == key))
    return result.scalar_one_or_none()


async def create_job(
    session: AsyncSession,
    *,
    topic: str,
    niche: str,
    business_model: str = "YouTube_Shorts",
    idempotency_key: str | None = None,
    upload_to_youtube: bool = False,
) -> Job:
    if business_model in DIRECT_UPLOAD_MODELS:
        upload_to_youtube = True
    job = Job(
        topic=topic,
        niche=niche,
        business_model=business_model,
        status=JobStatus.QUEUED.value,
        stage=JobStage.QUEUED.value,
        attempt=0,
        idempotency_key=idempotency_key,
        upload_to_youtube=upload_to_youtube,
        cost_cents=0,
    )
    session.add(job)
    await session.flush()
    session.add(
        JobEvent(job_id=job.id, from_stage=None, to_stage=JobStage.QUEUED.value, message="created")
    )
    await session.commit()
    await session.refresh(job)
    return job


async def transition_stage(
    session: AsyncSession,
    job: Job,
    to_stage: JobStage,
    *,
    status: JobStatus | None = None,
    message: str | None = None,
    error: str | None = None,
) -> None:
    from src.domain import can_transition

    from_stage = job.stage
    if not can_transition(from_stage, to_stage):
        raise ValueError(f"Illegal stage transition {from_stage!r} -> {to_stage.value!r}")
    job.stage = to_stage.value
    if status is not None:
        job.status = status.value
    if error is not None:
        job.error = error
    session.add(
        JobEvent(
            job_id=job.id,
            from_stage=from_stage,
            to_stage=to_stage.value,
            message=message or error,
        )
    )
    await session.commit()
    await session.refresh(job)


async def add_artifact(
    session: AsyncSession,
    job_id: int,
    kind: ArtifactKind,
    s3_key: str,
    checksum: str | None = None,
    meta: dict[str, Any] | None = None,
) -> JobArtifact:
    art = JobArtifact(
        job_id=job_id,
        kind=kind.value,
        s3_key=s3_key,
        checksum=checksum,
        meta=meta or {},
    )
    session.add(art)
    await session.commit()
    await session.refresh(art)
    return art


async def record_youtube_upload(
    session: AsyncSession,
    job_id: int,
    video_id: str,
    privacy: str,
) -> YouTubeUpload:
    row = YouTubeUpload(job_id=job_id, video_id=video_id, privacy=privacy)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def list_jobs(session: AsyncSession, limit: int = 50, offset: int = 0) -> list[Job]:
    result = await session.execute(
        select(Job).order_by(Job.id.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


async def get_latest_job_time(session: AsyncSession, business_model: str) -> datetime | None:
    result = await session.execute(
        select(Job.created_at)
        .where(Job.business_model == business_model)
        .order_by(Job.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_business_model_stats(
    session: AsyncSession, business_model: str, since: datetime
) -> dict[str, int]:
    result = await session.execute(
        select(Job.status, func.count(Job.id))
        .where(Job.business_model == business_model, Job.created_at >= since)
        .group_by(Job.status)
    )
    stats = {"succeeded": 0, "failed": 0, "queued": 0, "running": 0}
    for status, count in result.all():
        if status in stats:
            stats[status] = count
    return stats


async def get_oldest_completed_jobs(session: AsyncSession, limit: int = 5) -> list[Job]:
    """Get the oldest completed (SUCCEEDED or FAILED) jobs for disk cleanup."""
    result = await session.execute(
        select(Job)
        .where(Job.status.in_([JobStatus.SUCCEEDED.value, JobStatus.FAILED.value]))
        .order_by(Job.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def delete_heavy_artifacts(session: AsyncSession, job_id: int) -> None:
    """Delete database records for heavy artifacts (video, audio, images)."""
    heavy_kinds = [
        ArtifactKind.FINAL_VIDEO.value,
        ArtifactKind.SCENE_VIDEO.value,
        ArtifactKind.AUDIO.value,
        ArtifactKind.IMAGE.value,
        ArtifactKind.PODCAST_AUDIO.value
    ]
    await session.execute(
        delete(JobArtifact).where(
            JobArtifact.job_id == job_id,
            JobArtifact.kind.in_(heavy_kinds)
        )
    )
    await session.commit()
