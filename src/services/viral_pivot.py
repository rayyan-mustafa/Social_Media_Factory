"""Viral Trend Scraper (Phase 1).

Monitors multiple sources (Reddit, YouTube, Google Trends, TikTok, Instagram, Facebook)
to find spiking historical/documentary topics.
"""

import asyncio
import os

import httpx
from apify_client import ApifyClientAsync

from src.core.logging import get_logger

logger = get_logger(__name__)

class ViralTrendScraper:
    def __init__(self):
        self.subreddits = ["history", "todayilearned", "Damnthatsinteresting"]
        self.headers = {
            "User-Agent": "YouTubeAutomationViralScraper/1.0 (Contact: admin@example.com)"
        }
        self.apify_token = os.getenv("APIFY_TOKEN")
        self.apify_client = ApifyClientAsync(self.apify_token) if self.apify_token else None

    async def fetch_reddit_trends(self, subreddit: str, limit: int = 5) -> list:
        """Fetch the top posts of the day from a specific subreddit."""
        url = f"https://www.reddit.com/r/{subreddit}/top.json?t=day&limit={limit}"
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, headers=self.headers, timeout=10.0)
                response.raise_for_status()
                data = response.json()
                
                trends = []
                for child in data.get("data", {}).get("children", []):
                    post = child.get("data", {})
                    trends.append({
                        "title": post.get("title"),
                        "upvotes": post.get("ups"),
                        "comments": post.get("num_comments"),
                        "url": post.get("url"),
                        "source": f"Reddit r/{subreddit}",
                        "engagement_score": post.get("ups", 0) + post.get("num_comments", 0)
                    })
                return trends
            except Exception as e:
                logger.error("reddit_scrape_failed", extra={"subreddit": subreddit, "error": str(e)})
                return []

    async def fetch_youtube_trends(self) -> list:
        """Fetch trending YouTube videos. (Placeholder for YouTube Data API)"""
        # In a real implementation, hit GET https://www.googleapis.com/youtube/v3/videos?part=snippet,statistics&chart=mostPopular
        logger.info("fetch_youtube_trends_placeholder_called")
        return []

    async def fetch_google_trends(self) -> list:
        """Fetch Google Trends daily searches. (Placeholder for pytrends/RapidAPI)"""
        logger.info("fetch_google_trends_placeholder_called")
        return []

    async def fetch_tiktok_viral(self) -> list:
        """Fetch viral TikTok trends using Apify."""
        if not self.apify_client:
            logger.info("fetch_tiktok_viral_skipped_no_token")
            return []
            
        try:
            # Using a popular TikTok scraper actor
            run = await self.apify_client.actor("clockworks/tiktok-scraper").call(
                run_input={"hashtags": ["history", "documentary", "trending"], "resultsPerPage": 5}
            )
            dataset = await self.apify_client.dataset(run["defaultDatasetId"]).list_items()
            
            trends = []
            for item in dataset.items:
                trends.append({
                    "title": item.get("text", "TikTok Video"),
                    "url": item.get("webVideoUrl"),
                    "source": "TikTok",
                    "engagement_score": item.get("playCount", 0) + item.get("diggCount", 0)
                })
            return trends
        except Exception as e:
            logger.error("tiktok_scrape_failed", extra={"error": str(e)})
            return []

    async def fetch_instagram_viral(self) -> list:
        """Fetch viral Instagram Reels/Posts using Apify."""
        if not self.apify_client:
            logger.info("fetch_instagram_viral_skipped_no_token")
            return []
            
        try:
            # Using Apify's official Instagram Scraper
            run = await self.apify_client.actor("apify/instagram-scraper").call(
                run_input={"search": "history, documentary", "searchType": "hashtag", "resultsLimit": 5}
            )
            dataset = await self.apify_client.dataset(run["defaultDatasetId"]).list_items()
            
            trends = []
            for item in dataset.items:
                trends.append({
                    "title": item.get("caption", "Instagram Post"),
                    "url": item.get("url"),
                    "source": "Instagram",
                    "engagement_score": item.get("likesCount", 0) + item.get("commentsCount", 0)
                })
            return trends
        except Exception as e:
            logger.error("instagram_scrape_failed", extra={"error": str(e)})
            return []

    async def fetch_facebook_viral(self) -> list:
        """Fetch viral Facebook videos using Apify."""
        if not self.apify_client:
            logger.info("fetch_facebook_viral_skipped_no_token")
            return []
            
        try:
            run = await self.apify_client.actor("apify/facebook-pages-scraper").call(
                run_input={"startUrls": [{"url": "https://www.facebook.com/history"}], "resultsLimit": 5}
            )
            dataset = await self.apify_client.dataset(run["defaultDatasetId"]).list_items()
            
            trends = []
            for item in dataset.items:
                trends.append({
                    "title": item.get("text", "Facebook Post"),
                    "url": item.get("url"),
                    "source": "Facebook",
                    "engagement_score": item.get("likes", 0) + item.get("shares", 0)
                })
            return trends
        except Exception as e:
            logger.error("facebook_scrape_failed", extra={"error": str(e)})
            return []

    async def determine_best_seed(self) -> dict:
        """Aggregates trends across all platforms and picks the absolute best seed topic."""
        all_trends = []
        
        # 1. Reddit Trends
        for sub in self.subreddits:
            trends = await self.fetch_reddit_trends(sub)
            all_trends.extend(trends)
            
        # 2. YouTube Trends
        yt_trends = await self.fetch_youtube_trends()
        all_trends.extend(yt_trends)
        
        # 3. Google Trends
        gt_trends = await self.fetch_google_trends()
        all_trends.extend(gt_trends)
        
        # 4. TikTok Trends
        tt_trends = await self.fetch_tiktok_viral()
        all_trends.extend(tt_trends)
        
        # 5. Instagram Trends
        ig_trends = await self.fetch_instagram_viral()
        all_trends.extend(ig_trends)
        
        # 6. Facebook Trends
        fb_trends = await self.fetch_facebook_viral()
        all_trends.extend(fb_trends)
            
        if not all_trends:
            logger.warning("No trends found across any platform. Falling back to default seed.")
            return {"title": "The Fall of Rome", "niche": "History (Fallback)"}
            
        # Sort by engagement score (unified metric across platforms)
        all_trends.sort(key=lambda x: x.get("engagement_score", 0), reverse=True)
        
        # Pick the most viral topic overall
        best_trend = all_trends[0]
        
        logger.info("viral_trend_selected", extra={"trend": best_trend})
        
        return {
            "title": best_trend["title"],
            "niche": f"Trending on {best_trend.get('source', 'Web')}",
            "metrics": {
                "engagement_score": best_trend.get("engagement_score", 0),
                "url": best_trend.get("url", "")
            }
        }

if __name__ == "__main__":
    async def test():
        scraper = ViralTrendScraper()
        seed = await scraper.determine_best_seed()
        print("Selected Seed:", seed)
        
    asyncio.run(test())
