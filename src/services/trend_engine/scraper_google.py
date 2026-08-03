import httpx
import logging
import xml.etree.ElementTree as ET
from typing import List
from src.domain.trends import TrendCandidate

logger = logging.getLogger(__name__)

class GoogleTrendsScraper:
    def __init__(self, geo: str = "US"):
        self.geo = geo
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

    async def fetch_trends(self) -> List[TrendCandidate]:
        candidates = []
        url = f"https://trends.google.com/trending/rss?geo={self.geo}"
        
        async with httpx.AsyncClient(headers=self.headers, timeout=15.0) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
                
                root = ET.fromstring(response.text)
                
                # Namespaces used in Google Trends RSS
                namespaces = {'ht': 'https://trends.google.com/trending/rss'}
                
                for item in root.findall('.//item'):
                    title = item.find('title')
                    traffic_element = item.find('ht:approx_traffic', namespaces)
                    
                    if title is not None and title.text:
                        topic = title.text.strip()
                        traffic_str = traffic_element.text if traffic_element is not None else "0"
                        
                        # Parse "100,000+" to integer
                        try:
                            clean_traffic = traffic_str.replace(',', '').replace('+', '').strip()
                            traffic = int(clean_traffic)
                        except ValueError:
                            traffic = 0
                            
                        # Normalize engagement score based on expected max traffic for top trend (~5M)
                        max_expected_traffic = 5_000_000
                        engagement = min(traffic / max_expected_traffic, 1.0)
                        
                        candidates.append(
                            TrendCandidate(
                                topic=topic,
                                platform_source=f"google_trends/{self.geo}",
                                engagement_score=engagement,
                                raw_data={
                                    "approx_traffic": traffic_str,
                                    "parsed_traffic": traffic
                                }
                            )
                        )
            except Exception as e:
                logger.warning("google_scraper_failed_gracefully", extra={"error": str(e)})

        return candidates
