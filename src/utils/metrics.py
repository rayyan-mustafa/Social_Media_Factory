"""Prometheus metrics helpers."""

from prometheus_client import Counter, Gauge, Histogram

JOBS_CREATED = Counter("youtube_jobs_created_total", "Jobs created via API")
JOBS_SUCCEEDED = Counter("youtube_jobs_succeeded_total", "Jobs completed successfully")
JOBS_FAILED = Counter("youtube_jobs_failed_total", "Jobs failed terminally")
STAGE_DURATION = Histogram(
    "youtube_stage_duration_seconds",
    "Pipeline stage duration",
    ["stage"],
)
ACTIVE_JOBS = Gauge("youtube_active_jobs", "Jobs currently running")
