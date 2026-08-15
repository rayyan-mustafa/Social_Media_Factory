# src/agents/leadgen/scraper.py
"""Scraper module for Lead‑Gen pipeline.
Uses Google Places Text Search to retrieve business listings.
Falls back to Nominatim geocoding if Google API key is missing or request fails.
"""
import os
import requests
from typing import List, Dict
from .config import config

GOOGLE_PLACES_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"

def search_google_places(city: str, niche: str) -> List[Dict]:
    """Search Google Places Text Search for ``niche`` in ``city``.
    Returns a list of dicts with required fields.
    """
    api_key = config.google_api_key
    if not api_key or api_key == "PLACEHOLDER":
        raise RuntimeError("Google API key not configured")
    query = f"{niche} in {city}"
    params = {
        "query": query,
        "key": api_key,
    }
    resp = requests.get(GOOGLE_PLACES_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    results = []
    for place in data.get("results", []):
        # Extract required fields; use placeholders if missing.
        result = {
            "business_name": place.get("name"),
            "phone": place.get("formatted_phone_number"),
            "address": place.get("formatted_address"),
            "review_count": place.get("user_ratings_total"),
            "rating": place.get("rating"),
            "website_url": place.get("website"),
            "category": ", ".join(place.get("types", [])),
        }
        results.append(result)
    return results

def search_nominatim(city: str, niche: str) -> List[Dict]:
    """Fallback scraper using Nominatim.
    Performs a search for ``niche`` within ``city`` and returns minimal info.
    """
    query = f"{niche} {city}"
    params = {
        "q": query,
        "format": "json",
        "addressdetails": 1,
        "limit": 20,
    }
    headers = {"User-Agent": "leadgen-scraper/1.0"}
    resp = requests.get(NOMINATIM_SEARCH_URL, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    places = resp.json()
    results = []
    for p in places:
        result = {
            "business_name": p.get("display_name"),
            "phone": None,
            "address": p.get("display_name"),
            "review_count": None,
            "rating": None,
            "website_url": None,
            "category": p.get("type"),
        }
        results.append(result)
    return results

def scrape(city: str, niche: str) -> List[Dict]:
    """High‑level entry point.
    Tries Google Places first; on failure falls back to Nominatim.
    """
    try:
        return search_google_places(city, niche)
    except Exception as e:
        # Log and fallback
        print(f"Google Places failed ({e}); falling back to Nominatim")
        return search_nominatim(city, niche)
