"""OpenStreetMap / Overpass API lookup -- free, keyless, no rate-limit key.

Searches for a named POI within a radius of a lat/lon point via the Overpass
QL API. Useful as a free cross-check/fallback for discovery.py's Google
Maps scrape.
"""

from .. import config
from .base import get_json

ENABLED = True  # always on; no API key required


def _build_query(name: str, lat: float, lon: float, radius_m: int) -> str:
    escaped = name.replace('"', '\\"')
    return f"""
    [out:json][timeout:{int(config.SOURCE_HTTP_TIMEOUT)}];
    (
      node["name"~"{escaped}",i](around:{radius_m},{lat},{lon});
      way["name"~"{escaped}",i](around:{radius_m},{lat},{lon});
    );
    out center 5;
    """


async def lookup_by_coords(name: str, lat: float, lon: float, radius_m: int = 200) -> dict | None:
    """Find an OSM node/way matching `name` near (lat, lon)."""
    data = await get_json(
        config.OVERPASS_API_URL,
        params={"data": _build_query(name, lat, lon, radius_m)},
        source="osm",
    )
    if not data or not data.get("elements"):
        return None
    element = data["elements"][0]
    tags = element.get("tags", {})
    return {
        "osm_id": element.get("id"),
        "osm_type": element.get("type"),
        "osm_tags": tags,
        "osm_website": tags.get("website") or tags.get("contact:website", ""),
        "osm_phone": tags.get("phone") or tags.get("contact:phone", ""),
    }
