"""Foursquare Places API -- place search.

https://location.foursquare.com/developer/reference/place-search
"""

from .. import config
from .base import get_json


def enabled() -> bool:
    return bool(config.FOURSQUARE_API_KEY)


async def search(query: str, near: str) -> dict | None:
    """Search Foursquare Places for `query` (e.g. "bakery") `near` a location string."""
    if not enabled():
        return None

    data = await get_json(
        "https://places-api.foursquare.com/places/search",
        headers={
            "Authorization": f"Bearer {config.FOURSQUARE_API_KEY}",
            "X-Places-Api-Version": "2025-06-17",
        },
        params={"query": query, "near": near, "limit": 1},
        source="foursquare",
    )
    if not data or not data.get("results"):
        return None

    place = data["results"][0]
    location = place.get("location", {})
    return {
        "foursquare_id": place.get("fsq_place_id", place.get("fsq_id", "")),
        "foursquare_name": place.get("name", ""),
        "foursquare_address": location.get("formatted_address", ""),
        "foursquare_categories": [c.get("name") for c in place.get("categories", [])],
        "foursquare_website": place.get("website", ""),
        "foursquare_phone": place.get("tel", ""),
    }
