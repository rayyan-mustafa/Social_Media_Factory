"""FastAPI application: health, metrics, jobs."""

from __future__ import annotations

from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, HTTPException, Query, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.responses import Response

from src import __version__
from src.core.config import get_settings
from src.core.logging import get_logger, setup_logging
from src.db import Job, JobArtifact, get_db_session, get_engine
from src.db.repository import create_job, get_job, get_job_by_idempotency, list_jobs
from src.domain import ArtifactKind, ArtifactRead, JobCreate, JobRead, JobStage, JobStatus
from src.utils.metrics import JOBS_CREATED

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    app.state.redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    get_engine()
    logger.info("api_started", extra={"version": __version__})
    yield
    await app.state.redis.close()


app = FastAPI(
    title="YouTube Automation Engine",
    version=__version__,
    description="Backend/platform API for the automated documentary content pipeline.",
    lifespan=lifespan,
)


def job_to_read(job: Job) -> JobRead:
    yt_id = None
    # Avoid async lazy-load (MissingGreenlet) when relationship was not eager-loaded
    state = sa_inspect(job)
    if "youtube_upload" not in state.unloaded:
        upload = job.youtube_upload
        if upload is not None:
            yt_id = upload.video_id
    return JobRead(
        id=job.id,
        topic=job.topic,
        niche=job.niche,
        status=JobStatus(job.status),
        stage=JobStage(job.stage),
        attempt=job.attempt,
        idempotency_key=job.idempotency_key,
        error=job.error,
        cost_cents=job.cost_cents,
        youtube_video_id=yt_id,
    )


@app.get("/health")
async def health(session: AsyncSession = Depends(get_db_session)) -> dict:
    settings = get_settings()
    db_ok = False
    redis_ok = False
    try:
        await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        logger.exception("health_db_failed")
    try:
        pong = await app.state.redis.ping()
        redis_ok = bool(pong)
    except Exception:
        logger.exception("health_redis_failed")
    status_code = "ok" if db_ok and redis_ok else "degraded"
    return {
        "status": status_code,
        "version": __version__,
        "env": settings.app_env,
        "checks": {"database": db_ok, "redis": redis_ok},
    }


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/v1/jobs", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED)
async def create_job_endpoint(
    body: JobCreate,
    session: AsyncSession = Depends(get_db_session),
) -> JobRead:
    if body.idempotency_key:
        existing = await get_job_by_idempotency(session, body.idempotency_key)
        if existing is not None:
            return job_to_read(existing)

    job = await create_job(
        session,
        topic=body.topic,
        niche=body.niche,
        idempotency_key=body.idempotency_key,
        upload_to_youtube=body.upload_to_youtube,
    )
    await app.state.redis.enqueue_job("run_pipeline", job.id)
    JOBS_CREATED.inc()
    logger.info("job_enqueued", extra={"job_id": job.id})
    return job_to_read(job)


@app.get("/v1/jobs", response_model=list[JobRead])
async def list_jobs_endpoint(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> list[JobRead]:
    jobs = await list_jobs(session, limit=limit, offset=offset)
    return [job_to_read(j) for j in jobs]


@app.get("/v1/jobs/{job_id}", response_model=JobRead)
async def get_job_endpoint(
    job_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> JobRead:
    result = await session.execute(
        select(Job).options(selectinload(Job.youtube_upload)).where(Job.id == job_id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job_to_read(job)


@app.get("/v1/jobs/{job_id}/artifacts", response_model=list[ArtifactRead])
async def list_artifacts(
    job_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> list[ArtifactRead]:
    job = await get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    result = await session.execute(
        select(JobArtifact).where(JobArtifact.job_id == job_id).order_by(JobArtifact.id)
    )
    arts = result.scalars().all()
    return [
        ArtifactRead(
            id=a.id,
            kind=ArtifactKind(a.kind),
            s3_key=a.s3_key,
            checksum=a.checksum,
            meta=a.meta or {},
        )
        for a in arts
    ]


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run("src.api.main:app", host=settings.api_host, port=settings.api_port, reload=False)
