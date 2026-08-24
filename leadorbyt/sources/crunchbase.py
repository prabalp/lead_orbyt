"""Crunchbase API v4 -- organization lookup by domain (autocomplete + entity fetch).

https://data.crunchbase.com/docs/using-the-api
"""

from .. import config
from .base import get_json

CARD_FIELDS = "identifier,short_description,website,founded_on,num_employees_enum,categories,location_identifiers"


def enabled() -> bool:
    return bool(config.CRUNCHBASE_API_KEY)


async def lookup_by_domain(domain: str) -> dict | None:
    if not enabled() or not domain:
        return None

    headers = {"X-cb-user-key": config.CRUNCHBASE_API_KEY}

    autocomplete = await get_json(
        "https://api.crunchbase.com/api/v4/autocompletes",
        headers=headers,
        params={"query": domain, "collection_ids": "organizations", "limit": 1},
        source="crunchbase",
    )
    entities = (autocomplete or {}).get("entities") or []
    if not entities:
        return None
    permalink = entities[0].get("identifier", {}).get("permalink", "")
    if not permalink:
        return None

    detail = await get_json(
        f"https://api.crunchbase.com/api/v4/entities/organizations/{permalink}",
        headers=headers,
        params={"card_ids": "fields", "field_ids": CARD_FIELDS},
        source="crunchbase",
    )
    props = (detail or {}).get("properties", {})
    return {
        "crunchbase_permalink": permalink,
        "crunchbase_description": props.get("short_description", ""),
        "crunchbase_website": (props.get("website") or {}).get("value", ""),
        "crunchbase_founded_on": (props.get("founded_on") or {}).get("value", ""),
        "crunchbase_employee_range": props.get("num_employees_enum", ""),
        "crunchbase_categories": [c.get("value") for c in props.get("categories", []) or []],
    }
