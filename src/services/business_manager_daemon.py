"""Business Manager Microservice (CEO).

Completely decoupled from the heavy video generation workers. 
Scrapes trends, manages production benchmarks, and queues auto-approved work when starved.
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
from src.db.repository import create_job, get_business_model_stats
from src.domain import BUSINESS_MODELS, ScriptPayload, SceneScript
from src.domain.trends import TrendCandidate
from src.services.notifications import dispatch_ledger
from src.services.trend_engine.aggregator import TrendAggregator
from src.services.scheduler.calendar import ContentCalendar
from src.services.optimizer.scorer import RuleBasedTopicScorer
from src.services.content_multiplier import ContentMultiplier

logger = get_logger(__name__)

from src.services.financial.budget_guard import can_afford_job

async def project_financial_impact(session, table: str) -> bool:
    """
    Module 9 dependency: Evaluate autonomous spending limits via Budget Guard.
    Uses a baseline estimated cost (e.g. 50 cents) to verify if the model can afford it.
    """
    # Assuming baseline 50 cents to create a job if no dynamic cost given
    estimated_cost_cents = 50 
    return await can_afford_job(session, table, estimated_cost_cents)

async def main():
    logger.info("Business Manager Daemon initialized.")
    settings = get_settings()
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    db_factory = get_session_factory()
    
    aggregator = TrendAggregator(redis)
    multiplier = ContentMultiplier()
    
    # Load Business Benchmarks
    benchmark_file = os.path.join(os.path.dirname(__file__), "..", "..", "config", "benchmarks.json")
    with open(benchmark_file) as f:
        benchmarks = json.load(f)
    
    biz_benchmarks = benchmarks.get("business", {})
    min_success_rate = biz_benchmarks.get("minimum_success_rate_percent", 50)
    max_failed_jobs = biz_benchmarks.get("max_failed_jobs_per_day", 5)

    CHECK_INTERVAL = 60         # Evaluate benchmarks every 60 seconds
    
    TABLES = list(BUSINESS_MODELS)
    suspended_tables = set()

    while True:
        try:
            # [PRECEDENCE RULE: Content Multiplier vs Content Calendar]
            # The Content Multiplier determines the 9 theoretical outputs for a given topic, 
            # but the Content Calendar serves as a strict filter. If a multiplied output 
            # is not "due" yet according to the Calendar, it is skipped/deferred, rather 
            # than blindly enqueued. The Calendar paces the output.

            logger.info("business_manager_evaluating_benchmarks")
            async with db_factory() as session:
                calendar = ContentCalendar(session)
                scorer = RuleBasedTopicScorer(session)
                
                models_due = []
                
                for table in TABLES:
                    # 1 & 2. Check Calendar & Starvation (Collapsed into one check)
                    is_due = await calendar.is_due_for_content(table)
                    if not is_due:
                        continue
                        
                    # 3. Evaluate Yield (Quality)
                    since_24h = datetime.now(UTC) - timedelta(hours=24)
                    stats = await get_business_model_stats(session, table, since=since_24h)
                    
                    # Use total_succeeded which includes AWAITING_MANUAL_PUBLISH
                    succeeded = stats.get("total_succeeded", 0)
                    failed = stats.get("failed", 0)
                    total_finished = succeeded + failed
                    
                    # Yield check zero-data edgecase: min sample size of 3
                    if total_finished >= 3:
                        success_rate = (succeeded / total_finished) * 100
                        if success_rate < min_success_rate or failed >= max_failed_jobs:
                            if table not in suspended_tables:
                                logger.critical("business_manager_poor_yield", extra={"table": table, "success_rate": success_rate, "failed": failed})
                                suspended_tables.add(table)
                                await dispatch_ledger(
                                    "BUSINESS INTERVENTION: Worker Suspended", 
                                    {"Action": "Suspended Queue", "Business Model": table, "Reason": f"Yield dropped to {success_rate:.1f}% ({failed} failures)."}
                                )
                            continue
                    
                    # If it's due and not suspended, we want content for it
                    if table not in suspended_tables:
                        models_due.append(table)
                
                # 4 & 5 & 6. Produce content if anything is due
                if models_due:
                    logger.info(f"business_manager_content_needed_for: {models_due}")
                    
                    # 4. Get Optimized Topic via TrendAggregator
                    candidates = await aggregator.aggregate_and_store()
                    if candidates:
                        # Grab the top candidate
                        candidate = candidates[0]
                        # We use the scorer against the first due model's niche as the baseline
                        target_model = models_due[0]
                        
                        # Just score the best one to see if it meets threshold, or get the scorer's modified version
                        # Alternatively, we could score all candidates. For now, pass the top one.
                        # Niche can default to "general" or use the top candidate's implicit category
                        niche = "general"
                        recs = await scorer.rank_candidates([candidate], target_model, niche)
                        
                        if recs:
                            best_rec = recs[0]
                            
                            # 6. Track Financial Impact
                            # Check budget guard per table below
                            
                            if True: # We still want to generate multiplied scripts if at least one model is due
                                # 5. Trigger Content Multiplier (Filtered by Calendar)
                                # First, generate the adapted scripts (titles) for all models
                                # Ensure we meet SceneScript min_length constraints (text>=10, visual_query>=2)
                                padded_text = f"Base content for topic: {best_rec.candidate.topic}"
                                padded_visual = f"visual query for {best_rec.candidate.topic}"
                                
                                master_script = ScriptPayload(
                                    title=best_rec.candidate.topic,
                                    description="Base topic from trends",
                                    scenes=[SceneScript(index=1, text=padded_text, visual_query=padded_visual)]
                                )
                                multiplied_scripts = await multiplier.multiply_script(master_script)
                                
                                # DIRECT_UPLOAD_MODELS - Models that go to YouTube
                                direct_upload_models = {"YouTube_Shorts", "Web_Series", "Sleep_Stories"}
                                
                                for table in models_due:
                                    # 6. Track Financial Impact per-model
                                    approved = await project_financial_impact(session, table)
                                    if not approved:
                                        logger.warning("business_manager_budget_breached", extra={"table": table})
                                        continue

                                    # Fallback to the original topic if multiplier didn't return one for this table
                                    adapted_topic = best_rec.candidate.topic
                                    if table in multiplied_scripts and "title" in multiplied_scripts[table]:
                                        adapted_topic = multiplied_scripts[table]["title"]
                                    
                                    upload_to_youtube = table in direct_upload_models
                                    
                                    logger.info("business_manager_auto_approving_topic", extra={"table": table, "topic": adapted_topic})
                                    job = await create_job(
                                        session, 
                                        topic=adapted_topic, 
                                        niche=niche, 
                                        business_model=table, 
                                        upload_to_youtube=upload_to_youtube
                                    )
                                    await redis.enqueue_job(
                                        "run_pipeline",
                                        job.id,
                                        _queue_name=table,
                                    )
                                    await dispatch_ledger(
                                        "BUSINESS INTERVENTION: Calendar Due", 
                                        {"Action": "Auto-Approve Optimized Topic", "Business Model": table, "Topic": adapted_topic}
                                    )

        except Exception as e:
            logger.error("business_manager_loop_error", extra={"error": str(e)})
            # Fire an alert so we aren't silently failing (e.g. from Pydantic validation errors)
            try:
                await dispatch_ledger(
                    "BUSINESS INTERVENTION: Daemon Crash",
                    {"Action": "Loop Exception", "Error": str(e)}
                )
            except Exception as nested_e:
                logger.error("business_manager_alert_failed", extra={"error": str(nested_e)})
            
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    # If run directly instead of via module entry point
    pass
