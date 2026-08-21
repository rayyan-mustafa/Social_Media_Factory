"""ARQ worker settings."""

from __future__ import annotations

import os

from arq import cron
from arq.connections import RedisSettings, default_queue_name

from src.core.config import get_settings
from src.core.logging import setup_logging
from src.services.heartbeat import write_heartbeat
from src.workers.pipeline import run_pipeline


def _redis_settings() -> RedisSettings:
    settings = get_settings()
    return RedisSettings.from_dsn(settings.redis_url)


async def on_startup(ctx: dict) -> None:
    setup_logging()


class WorkerSettings:
    queue_name = os.getenv("ARQ_QUEUE_NAME", default_queue_name)
    functions = [run_pipeline]
    cron_jobs = [
        cron(write_heartbeat, minute=set(range(0, 60, 1)))  # Run every 1 minute
    ]
    redis_settings = _redis_settings()
    on_startup = on_startup
    max_jobs = 2
    job_timeout = 3600


def run_worker() -> None:
    import sys

    from arq.cli import cli

    sys.argv = ["arq", "src.workers.settings.WorkerSettings"]
    cli()
