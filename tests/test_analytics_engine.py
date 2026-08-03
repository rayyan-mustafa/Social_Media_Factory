import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.services.analytics.source_youtube import YouTubeAnalyticsSource
from src.services.analytics.source_wordpress import WordPressAnalyticsSource
from src.services.analytics.source_stubs import SpotifyAnalyticsStub
from src.services.analytics.collector import AnalyticsCollector
from src.domain.analytics import PerformanceRecord
from src.db import YouTubeUpload, PerformanceRecord as DBPerformanceRecord
import httpx

@pytest.fixture
def mock_httpx_get():
    with patch("httpx.AsyncClient.get") as mock_get:
        yield mock_get

@pytest.mark.asyncio
async def test_youtube_analytics_source(mock_httpx_get, monkeypatch):
    # Mock settings to have an API key
    from src.core.config import Settings
    fake_settings = Settings(youtube_api_key="fake_key")
    monkeypatch.setattr("src.services.analytics.source_youtube.get_settings", lambda: fake_settings)
    
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "items": [
            {
                "id": "vid123",
                "statistics": {
                    "viewCount": "10500",
                    "likeCount": "500",
                    "commentCount": "50"
                }
            }
        ]
    }
    mock_response.raise_for_status = MagicMock()
    mock_httpx_get.return_value = mock_response

    source = YouTubeAnalyticsSource()
    record = await source.fetch_performance("vid123")
    
    assert record.platform == "youtube"
    assert record.platform_content_id == "vid123"
    assert record.views == 10500
    assert record.engagement_metrics["likes"] == 500
    assert record.engagement_metrics["comments"] == 50
    assert record.revenue_cents == 0

@pytest.mark.asyncio
async def test_wordpress_analytics_source(mock_httpx_get):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "id": 456,
        "views": 2000
    }
    mock_response.raise_for_status = MagicMock()
    mock_httpx_get.return_value = mock_response

    source = WordPressAnalyticsSource()
    record = await source.fetch_performance("456")
    
    assert record.platform == "wordpress"
    assert record.platform_content_id == "456"
    assert record.views == 2000

@pytest.mark.asyncio
async def test_spotify_stub():
    source = SpotifyAnalyticsStub()
    record = await source.fetch_performance("spot123")
    assert record.platform == "spotify"
    assert record.views == 0
    assert record.engagement_metrics.get("manual_import_required") is True

@pytest.mark.asyncio
async def test_analytics_collector():
    # Mock db session and redis
    mock_db = AsyncMock()
    mock_redis = AsyncMock()
    
    # Mock db result for select(YouTubeUpload)
    mock_upload = MagicMock(spec=YouTubeUpload)
    mock_upload.video_id = "test_vid_1"
    mock_upload.job_id = 10
    
    mock_result = MagicMock()
    mock_result.scalars().all.return_value = [mock_upload]
    mock_db.execute.return_value = mock_result
    
    collector = AnalyticsCollector(redis_client=mock_redis, db_session=mock_db)
    
    # Mock the youtube source inside the collector
    mock_yt_source = AsyncMock()
    mock_yt_source.fetch_performance.return_value = PerformanceRecord(
        platform="youtube",
        platform_content_id="test_vid_1",
        views=100
    )
    collector.sources["youtube"] = mock_yt_source
    
    await collector.run_collection_cycle()
    
    # Verify the source was called correctly
    mock_yt_source.fetch_performance.assert_called_with("test_vid_1")
    
    # Verify the database insertion was called (add + commit)
    assert mock_db.add.called
    added_record = mock_db.add.call_args[0][0]
    assert isinstance(added_record, DBPerformanceRecord)
    assert added_record.platform == "youtube"
    assert added_record.platform_content_id == "test_vid_1"
    assert added_record.views == 100
    assert added_record.job_id == 10
    
    assert mock_db.commit.called
    
    # Verify redis pub/sub was called
    mock_redis.publish.assert_called_with("events:analytics:updated", "collection_cycle_complete")
