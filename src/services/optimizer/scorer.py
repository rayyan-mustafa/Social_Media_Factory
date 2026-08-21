import logging
from typing import List
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from src.db import PerformanceRecord as DBPerformanceRecord, Job
from src.domain.trends import TrendCandidate
from src.domain.optimizer import TopicRecommendation

logger = logging.getLogger(__name__)

class RuleBasedTopicScorer:
    """
    V1 Topic Optimizer engine using heuristic scoring based on historical analytics.
    Degrades gracefully to raw trend scores if no historical data exists.
    """
    
    def __init__(self, db_session: AsyncSession):
        self.db_session = db_session
        
    async def rank_candidates(
        self, 
        candidates: List[TrendCandidate], 
        business_model: str, 
        niche: str
    ) -> List[TopicRecommendation]:
        """
        Rank a list of candidates using historical analytics data.
        
        Args:
            candidates: Raw trends from the Trend Engine.
            business_model: The target model (e.g., "YouTube_Shorts")
            niche: The target niche (e.g., "documentary")
            
        Returns:
            Ranked list of TopicRecommendation objects (highest score first).
        """
        if not candidates:
            return []
            
        # 1. Calculate historical baseline for this specific model + niche
        multiplier, confidence = await self._calculate_analytics_multiplier(business_model, niche)
        
        # 2. Score candidates
        recommendations = []
        for candidate in candidates:
            # V1 Heuristic: raw engagement score * niche analytics multiplier
            predicted_score = candidate.engagement_score * multiplier
            
            recommendations.append(
                TopicRecommendation(
                    candidate=candidate,
                    business_model=business_model,
                    niche=niche,
                    predicted_score=predicted_score,
                    confidence_level=confidence
                )
            )
            
        # 3. Sort by predicted_score descending
        recommendations.sort(key=lambda r: r.predicted_score, reverse=True)
        return recommendations
        
    async def _calculate_analytics_multiplier(self, business_model: str, niche: str) -> tuple[float, str]:
        """
        Calculate a performance multiplier based on the last 30 days of data.
        Returns: (multiplier: float, confidence_level: str)
        """
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        
        # Fetch median/avg views for the requested model & niche
        stmt = (
            select(func.avg(DBPerformanceRecord.views))
            .join(Job, DBPerformanceRecord.job_id == Job.id)
            .where(Job.business_model == business_model)
            .where(Job.niche == niche)
            .where(DBPerformanceRecord.collected_at >= thirty_days_ago)
        )
        
        result = await self.db_session.execute(stmt)
        avg_views = result.scalar()
        
        # If no data exists, gracefully degrade
        if avg_views is None or avg_views == 0:
            logger.info(f"No historical analytics found for {business_model}/{niche}. Degrading to base score.")
            return 1.0, "low"
            
        # V1 logic: If average views are high, boost the score. 
        # (This is a simplified heuristic; a real system would compare to a global baseline)
        if avg_views > 10000:
            multiplier = 1.5
            confidence = "high"
        elif avg_views > 1000:
            multiplier = 1.2
            confidence = "medium"
        else:
            multiplier = 0.8  # Historically poor performance, slightly penalize
            confidence = "medium"
            
        return multiplier, confidence
