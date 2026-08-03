"""Standalone Watchdog Microservice."""

import asyncio
import json
import os
import subprocess
import time
from datetime import datetime, timezone

from sqlalchemy import select

from src.core.config import get_settings
from src.core.logging import get_logger
from src.db import get_session_factory, Distribution
from src.db.repository import (
    delete_heavy_artifacts,
    get_jobs_by_status,
    get_oldest_completed_jobs,
    transition_stage,
)
from src.domain import JobStage, JobStatus
from src.services.notifications import dispatch_ledger
from src.services.storage import ObjectStorage
from src.services.watchdog import run_watchdog_benchmark
from src.services.distributors.youtube import YouTubeDistributor
from src.services.distributors.wordpress import WordPressDistributor

logger = get_logger(__name__)

# Map business model to worker container name
MODEL_TO_CONTAINER = {
    "YouTube_Shorts": "youtube_automation-worker_youtube_shorts-1",
    "Podcast_Audio": "youtube_automation-worker_podcast_audio-1",
    "SEO_Blogs": "youtube_automation-worker_seo_blogs-1",
    "Web_Series": "youtube_automation-worker_web_series-1",
    "Radio_FM": "youtube_automation-worker_radio_fm-1",
    "EBooks_KDP": "youtube_automation-worker_ebooks_kdp-1",
    "Audiobooks_ACX": "youtube_automation-worker_audiobooks_acx-1",
    "Sleep_Stories": "youtube_automation-worker_sleep_stories-1",
    "Online_Courses_Teachable": "youtube_automation-worker_online_courses-1",
}


async def restart_worker_container(justification: str, business_model: str | None = None):
    """Restart specific worker containers via Docker socket, or all if model is None."""
    targets = [MODEL_TO_CONTAINER[business_model]] if business_model and business_model in MODEL_TO_CONTAINER else list(MODEL_TO_CONTAINER.values())
    
    logger.error(
        "watchdog_general_intervention",
        extra={"msg": f"Restarting worker containers: {targets}", "justification": justification},
    )
    restarted: list[str] = []
    for name in targets:
        try:
            subprocess.run(
                [
                    "curl",
                    "-s",
                    "--unix-socket",
                    "/var/run/docker.sock",
                    "-X",
                    "POST",
                    f"http://localhost/containers/{name}/restart",
                ],
                check=False,
            )
            restarted.append(name)
        except Exception as e:
            logger.error(
                "watchdog_docker_restart_failed",
                extra={"error": str(e), "container": name},
            )
    await dispatch_ledger(
        "GENERAL INTERVENTION: Workers Restarted",
        {
            "Action": f"Hard Restart via Docker Socket (Targets: {len(targets)})",
            "Containers": ", ".join(restarted),
            "Justification": justification,
        },
    )


async def check_stale_jobs(db_factory):
    """Evaluate stuck jobs and apply rules based on status."""
    async with db_factory() as session:
        active_statuses = [
            JobStatus.FAILED.value,
            JobStatus.AWAITING_PLATFORM_APPROVAL.value,
            JobStatus.AWAITING_MANUAL_PUBLISH.value,
            JobStatus.RUNNING.value,
            JobStatus.QUEUED.value,
        ]
        
        active_jobs = await get_jobs_by_status(session, active_statuses)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        for job in active_jobs:
            elapsed_hours = (now - job.updated_at).total_seconds() / 3600
            
            if job.status == JobStatus.FAILED.value:
                # 6 hours without ARQ retrying
                if elapsed_hours > 6.0:
                    justification = f"Job {job.id} stuck in FAILED for > 6 hours. Triggering granular restart."
                    logger.error("watchdog_stuck_failed_job", extra={"job_id": job.id, "hours": elapsed_hours})
                    await restart_worker_container(justification, job.business_model)
                    
            elif job.status == JobStatus.AWAITING_PLATFORM_APPROVAL.value:
                # Expected state, e.g. TikTok review
                if elapsed_hours > 72.0: # 3 days
                    logger.warning("watchdog_prolonged_platform_approval", extra={"job_id": job.id, "hours": elapsed_hours})
                    await dispatch_ledger("WARNING: Prolonged Platform Approval", {"Job ID": job.id, "Hours": f"{elapsed_hours:.1f}"})
                    
            elif job.status == JobStatus.AWAITING_MANUAL_PUBLISH.value:
                # Expected state, manual cadence
                if elapsed_hours > (30 * 24): # 30 days
                    logger.warning("watchdog_overdue_manual_publish", extra={"job_id": job.id, "hours": elapsed_hours})
                    await dispatch_ledger("WARNING: Overdue Manual Publish", {"Job ID": job.id, "Days Overdue": f"{(elapsed_hours/24 - 30):.1f}"})
                    
            elif job.status in (JobStatus.RUNNING.value, JobStatus.QUEUED.value):
                # 2 hours without transition is a hung job
                if elapsed_hours > 2.0:
                    justification = f"Job {job.id} hung in {job.status} for > 2 hours. Forcing FAILED and restarting worker."
                    logger.error("watchdog_hung_job", extra={"job_id": job.id, "status": job.status, "hours": elapsed_hours})
                    await transition_stage(
                        session, job, 
                        to_stage=JobStage(job.stage), 
                        status=JobStatus.FAILED,
                        error="Watchdog: Job hung for > 2 hours"
                    )
                    await restart_worker_container(justification, job.business_model)


