"""Yelp Fusion API -- business search.

https://docs.developer.yelp.com/reference/v3_business_search
"""

from .. import config
from .base import get_json


def enabled() -> bool:
    return bool(config.YELP_API_KEY)


async def search(term: str, location: str) -> dict | None:
    """Search Yelp Fusion for `term` (e.g. "plumbers") in `location`."""
    if not enabled():
        return None

    data = await get_json(
        "https://api.yelp.com/v3/businesses/search",
        headers={"Authorization": f"Bearer {config.YELP_API_KEY}"},
        params={"term": term, "location": location, "limit": 1},
        source="yelp",
    )
    if not data or not data.get("businesses"):
        return None

    business = data["businesses"][0]
    return {
        "yelp_id": business.get("id", ""),
        "yelp_name": business.get("name", ""),
        "yelp_url": business.get("url", ""),
        "yelp_phone": business.get("display_phone", ""),
        "yelp_rating": business.get("rating"),
        "yelp_review_count": business.get("review_count"),
        "yelp_categories": [c.get("title") for c in business.get("categories", [])],
        "yelp_is_closed": business.get("is_closed"),
    }
