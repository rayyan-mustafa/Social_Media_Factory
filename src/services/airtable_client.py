"""Airtable Integration for Business Manager.

Handles pushing viral topics to Airtable for human approval, 
and fetching approved topics back into the pipeline.
"""

import httpx
import os
from typing import List, Dict, Any
from src.core.logging import get_logger

logger = get_logger(__name__)

class AirtableClient:
    def __init__(self):
        self.api_key = os.getenv("AIRTABLE_API_KEY")
        self.base_id = os.getenv("AIRTABLE_BASE_ID")
        
        if not self.api_key or not self.base_id:
            logger.warning("airtable_credentials_missing", extra={"msg": "Airtable API Key or Base ID is not set."})
            
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
    def _get_url(self, table_name: str) -> str:
        return f"https://api.airtable.com/v0/{self.base_id}/{table_name}"

    async def push_topic(self, title: str, niche: str, metrics: Dict[str, Any], table_name: str = "YouTube_Shorts") -> bool:
        """Pushes a newly scraped topic to Airtable with 'Suggested' status."""
        if not self.api_key:
            return False
            
        payload = {
            "records": [
                {
                    "fields": {
                        "Topic": title,
                        "Niche": niche,
                        "Status": "Suggested",
                        "Upvotes": metrics.get("upvotes", 0),
                        "Comments": metrics.get("comments", 0)
                    }
                }
            ]
        }
        
        url = self._get_url(table_name)
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, headers=self.headers, json=payload, timeout=10.0)
                response.raise_for_status()
                logger.info("airtable_topic_pushed", extra={"topic": title, "table": table_name})
                return True
            except Exception as e:
                logger.error("airtable_push_failed", extra={"error": str(e), "topic": title, "table": table_name})
                return False

    async def get_approved_topics(self, table_name: str) -> List[Dict[str, Any]]:
        """Fetches topics from Airtable that have been marked 'Approved'."""
        if not self.api_key:
            return []
            
        # URL encode the formula: filterByFormula=Status="Approved"
        url = f"{self._get_url(table_name)}?filterByFormula=Status%3D%22Approved%22"
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, headers=self.headers, timeout=10.0)
                response.raise_for_status()
                data = response.json()
                
                approved = []
                for record in data.get("records", []):
                    approved.append({
                        "id": record["id"],
                        "topic": record["fields"].get("Topic"),
                        "niche": record["fields"].get("Niche", "History"),
                    })
                    
                return approved
            except Exception as e:
                logger.error("airtable_fetch_failed", extra={"error": str(e), "table": table_name})
                return []

    async def mark_topic_processing(self, record_id: str, table_name: str) -> bool:
        """Updates an Airtable record status from 'Approved' to 'Processing'."""
        if not self.api_key:
            return False
            
        payload = {
            "records": [
                {
                    "id": record_id,
                    "fields": {
                        "Status": "Processing"
                    }
                }
            ]
        }
        
        url = self._get_url(table_name)
        async with httpx.AsyncClient() as client:
            try:
                # Airtable uses PATCH for updating records
                response = await client.patch(url, headers=self.headers, json=payload, timeout=10.0)
                response.raise_for_status()
                logger.info("airtable_topic_marked_processing", extra={"record_id": record_id, "table": table_name})
                return True
            except Exception as e:
                logger.error("airtable_update_failed", extra={"error": str(e), "record_id": record_id, "table": table_name})
                return False
