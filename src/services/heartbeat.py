import time
import json
import os
from src.core.logging import get_logger

logger = get_logger(__name__)

def get_memory_usage() -> float:
    """Reads /proc/meminfo to calculate memory usage percentage."""
    try:
        with open('/proc/meminfo', 'r') as f:
            lines = f.readlines()
        mem_total = 0
        mem_available = 0
        for line in lines:
            if line.startswith('MemTotal:'):
                mem_total = int(line.split()[1])
            elif line.startswith('MemAvailable:'):
                mem_available = int(line.split()[1])
        if mem_total == 0:
            return 0.0
        return ((mem_total - mem_available) / mem_total) * 100.0
    except Exception:
        return 0.0

async def write_heartbeat(ctx: dict) -> None:
    """Medic Agent: Writes a full telemetry log report to Redis."""
    redis = ctx.get("redis")
    if not redis:
        logger.error("Medic could not write heartbeat: No Redis connection.")
        return
        
    current_time = int(time.time())
    mem_percent = get_memory_usage()
    
    # Process Pipeline Benchmarks
    tts_metrics = await redis.lrange("worker:metrics:tts", 0, -1)
    ffmpeg_metrics = await redis.lrange("worker:metrics:ffmpeg", 0, -1)
    await redis.delete("worker:metrics:tts", "worker:metrics:ffmpeg")
    
    tts_avg = sum(float(x) for x in tts_metrics) / len(tts_metrics) if tts_metrics else 0.0
    ffmpeg_avg = sum(float(x) for x in ffmpeg_metrics) / len(ffmpeg_metrics) if ffmpeg_metrics else 0.0

    payload = {
        "timestamp": current_time,
        "memory_percent": mem_percent,
        "status": "healthy" if mem_percent < 90 else "critical_memory",
        "jobs_completed": ctx.get("jobs_complete", 0),
        "jobs_failed": ctx.get("jobs_failed", 0),
        "benchmarks": {
            "tts_avg_duration": tts_avg,
            "ffmpeg_avg_duration": ffmpeg_avg
        }
    }
    
    await redis.set("worker:heartbeat", json.dumps(payload))
    logger.debug("Medic telemetry updated.", extra={"payload": payload})
