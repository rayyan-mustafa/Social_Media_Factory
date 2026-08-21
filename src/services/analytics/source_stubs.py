import logging
from src.domain.analytics import PerformanceRecord

logger = logging.getLogger(__name__)

class SpotifyAnalyticsStub:
    """Stub for Spotify analytics (requires manual CSV export)."""
    async def fetch_performance(self, content_id: str) -> PerformanceRecord:
        logger.warning(f"Spotify analytics API not available. Manual CSV import required for {content_id}.")
        return PerformanceRecord(
            platform="spotify",
            platform_content_id=content_id,
            views=0,
            engagement_metrics={"manual_import_required": True}
        )

class KDPAnalyticsStub:
    """Stub for Kindle Direct Publishing analytics (requires manual CSV export)."""
    async def fetch_performance(self, content_id: str) -> PerformanceRecord:
        logger.warning(f"KDP analytics API not available. Manual CSV import required for {content_id}.")
        return PerformanceRecord(
            platform="kdp",
            platform_content_id=content_id,
            views=0,
            engagement_metrics={"manual_import_required": True}
        )

class ACXAnalyticsStub:
    """Stub for Audiobook Creation Exchange analytics (requires manual CSV export)."""
    async def fetch_performance(self, content_id: str) -> PerformanceRecord:
        logger.warning(f"ACX analytics API not available. Manual CSV import required for {content_id}.")
        return PerformanceRecord(
            platform="acx",
            platform_content_id=content_id,
            views=0,
            engagement_metrics={"manual_import_required": True}
        )