async def check_distribution_monitoring(db_factory):
    """Monitor Tier A distribution statuses."""
    youtube = YouTubeDistributor()
    wordpress = WordPressDistributor()
    
    async with db_factory() as session:
        # Check pending distributions
        result = await session.execute(
            select(Distribution).where(Distribution.status == "pending_processing")
        )
        pending = list(result.scalars().all())
        
        for dist in pending:
            if dist.platform == "youtube":
                try:
                    status = youtube.check_processing_status(dist.external_id)
                    logger.info("youtube_processing_status", extra={"video_id": dist.external_id, "status": status})
                    if status == "succeeded":
                        dist.status = "succeeded"
                        await session.commit()
                    elif status in ("failed", "rejected"):
                        # We don't enforce re-encode directly here anymore, just record failure
                        dist.status = "failed"
                        await session.commit()
                        await dispatch_ledger("GENERAL INTERVENTION: YouTube Failure", {"Video ID": dist.external_id, "Job ID": dist.job_id})
                except Exception as e:
                    logger.error("youtube_processing_check_failed", extra={"error": str(e), "video_id": dist.external_id})
            
            elif dist.platform == "wordpress":
                # Polling WP status if applicable
                pass


from src.domain import BUSINESS_MODELS
from src.services.financial.budget_guard import _get_rolling_24h_spend, DAILY_SPENDING_CAP_CENTS
from src.services.financial.roi_calculator import get_trailing_30_day_roi, MIN_ROI_THRESHOLD

async def check_financial_alerts(db_factory):
    """Evaluate financial bounds (cost/revenue) from Module 9."""
    async with db_factory() as session:
        for model in BUSINESS_MODELS:
            try:
                # 1. Daily Spend Alert
                spend = await _get_rolling_24h_spend(session, model)
                if spend > DAILY_SPENDING_CAP_CENTS:
                    logger.warning("financial_alert_daily_cap", extra={"model": model, "spend": spend})
                    await dispatch_ledger("WARNING: Financial Alert", {"Model": model, "Reason": f"Daily cap breached: {spend} cents"})
                
                # 2. ROI Alert
                roi = await get_trailing_30_day_roi(session, model)
                if isinstance(roi, float) and roi < MIN_ROI_THRESHOLD:
                    logger.warning("financial_alert_roi", extra={"model": model, "roi": roi})
                    await dispatch_ledger("WARNING: Financial Alert", {"Model": model, "Reason": f"ROI dropped below threshold: {roi:.2%}"})
            except Exception as e:
                logger.error("financial_alert_evaluation_failed", extra={"model": model, "error": str(e)})


async def main():
    logger.info("Watchdog General Daemon initialized. Monitoring System Health.")
    settings = get_settings()

    benchmark_file = os.path.join(os.path.dirname(__file__), "..", "..", "config", "benchmarks.json")
    with open(benchmark_file) as f:
        benchmarks = json.load(f)

    db_factory = get_session_factory()

    while True:
        try:
            # 1. Run Standard Benchmarks
            try:
                await run_watchdog_benchmark()
            except Exception as e:
                logger.error("watchdog_benchmark_evaluation_failed", extra={"error": str(e)})
            
            # 2. Check Stuck Jobs
            await check_stale_jobs(db_factory)
            
            # 3. Check Distribution Monitoring (Tier A only)
            await check_distribution_monitoring(db_factory)
            
            # 4. Financial Alerts
            await check_financial_alerts(db_factory)

            # 5. Disk Space Management
            try:
                import shutil
                disk_limit = benchmarks.get("disk", {}).get("max_capacity_percent", 50.0)
                emergency_limit = 90.0
                
                usage = shutil.disk_usage("/")
                used_percent = (usage.used / usage.total) * 100
                
                if used_percent > emergency_limit:
                    justification = f"Disk at critical {used_percent:.1f}%. Initiating emergency purge."
                    logger.critical("SYSTEM_HANG_PREVENTION", extra={"justification": justification})
                    
                    storage = ObjectStorage()
                    async with db_factory() as session:
                        old_jobs = await get_oldest_completed_jobs(session, limit=50)
                        target_percent = disk_limit - 10.0
                        
                        for old_job in old_jobs:
                            usage = shutil.disk_usage("/")
                            current_used = (usage.used / usage.total) * 100
                            if current_used <= target_percent:
                                break
                                
                            job_id = old_job.id
                            storage.delete_prefix(f"jobs/{job_id}/")
                            await delete_heavy_artifacts(session, job_id)
                            
                            await dispatch_ledger(
                                "CRITICAL INTERVENTION: Emergency Disk Purge", 
                                {"Action": f"Wiped artifacts for Job {job_id}"}
                            )
                            
                elif used_percent > disk_limit:
                    storage = ObjectStorage()
                    async with db_factory() as session:
                        old_jobs = await get_oldest_completed_jobs(session, limit=10)
                        for old_job in old_jobs:
                            usage = shutil.disk_usage("/")
                            current_used = (usage.used / usage.total) * 100
                            if current_used <= disk_limit - 5.0:
                                break
                                
                            job_id = old_job.id
                            storage.delete_prefix(f"jobs/{job_id}/")
                            await delete_heavy_artifacts(session, job_id)
            except Exception as e:
                logger.error("watchdog_disk_monitor_failed", extra={"error": str(e)})
                        
        except Exception as e:
            logger.error("watchdog_daemon_crash_averted", extra={"error": str(e)})
        
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
