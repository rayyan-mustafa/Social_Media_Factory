"""Google Places Text Search + Details. Optional Nominatim fallback."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

PLACES_TEXT = "https://maps.googleapis.com/maps/api/place/textsearch/json"
PLACES_DETAILS = "https://maps.googleapis.com/maps/api/place/details/json"
PLACES_NEW_SEARCH = "https://places.googleapis.com/v1/places:searchText"
NOMINATIM = "https://nominatim.openstreetmap.org/search"

_UA = "new_yt_automation-leadgen/1.0 (agency hunter; contact=ops)"


class PlacesError(RuntimeError):
    pass


def places_api_key() -> str:
    for env in (
        "GOOGLE_PLACES_API_KEY",
        "GOOGLE_MAPS_API_KEY",
        "GOOGLE_API_KEY",
        "YOUTUBE_API_KEY",
    ):
        v = (os.getenv(env) or "").strip()
        if v:
            return v
    try:
        from src.services.settings import get_settings

        s = get_settings()
        return (getattr(s, "youtube_api_key", None) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _get_json(url: str, *, headers: dict[str, str] | None = None, timeout: int = 25) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def _post_json(url: str, payload: dict[str, Any], *, headers: dict[str, str], timeout: int = 25) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "User-Agent": _UA, **headers}
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:400]
        raise PlacesError(f"Places New HTTP {exc.code}: {err_body}") from exc
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def _places_new_to_legacy(place: dict[str, Any]) -> dict[str, Any]:
    name = place.get("displayName") or {}
    title = name.get("text") if isinstance(name, dict) else str(name or "")
    pid = str(place.get("id") or "").replace("places/", "")
    return {
        "place_id": pid,
        "name": title,
        "formatted_address": str(place.get("formattedAddress") or ""),
        "website": str(place.get("websiteUri") or ""),
        "international_phone_number": str(place.get("internationalPhoneNumber") or ""),
        "formatted_phone_number": str(place.get("nationalPhoneNumber") or ""),
        "rating": place.get("rating"),
        "user_ratings_total": place.get("userRatingCount") or 0,
        "url": str(place.get("googleMapsUri") or ""),
        "types": place.get("types") or [],
        "business_status": str(place.get("businessStatus") or ""),
        "source": "places_new",
    }


def search_places_new(
    *,
    query: str,
    city: str,
    api_key: str,
    language: str = "en",
    limit: int = 25,
) -> list[dict[str, Any]]:
    q = f"{query} in {city}".strip()
    field_mask = ",".join(
        [
            "places.id",
            "places.displayName",
            "places.formattedAddress",
            "places.websiteUri",
            "places.nationalPhoneNumber",
            "places.internationalPhoneNumber",
            "places.rating",
            "places.userRatingCount",
            "places.googleMapsUri",
            "places.types",
            "places.businessStatus",
        ]
    )
    data = _post_json(
        PLACES_NEW_SEARCH,
        {"textQuery": q, "languageCode": language, "pageSize": min(20, max(1, int(limit)))},
        headers={
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": field_mask,
        },
    )
    if data.get("error"):
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else err
        raise PlacesError(f"Places New searchText: {msg}")
    out: list[dict[str, Any]] = []
    for place in data.get("places") or []:
        if isinstance(place, dict):
            out.append(_places_new_to_legacy(place))
        if len(out) >= int(limit):
            break
    return out


def search_places(
    *,
    query: str,
    city: str,
    api_key: str | None = None,
    language: str = "en",
    limit: int = 25,
    pause_s: float = 0.15,
) -> list[dict[str, Any]]:
    key = (api_key or places_api_key()).strip()
    if not key:
        raise PlacesError("no Places API key (set GOOGLE_PLACES_API_KEY)")
    try:
        return search_places_new(
            query=query, city=city, api_key=key, language=language, limit=limit
        )
    except PlacesError as exc:
        logger.info("Places New failed (%s); trying legacy textsearch", exc)
    except urllib.error.HTTPError as exc:
        logger.info("Places New HTTP %s; trying legacy textsearch", exc.code)
    q = f"{query} in {city}".strip()
    params = {"query": q, "language": language, "key": key}
    url = PLACES_TEXT + "?" + urllib.parse.urlencode(params)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _page in range(3):
        data = _get_json(url)
        status = str(data.get("status") or "")
        if status not in {"OK", "ZERO_RESULTS"}:
            raise PlacesError(
                f"Places textsearch {status}: {data.get('error_message') or data}"
            )
        for row in data.get("results") or []:
            pid = str(row.get("place_id") or "").strip()
            if not pid or pid in seen:
                continue
            seen.add(pid)
            out.append(row)
            if len(out) >= int(limit):
                return out[: int(limit)]
        token = str(data.get("next_page_token") or "").strip()
        if not token or len(out) >= int(limit):
            break
        time.sleep(2.1)
        url = PLACES_TEXT + "?" + urllib.parse.urlencode(
            {"pagetoken": token, "key": key, "language": language}
        )
        time.sleep(max(0.0, pause_s))
    return out[: int(limit)]


def place_details(place_id: str, *, api_key: str | None = None, language: str = "en") -> dict[str, Any]:
    key = (api_key or places_api_key()).strip()
    if not key:
        raise PlacesError("no Places API key")
    fields = ",".join(
        [
            "place_id",
            "name",
            "formatted_address",
            "formatted_phone_number",
            "international_phone_number",
            "website",
            "url",
            "rating",
            "user_ratings_total",
            "business_status",
            "types",
            "geometry",
        ]
    )
    url = PLACES_DETAILS + "?" + urllib.parse.urlencode(
        {"place_id": place_id, "fields": fields, "language": language, "key": key}
    )
    data = _get_json(url)
    status = str(data.get("status") or "")
    if status != "OK":
        logger.warning("place details %s: %s", place_id, status)
        return {}
    result = data.get("result")
    return result if isinstance(result, dict) else {}


def search_nominatim(*, query: str, city: str, limit: int = 25) -> list[dict[str, Any]]:
    """Degraded hunt when Places is denied. No ratings."""
    q = f"{query} {city}".strip()
    url = NOMINATIM + "?" + urllib.parse.urlencode(
        {"q": q, "format": "json", "addressdetails": 1, "limit": int(limit), "extratags": 1}
    )
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = json.loads(resp.read().decode("utf-8", errors="replace"))
    rows = raw if isinstance(raw, list) else []
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        extras = row.get("extratags") if isinstance(row.get("extratags"), dict) else {}
        name = str(row.get("name") or row.get("display_name") or "").split(",")[0].strip()
        out.append(
            {
                "place_id": f"osm_{row.get('osm_type')}_{row.get('osm_id')}",
                "name": name,
                "formatted_address": str(row.get("display_name") or ""),
                "website": str(extras.get("website") or extras.get("contact:website") or ""),
                "international_phone_number": str(
                    extras.get("phone") or extras.get("contact:phone") or ""
                ),
                "rating": None,
                "user_ratings_total": 0,
                "url": str(row.get("osm_id") or ""),
                "types": [str(row.get("type") or "establishment")],
                "source": "nominatim",
            }
        )
        time.sleep(1.05)
        if i + 1 >= int(limit):
            break
    return out
