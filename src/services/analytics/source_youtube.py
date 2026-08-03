import logging
import httpx
from src.domain.analytics import PerformanceRecord
from src.core.config import get_settings

logger = logging.getLogger(__name__)

class YouTubeAnalyticsSource:
    """YouTube Analytics source utilizing the YouTube Data API v3 for public stats."""
    
    def __init__(self):
        self.settings = get_settings()
        self.api_key = getattr(self.settings, "youtube_api_key", None)
        self.base_url = "https://www.googleapis.com/youtube/v3/videos"

    async def fetch_performance(self, content_id: str) -> PerformanceRecord:
        if not self.api_key:
            logger.error("YouTube API key not configured for analytics.")
            return self._empty_record(content_id)

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    self.base_url,
                    params={
                        "part": "statistics",
                        "id": content_id,
                        "key": self.api_key
                    }
                )
                response.raise_for_status()
                data = response.json()
                
                if not data.get("items"):
                    logger.warning(f"YouTube video {content_id} not found.")
                    return self._empty_record(content_id)
                
                stats = data["items"][0].get("statistics", {})
                
                return PerformanceRecord(
                    platform="youtube",
                    platform_content_id=content_id,
                    views=int(stats.get("viewCount", 0)),
                    engagement_metrics={
                        "likes": int(stats.get("likeCount", 0)),
                        "comments": int(stats.get("commentCount", 0)),
                        "favorites": int(stats.get("favoriteCount", 0))
                    },
                    # Note: Revenue requires the separate YouTube Analytics API (OAuth)
                    revenue_cents=0
                )
                
        except Exception as e:
            logger.error("youtube_analytics_fetch_failed", extra={"video_id": content_id, "error": str(e)})
            return self._empty_record(content_id)

    def _empty_record(self, content_id: str) -> PerformanceRecord:
        return PerformanceRecord(
            platform="youtube",
            platform_content_id=content_id,
            views=0,
            engagement_metrics={}
        )
