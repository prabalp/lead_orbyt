"""Hunter.io Domain Search -- finds email addresses/pattern for a domain.

https://hunter.io/api-documentation/v2#domain-search
"""

from .. import config
from .base import get_json


def enabled() -> bool:
    return bool(config.HUNTER_API_KEY)


async def domain_search(domain: str) -> dict | None:
    if not enabled() or not domain:
        return None

    data = await get_json(
        "https://api.hunter.io/v2/domain-search",
        params={"domain": domain, "api_key": config.HUNTER_API_KEY, "limit": 5},
        source="hunter",
    )
    result = (data or {}).get("data")
    if not result:
        return None
    return {
        "hunter_email_pattern": result.get("pattern", ""),
        "hunter_organization": result.get("organization", ""),
        "hunter_emails": [e.get("value") for e in result.get("emails", [])],
    }
