import httpx
import logging
from typing import List
from src.domain.trends import TrendCandidate
from src.core.config import get_settings

logger = logging.getLogger(__name__)

class YouTubeScraper:
    def __init__(self, region_code: str = "US"):
        self.region_code = region_code
        self.settings = get_settings()

    async def fetch_trends(self) -> List[TrendCandidate]:
        candidates = []
        if not self.settings.youtube_api_key:
            logger.warning("youtube_scraper_skipped_no_api_key")
            return candidates

        url = "https://www.googleapis.com/youtube/v3/videos"
        params = {
            "part": "snippet,statistics",
            "chart": "mostPopular",
            "regionCode": self.region_code,
            "maxResults": 15,
            "key": self.settings.youtube_api_key
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                
                for item in data.get("items", []):
                    snippet = item.get("snippet", {})
                    statistics = item.get("statistics", {})
                    
                    title = snippet.get("title", "")
                    view_count = int(statistics.get("viewCount", 0))
                    like_count = int(statistics.get("likeCount", 0))
                    
                    # Normalize engagement score based on expected max views for trending (~10M)
                    max_expected_views = 10_000_000
                    engagement = min(view_count / max_expected_views, 1.0)
                    
                    if title:
                        candidates.append(
                            TrendCandidate(
                                topic=title,
                                platform_source=f"youtube/trending/{self.region_code}",
                                engagement_score=engagement,
                                raw_data={
                                    "video_id": item.get("id"),
                                    "view_count": view_count,
                                    "like_count": like_count,
                                    "channel_title": snippet.get("channelTitle", "")
                                }
                            )
                        )
            except Exception as e:
                logger.error("youtube_scraper_failed", extra={"error": str(e)})

        return candidates
