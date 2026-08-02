"""Standalone Watchdog Microservice."""

import asyncio
import json
import os
import subprocess
import time

from arq.connections import RedisSettings, create_pool

from src.core.config import get_settings
from src.core.logging import get_logger
from src.db import get_session_factory
from src.db.repository import (
    delete_heavy_artifacts,
    get_job,
    get_oldest_completed_jobs,
    transition_stage,
)
from src.domain import JobStage, JobStatus
from src.services.notifications import dispatch_ledger
from src.services.storage import ObjectStorage
from src.services.watchdog import run_watchdog_benchmark
from src.services.youtube_uploader import YouTubeUploader

logger = get_logger(__name__)

# Compose project workers — all 9 production model queues
WORKER_CONTAINERS = (
    "youtube_automation-worker_youtube_shorts-1",
    "youtube_automation-worker_podcast_audio-1",
    "youtube_automation-worker_seo_blogs-1",
    "youtube_automation-worker_web_series-1",
    "youtube_automation-worker_radio_fm-1",
    "youtube_automation-worker_ebooks_kdp-1",
    "youtube_automation-worker_audiobooks_acx-1",
    "youtube_automation-worker_sleep_stories-1",
    "youtube_automation-worker_online_courses-1",
)


async def restart_worker_container(justification: str):
    """Restart all factory worker containers via Docker socket."""
    logger.error(
        "watchdog_general_intervention",
        extra={"msg": "Restarting factory workers.", "justification": justification},
    )
    restarted: list[str] = []
    for name in WORKER_CONTAINERS:
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
            "Action": "Hard Restart via Docker Socket",
            "Containers": ", ".join(restarted),
            "Justification": justification,
        },
    )

