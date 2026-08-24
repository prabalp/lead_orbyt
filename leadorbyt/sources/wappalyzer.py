"""Wappalyzer's hosted lookup API -- technology stack by URL.

https://www.wappalyzer.com/docs/api/
"""

from .. import config
from .base import get_json


def enabled() -> bool:
    return bool(config.WAPPALYZER_API_KEY)


async def lookup_by_url(url: str) -> dict | None:
    if not enabled() or not url:
        return None

    data = await get_json(
        "https://api.wappalyzer.com/v2/lookup/",
        headers={"x-api-key": config.WAPPALYZER_API_KEY},
        params={"urls": url},
        source="wappalyzer",
    )
    if not data or not isinstance(data, list) or not data[0].get("technologies"):
        return None

    technologies = data[0]["technologies"]
    return {
        "wappalyzer_technologies": [t.get("name") for t in technologies],
        "wappalyzer_categories": sorted({c.get("name") for t in technologies for c in t.get("categories", [])}),
    }
