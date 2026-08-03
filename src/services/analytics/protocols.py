from typing import Protocol, List
from src.domain.analytics import PerformanceRecord

class PlatformAnalyticsSource(Protocol):
    """Protocol for fetching analytics from a specific distribution platform."""

    async def fetch_performance(self, content_id: str) -> PerformanceRecord:
        """
        Fetch performance metrics for a specific piece of content.
        
        Args:
            content_id: The platform-specific ID (e.g., YouTube video ID).
            
        Returns:
            PerformanceRecord containing views, engagement, and revenue.
        """
        ...
