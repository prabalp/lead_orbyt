"""Google Places API (New) -- Text Search + Place Details.

https://developers.google.com/maps/documentation/places/web-service/text-search
"""

from .. import config
from .base import post_json

FIELD_MASK = (
    "places.displayName,places.formattedAddress,places.internationalPhoneNumber,"
    "places.websiteUri,places.rating,places.userRatingCount,places.businessStatus,"
    "places.types"
)


def enabled() -> bool:
    return bool(config.GOOGLE_PLACES_API_KEY)


async def search(query: str) -> dict | None:
    """Text-search Google Places for `query` (e.g. "coffee shops in Austin, TX")."""
    if not enabled():
        return None

    data = await post_json(
        "https://places.googleapis.com/v1/places:searchText",
        headers={
            "X-Goog-Api-Key": config.GOOGLE_PLACES_API_KEY,
            "X-Goog-FieldMask": FIELD_MASK,
            "Content-Type": "application/json",
        },
        json_body={"textQuery": query},
        source="google_places",
    )
    if not data or not data.get("places"):
        return None

    place = data["places"][0]
    return {
        "google_place_name": place.get("displayName", {}).get("text", ""),
        "google_place_address": place.get("formattedAddress", ""),
        "google_place_phone": place.get("internationalPhoneNumber", ""),
        "google_place_website": place.get("websiteUri", ""),
        "google_place_rating": place.get("rating"),
        "google_place_rating_count": place.get("userRatingCount"),
        "google_place_business_status": place.get("businessStatus", ""),
        "google_place_types": place.get("types", []),
    }
