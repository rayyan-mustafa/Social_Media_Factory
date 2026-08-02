"""Business Manager Microservice (CEO).

Completely decoupled from the heavy video generation workers. 
Scrapes trends, manages the Airtable pipeline, queues work, and monitors business benchmarks.
"""

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta

from arq.connections import RedisSettings, create_pool

from src.core.config import get_settings
from src.core.logging import get_logger
from src.db import get_session_factory
from src.db.repository import create_job, get_business_model_stats, get_latest_job_time
from src.domain import BUSINESS_MODELS
from src.services.airtable_client import AirtableClient
from src.services.notifications import dispatch_ledger
from src.services.viral_pivot import ViralTrendScraper

logger = get_logger(__name__)

async def main():
    logger.info("Business Manager Daemon initialized. Taking control of the Airtable production queue.")
    settings = get_settings()
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    db_factory = get_session_factory()
    
    scraper = ViralTrendScraper()
    airtable = AirtableClient()
    
    # Load Business Benchmarks
    benchmark_file = os.path.join(os.path.dirname(__file__), "..", "..", "config", "benchmarks.json")
    with open(benchmark_file) as f:
        benchmarks = json.load(f)
    
    biz_benchmarks = benchmarks.get("business", {})
    topic_starvation_hours = biz_benchmarks.get("topic_starvation_hours", 24)
    min_success_rate = biz_benchmarks.get("minimum_success_rate_percent", 50)
    max_failed_jobs = biz_benchmarks.get("max_failed_jobs_per_day", 5)

    last_scrape_time = 0
    SCRAPE_INTERVAL = 6 * 3600  # Scrape every 6 hours
    POLL_INTERVAL = 60 * 5      # Poll Airtable for approvals every 5 mins
    
    # Single source of truth: src.domain.BUSINESS_MODELS
    TABLES = list(BUSINESS_MODELS)
    
    # Track suspended tables due to poor yield
    suspended_tables = set()

    while True:
        try:
            current_time = int(time.time())
            
            # 0. Benchmark Evaluation Phase (Autonomous Management)
            logger.info("business_manager_evaluating_benchmarks")
            async with db_factory() as session:
                for table in TABLES:
                    # Check B: Pipeline Yield (Quality)
                    since_24h = datetime.now(UTC) - timedelta(hours=24)
                    stats = await get_business_model_stats(session, table, since=since_24h)
                    total_finished = stats.get("succeeded", 0) + stats.get("failed", 0)
                    failed = stats.get("failed", 0)
                    
                    if total_finished > 0:
                        success_rate = (stats.get("succeeded", 0) / total_finished) * 100
                        if success_rate < min_success_rate or failed >= max_failed_jobs:
                            if table not in suspended_tables:
                                logger.critical("business_manager_poor_yield", extra={"table": table, "success_rate": success_rate, "failed": failed})
                                suspended_tables.add(table)
                                await dispatch_ledger(
                                    "BUSINESS INTERVENTION: Worker Suspended", 
                                    {"Action": "Suspended Airtable Polling", "Business Model": table, "Reason": f"Yield dropped to {success_rate:.1f}% ({failed} failures)."}
                                )
                    else:
                        # If no jobs in 24h, check if suspended and maybe release if we want to try again (for now keep suspended until manual intervention)
                        pass
                    
                    # Check A: Content Starvation
                    if table not in suspended_tables:
                        latest_job_time = await get_latest_job_time(session, table)
                        starved = False
                        if latest_job_time:
                            # Ensure latest_job_time is timezone aware
                            if latest_job_time.tzinfo is None:
                                latest_job_time = latest_job_time.replace(tzinfo=UTC)
                            hours_since = (datetime.now(UTC) - latest_job_time).total_seconds() / 3600
                            if hours_since > topic_starvation_hours:
                                starved = True
                        else:
                            # Never had a job
                            starved = True
                            
                        if starved:
                            logger.warning("business_manager_starvation_detected", extra={"table": table})
                            # Autonomous Action: Scrape and Auto-Approve to feed the pipeline
                            seed_topic = await scraper.determine_best_seed()
                            if seed_topic:
                                logger.info("business_manager_auto_approving_topic", extra={"table": table, "topic": seed_topic["title"]})
                                job = await create_job(session, topic=seed_topic["title"], niche=seed_topic["niche"], business_model=table)
                                await redis.enqueue_job(
                                    "run_pipeline",
                                    job.id,
                                    _queue_name=table,
                                )
                                await dispatch_ledger(
                                    "BUSINESS INTERVENTION: Pipeline Starved", 
                                    {"Action": "Auto-Approve Trending Topic", "Business Model": table, "Topic": seed_topic["title"]}
                                )

            # 1. Scraping Phase (Top of Funnel)
            if current_time - last_scrape_time > SCRAPE_INTERVAL:
                logger.info("business_manager_scraping_trends")
                seed_topic = await scraper.determine_best_seed()
                
                # Push the absolute best trend to Airtable for human approval
                if seed_topic:
                    for table in TABLES:
                        if table not in suspended_tables:
                            success = await airtable.push_topic(
                                title=seed_topic["title"],
                                niche=seed_topic["niche"],
                                metrics=seed_topic.get("metrics", {}),
                                table_name=table
                            )
                            if success:
                                logger.info("airtable_suggestion_pushed", extra={"topic": seed_topic["title"], "table": table})
                
                last_scrape_time = current_time

            # 2. Polling Phase (Human-in-the-Loop)
            logger.info("business_manager_polling_airtable")
            
            for table in TABLES:
                if table in suspended_tables:
                    continue  # Skip polling if suspended due to poor yield
                    
                approved_topics = await airtable.get_approved_topics(table_name=table)
                for approved in approved_topics:
                    record_id = approved["id"]
                    topic = approved["topic"]
                    niche = approved["niche"]
                    
                    # Mark as processing to avoid duplicate queuing
                    update_success = await airtable.mark_topic_processing(record_id, table_name=table)
                    if not update_success:
                        continue
                    
                    # Push to main Video Production Database
                    async with db_factory() as session:
                        job = await create_job(session, topic=topic, niche=niche, business_model=table)
                        await redis.enqueue_job(
                            "run_pipeline",
                            job.id,
                            _queue_name=table,
                        )
                        logger.info("business_manager_arq_job_enqueued", extra={"job_id": job.id, "business_model": table})

        except Exception as e:
            logger.error("business_manager_loop_error", extra={"error": str(e)})
            
        await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
