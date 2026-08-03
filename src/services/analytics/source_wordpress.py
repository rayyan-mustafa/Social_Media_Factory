import logging
import httpx
from src.domain.analytics import PerformanceRecord
from src.core.config import get_settings

logger = logging.getLogger(__name__)

class WordPressAnalyticsSource:
    """WordPress Analytics source utilizing the WP REST API or Jetpack Stats."""
    
    def __init__(self):
        self.settings = get_settings()
        # Fallback to a dummy URL if not configured
        self.base_url = getattr(self.settings, "wordpress_url", "https://example.com/wp-json/wp/v2")

    async def fetch_performance(self, content_id: str) -> PerformanceRecord:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Basic implementation hitting standard WP endpoint for post views
                # Note: Default WP doesn't track views. This assumes a standard plugin setup (e.g. Jetpack)
                response = await client.get(f"{self.base_url}/posts/{content_id}")
                response.raise_for_status()
                data = response.json()
                
                # Extract views and comments if exposed in the API response
                views = int(data.get("views", 0))
                # WordPress typically exposes comment status/count if configured
                # This depends heavily on the specific plugin exposing stats to REST
                
                return PerformanceRecord(
                    platform="wordpress",
                    platform_content_id=content_id,
                    views=views,
                    engagement_metrics={
                        "comments": 0 # Would parse actual comment endpoint or field here
                    },
                    revenue_cents=0
                )
                
        except Exception as e:
            logger.error("wordpress_analytics_fetch_failed", extra={"post_id": content_id, "error": str(e)})
            return PerformanceRecord(
                platform="wordpress",
                platform_content_id=content_id,
                views=0,
                engagement_metrics={}
            )
