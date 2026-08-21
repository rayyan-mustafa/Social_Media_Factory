import asyncio
import logging
from typing import List, Dict
from redis.asyncio import Redis

from src.domain.trends import TrendCandidate
from src.services.trend_engine.scraper_youtube import YouTubeScraper
from src.services.trend_engine.scraper_google import GoogleTrendsScraper
from src.core.config import get_settings

logger = logging.getLogger(__name__)

class TrendAggregator:
    def __init__(self, redis_client: Redis):
        self.redis = redis_client
        self.settings = get_settings()
        self.scrapers = {
            "youtube": YouTubeScraper(),
            "google": GoogleTrendsScraper()
        }

    async def aggregate_and_store(self) -> List[TrendCandidate]:
        logger.info("trend_aggregation_start")
        
        # Run all scrapers concurrently
        scraper_names = list(self.scrapers.keys())
        tasks = [scraper.fetch_trends() for scraper in self.scrapers.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        all_candidates: List[TrendCandidate] = []
        for name, res in zip(scraper_names, results):
            if isinstance(res, Exception):
                if name == "youtube":
                    logger.error("youtube_scraper_failed", exc_info=res)
                else:
                    logger.warning(f"{name}_scraper_failed_gracefully", exc_info=res)
            elif isinstance(res, list):
                if name == "youtube" and not res:
                    logger.error("youtube_scraper_zero_results", extra={"msg": "YouTube returned zero trends. This is a primary source failure."})
                all_candidates.extend(res)
                
        # Deduplicate and score
        merged_trends = self._deduplicate_trends(all_candidates)
        
        # Sort by total engagement score descending
        sorted_trends = sorted(merged_trends, key=lambda x: x.engagement_score, reverse=True)
        
        # Store in Redis Sorted Set
        await self._store_in_redis(sorted_trends)
        
        logger.info("trend_aggregation_complete", extra={"total_trends": len(sorted_trends)})
        return sorted_trends

    def _deduplicate_trends(self, candidates: List[TrendCandidate]) -> List[TrendCandidate]:
        merged: Dict[str, TrendCandidate] = {}
        for c in candidates:
            # Simple normalization for deduplication (alphanumeric and spaces)
            normalized_topic = "".join(char for char in c.topic.lower() if char.isalnum() or char.isspace()).strip()
            # Fallback for very short string
            if not normalized_topic:
                normalized_topic = c.topic.lower()
                
            if normalized_topic in merged:
                # Merge logic: add engagement scores, append source and raw_data
                existing = merged[normalized_topic]
                # Cap combined engagement to prevent a single heavily cross-posted item from having infinite score
                existing.engagement_score = min(existing.engagement_score + c.engagement_score, 10.0)
                if c.platform_source not in existing.platform_source:
                    existing.platform_source += f", {c.platform_source}"
                existing.raw_data.update({c.platform_source: c.raw_data})
            else:
                c.raw_data = {c.platform_source: c.raw_data}
                merged[normalized_topic] = c
                
        return list(merged.values())

    async def _store_in_redis(self, trends: List[TrendCandidate]):
        if not trends:
            return
            
        # Clear old trends to replace them completely for this iteration
        pipeline = self.redis.pipeline()
        pipeline.delete("trends:candidates")
        
        # ZADD expects a mapping of {member (string): score (float)}
        zadd_data = {}
        for trend in trends:
            member = trend.model_dump_json()
            zadd_data[member] = trend.engagement_score
            
        pipeline.zadd("trends:candidates", zadd_data)
        # Expire after 24 hours so we don't have stale data if engine stops
        pipeline.expire("trends:candidates", 86400)
        
        await pipeline.execute()
