"""Analytics Engine for collecting performance data across platforms."""

from src.services.analytics.protocols import PlatformAnalyticsSource
from src.services.analytics.collector import AnalyticsCollector
from src.services.analytics.source_youtube import YouTubeAnalyticsSource
from src.services.analytics.source_wordpress import WordPressAnalyticsSource
from src.services.analytics.source_stubs import SpotifyAnalyticsStub, KDPAnalyticsStub, ACXAnalyticsStub

__all__ = [
    "PlatformAnalyticsSource",
    "AnalyticsCollector",
    "YouTubeAnalyticsSource",
    "WordPressAnalyticsSource",
    "SpotifyAnalyticsStub",
    "KDPAnalyticsStub",
    "ACXAnalyticsStub",
]
