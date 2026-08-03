import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.services.trend_engine.scraper_youtube import YouTubeScraper
from src.services.trend_engine.scraper_google import GoogleTrendsScraper
from src.services.trend_engine.aggregator import TrendAggregator
from src.domain.trends import TrendCandidate
from redis.asyncio import Redis
import httpx

@pytest.fixture
def mock_httpx_get():
    with patch("httpx.AsyncClient.get") as mock_get:
        yield mock_get

@pytest.mark.asyncio
async def test_youtube_scraper(mock_httpx_get, monkeypatch):
    # Mock settings to have an API key
    from src.core.config import Settings
    fake_settings = Settings(youtube_api_key="fake_key")
    monkeypatch.setattr("src.services.trend_engine.scraper_youtube.get_settings", lambda: fake_settings)
    
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "items": [
            {
                "id": "vid123",
                "snippet": {
                    "title": "Viral YouTube Video",
                    "channelTitle": "Cool Channel"
                },
                "statistics": {
                    "viewCount": "5000000",
                    "likeCount": "200000"
                }
            }
        ]
    }
    mock_response.raise_for_status = MagicMock()
    mock_httpx_get.return_value = mock_response

    scraper = YouTubeScraper()
    trends = await scraper.fetch_trends()
    
    assert len(trends) == 1
    assert trends[0].topic == "Viral YouTube Video"
    assert trends[0].engagement_score == 0.5  # 5,000,000 / 10,000,000

@pytest.mark.asyncio
async def test_google_trends_scraper(mock_httpx_get):
    xml_data = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:ht="https://trends.google.com/trending/rss">
      <channel>
        <item>
          <title>OpenAI News</title>
          <ht:approx_traffic>200,000+</ht:approx_traffic>
        </item>
      </channel>
    </rss>
    """
    mock_response = MagicMock()
    mock_response.text = xml_data
    mock_response.raise_for_status = MagicMock()
    mock_httpx_get.return_value = mock_response

    scraper = GoogleTrendsScraper()
    trends = await scraper.fetch_trends()
    
    assert len(trends) == 1
    assert trends[0].topic == "OpenAI News"
    assert trends[0].raw_data["parsed_traffic"] == 200000

@pytest.mark.asyncio
async def test_trend_aggregator():
    redis_mock = AsyncMock(spec=Redis)
    pipeline_mock = AsyncMock()
    redis_mock.pipeline.return_value = pipeline_mock
    
    aggregator = TrendAggregator(redis_mock)
    
    # Mock the scrapers to return dummy data
    t2 = TrendCandidate(topic="artificial intelligence!", platform_source="youtube/trending/US", engagement_score=0.3)
    t3 = TrendCandidate(topic="Unrelated topic", platform_source="google_trends/US", engagement_score=0.1)
    
    mock_youtube = AsyncMock()
    mock_youtube.fetch_trends.return_value = [t2]
    mock_google = AsyncMock()
    mock_google.fetch_trends.return_value = [t3]
    
    aggregator.scrapers = {"youtube": mock_youtube, "google": mock_google}
    
    trends = await aggregator.aggregate_and_store()
    
    # "artificial intelligence!" should be there with 0.3
    assert len(trends) == 2
    merged_ai = next(t for t in trends if "intelligence" in t.topic.lower())
    assert merged_ai.engagement_score == 0.3
    assert "youtube" in merged_ai.platform_source
    
    # Check that redis pipeline methods were called
    pipeline_mock.delete.assert_called_with("trends:candidates")
    assert pipeline_mock.zadd.called
    pipeline_mock.expire.assert_called_with("trends:candidates", 86400)
    assert pipeline_mock.execute.called