async def main():
    logger.info("Watchdog General Daemon initialized. Monitoring Medic Heartbeat and running SRE benchmarks.")
    settings = get_settings()
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))

    # Load limits
    benchmark_file = os.path.join(os.path.dirname(__file__), "..", "..", "config", "benchmarks.json")
    with open(benchmark_file) as f:
        benchmarks = json.load(f)
    tts_limit = benchmarks.get("pipeline", {}).get("kokoro_tts", {}).get("hard_limit_6k", 150.0)
    ffmpeg_limit = benchmarks.get("pipeline", {}).get("ffmpeg_nvenc", {}).get("hard_limit_6k", 900.0)

    # Initialize Uploader for monitoring
    uploader = YouTubeUploader()
    db_factory = get_session_factory()

    while True:
        try:
            # 1. Run Standard Benchmarks (External Providers)
            try:
                await run_watchdog_benchmark()
            except Exception as e:
                logger.error("watchdog_benchmark_evaluation_failed", extra={"error": str(e)})
            
            # 2. Check Medic Heartbeat (Internal Worker)
            raw_heartbeat = await redis.get("worker:heartbeat")
            current_time = int(time.time())
            
            if not raw_heartbeat:
                logger.warning("No worker heartbeat found yet.")
            else:
                try:
                    payload = json.loads(raw_heartbeat)
                except Exception:
                    # Fallback for old integer timestamps during transition
                    payload = {"timestamp": int(raw_heartbeat), "status": "unknown", "memory_percent": 0.0}

                last_timestamp = payload.get("timestamp", 0)
                seconds_since_heartbeat = current_time - last_timestamp
                
                # TRIGGER A: Instant Proactive Intervention
                if payload.get("status") == "critical_memory" or payload.get("memory_percent", 0.0) > 90.0:
                    justification = f"Medic reported critical memory at {payload.get('memory_percent', 0.0):.1f}%. Action taken to prevent OOM crash."
                    logger.critical("PROACTIVE INTERVENTION", extra={"justification": justification})
                    await restart_worker_container(justification)
                    await redis.set("worker:heartbeat", json.dumps({"timestamp": current_time, "status": "restarting", "memory_percent": 0.0}))
                    
                # TRIGGER B: Catastrophic Silence
                elif seconds_since_heartbeat > 60:
                    justification = f"Total System Lapse. No heartbeat received from Medic for {seconds_since_heartbeat} seconds."
                    logger.critical("TOTAL SYSTEM LAPSE DETECTED", extra={"justification": justification})
                    await restart_worker_container(justification)
                    await redis.set("worker:heartbeat", json.dumps({"timestamp": current_time, "status": "restarting", "memory_percent": 0.0}))
                    
                # TRIGGER C: Pipeline Degradation
                else:
                    worker_benchmarks = payload.get("benchmarks", {})
                    tts_avg = worker_benchmarks.get("tts_avg_duration", 0.0)
                    ffmpeg_avg = worker_benchmarks.get("ffmpeg_avg_duration", 0.0)
                    
                    if tts_avg > tts_limit:
                        justification = f"Medic benchmark report: TTS average duration ({tts_avg:.1f}s) exceeded limit ({tts_limit}s)."
                        logger.critical("PIPELINE DEGRADATION INTERVENTION", extra={"justification": justification})
                        await restart_worker_container(justification)
                        await redis.set("worker:heartbeat", json.dumps({"timestamp": current_time, "status": "restarting", "memory_percent": 0.0}))
                    elif ffmpeg_avg > ffmpeg_limit:
                        justification = f"Medic benchmark report: FFmpeg average duration ({ffmpeg_avg:.1f}s) exceeded limit ({ffmpeg_limit}s)."
                        logger.critical("PIPELINE DEGRADATION INTERVENTION", extra={"justification": justification})
                        await restart_worker_container(justification)
                        await redis.set("worker:heartbeat", json.dumps({"timestamp": current_time, "status": "restarting", "memory_percent": 0.0}))

                # TRIGGER D: YouTube Processing Monitoring
                pending_uploads = await redis.smembers("youtube:pending_processing")
                for item in pending_uploads:
                    job_id_str, video_id = item.decode("utf-8").split(":")
                    job_id = int(job_id_str)
                    
                    try:
                        status = uploader.check_processing_status(video_id)
                        logger.info("youtube_processing_status", extra={"video_id": video_id, "status": status})
                        
                        if status == "succeeded":
                            # Processing done.
                            await redis.srem("youtube:pending_processing", item)
                        elif status in ("failed", "rejected"):
                            # Supremacy Intervention
                            justification = f"General Intervention: YouTube rejected video {video_id}. Imposing emergency re-encode on Job {job_id}."
                            logger.critical("YOUTUBE PROCESSING FAILED", extra={"justification": justification, "video_id": video_id})
                            
                            # Update DB
                            async with db_factory() as session:
                                job = await get_job(session, job_id)
                                if job:
                                    await transition_stage(
                                        session, job, JobStage.QUEUED, status=JobStatus.QUEUED, 
                                        error="YouTube Processing Failed - Emergency Re-encode Forced"
                                    )
                                    
                            await redis.set(f"worker:emergency_config:{job_id}", "force_fallback_codec")
                            await dispatch_ledger("GENERAL INTERVENTION: YouTube Failure", {"Action": "Forced Re-encode", "Video ID": video_id, "Job ID": job_id})
                            await redis.srem("youtube:pending_processing", item)
                    except Exception as e:
                        logger.error("youtube_processing_check_failed", extra={"error": str(e), "video_id": video_id})
                        
            # 3. Disk Space Management
            try:
                import shutil
                disk_limit = benchmarks.get("disk", {}).get("max_capacity_percent", 50.0)
                emergency_limit = 90.0  # Critical threshold where system hangs are imminent
                
                usage = shutil.disk_usage("/")
                used_percent = (usage.used / usage.total) * 100
                
                # TRIGGER E: Critical Disk Exhaustion (System Hang Prevention)
                if used_percent > emergency_limit:
                    justification = f"Disk at critical {used_percent:.1f}%. Initiating emergency purge for required resources + safety margin."
                    logger.critical("SYSTEM_HANG_PREVENTION", extra={"justification": justification})
                    
                    storage = ObjectStorage()
                    async with db_factory() as session:
                        old_jobs = await get_oldest_completed_jobs(session, limit=50)
                        
                        # Aggressively delete to reach safe capacity minus a 10% safety margin
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
                                {"Action": f"Wiped artifacts for Job {job_id}", "Reason": f"Hang Prevention. Target: {target_percent}%. Current: {current_used:.1f}%"}
                            )
                            
                # TRIGGER F: Standard Disk Space Management (50% Threshold)
                elif used_percent > disk_limit:
                    logger.warning("watchdog_disk_warning", extra={"used_percent": used_percent, "limit": disk_limit})
                    
                    # Purge oldest heavy artifacts until we are under limit
                    storage = ObjectStorage()
                    async with db_factory() as session:
                        old_jobs = await get_oldest_completed_jobs(session, limit=10)
                        
                        for old_job in old_jobs:
                            usage = shutil.disk_usage("/")
                            current_used = (usage.used / usage.total) * 100
                            if current_used <= disk_limit - 5.0:  # Drop it to 5% below limit
                                break
                                
                            job_id = old_job.id
                            storage.delete_prefix(f"jobs/{job_id}/")
                            await delete_heavy_artifacts(session, job_id)
                            
                            await dispatch_ledger(
                                "GENERAL INTERVENTION: Disk Purge", 
                                {"Action": f"Deleted heavy artifacts for Job {job_id}", "Reason": f"Disk at {current_used:.1f}%"}
                            )
            except Exception as e:
                logger.error("watchdog_disk_monitor_failed", extra={"error": str(e)})
                        
        except Exception as e:
            logger.error("watchdog_daemon_crash_averted", extra={"error": str(e)})
        
        # Run every 1 minute to check heartbeat frequently, but benchmarks inside run_watchdog_benchmark might be expensive. 
        # Actually, let's keep it at 1 minute.
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
