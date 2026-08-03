"""Trend Engine for discovering topics across platforms."""

from src.services.trend_engine.aggregator import TrendAggregator
from src.services.trend_engine.scraper_youtube import YouTubeScraper
from src.services.trend_engine.scraper_google import GoogleTrendsScraper

__all__ = [
    "TrendAggregator",
    "YouTubeScraper",
    "GoogleTrendsScraper",
]
