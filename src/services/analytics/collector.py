import asyncio
import logging
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from src.db import PerformanceRecord as DBPerformanceRecord, Distribution
from src.services.analytics.protocols import PlatformAnalyticsSource
from src.services.analytics.source_youtube import YouTubeAnalyticsSource
from src.services.analytics.source_wordpress import WordPressAnalyticsSource
from src.services.analytics.source_stubs import SpotifyAnalyticsStub, KDPAnalyticsStub, ACXAnalyticsStub
from src.domain.analytics import PerformanceRecord

logger = logging.getLogger(__name__)

class AnalyticsCollector:
    """Collects analytics from all supported platforms on a schedule."""
    
    def __init__(self, redis_client: Redis, db_session: AsyncSession):
        self.redis = redis_client
        self.db_session = db_session
        
        # Map of platform -> source implementation
        self.sources: dict[str, PlatformAnalyticsSource] = {
            "youtube": YouTubeAnalyticsSource(),
            "wordpress": WordPressAnalyticsSource(),
            "spotify": SpotifyAnalyticsStub(),
            "kdp": KDPAnalyticsStub(),
            "acx": ACXAnalyticsStub(),
        }

    async def run_collection_cycle(self):
        """Main entry point intended to be run via cron (e.g., every 6 hours)."""
        logger.info("analytics_collection_start")
        
        # 1. Fetch all known active content from DB
        # For v1, we focus on distributions that have an external_id
        result = await self.db_session.execute(
            select(Distribution).where(Distribution.status == "success", Distribution.external_id.isnot(None))
        )
        distributions = result.scalars().all()
        
        tasks = []
        for dist in distributions:
            # We schedule fetching performance for each successful distribution
            tasks.append(self._collect_and_save(dist.platform, dist.external_id, job_id=dist.job_id))
            
        # You would query WordPress posts here if tracked in DB...
        
        # Run all fetches concurrently
        await asyncio.gather(*tasks)
        
        # Publish event
        await self.redis.publish("events:analytics:updated", "collection_cycle_complete")
        logger.info("analytics_collection_complete", extra={"collected_count": len(tasks)})

    async def _collect_and_save(self, platform: str, content_id: str, job_id: int = None):
        """Fetch stats for a single item and save to database."""
        source = self.sources.get(platform)
        if not source:
            logger.error(f"No analytics source configured for platform: {platform}")
            return
            
        try:
            # Fetch DTO
            record: PerformanceRecord = await source.fetch_performance(content_id)
            record.job_id = job_id
            
            # Save to DB
            db_record = DBPerformanceRecord(
                platform=record.platform,
                platform_content_id=record.platform_content_id,
                job_id=record.job_id,
                views=record.views,
                engagement_metrics=record.engagement_metrics,
                revenue_cents=record.revenue_cents,
                collected_at=record.collected_at
            )
            self.db_session.add(db_record)
            await self.db_session.commit()
            
        except Exception as e:
            logger.error(
                "analytics_collect_and_save_failed", 
                extra={"platform": platform, "content_id": content_id, "error": str(e)}
            )
            await self.db_session.rollback()
