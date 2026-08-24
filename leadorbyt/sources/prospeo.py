"""Prospeo domain email finder.

https://prospeo.io/api/documentation
"""

from .. import config
from .base import post_json


def enabled() -> bool:
    return bool(config.PROSPEO_API_KEY)


async def domain_search(domain: str) -> dict | None:
    if not enabled() or not domain:
        return None

    data = await post_json(
        "https://api.prospeo.io/domain-search",
        headers={"X-KEY": config.PROSPEO_API_KEY},
        json_body={"company": domain},
        source="prospeo",
    )
    response = (data or {}).get("response") or {}
    emails = response.get("email_list") or []
    if not emails:
        return None
    return {"prospeo_emails": [e.get("email") for e in emails if e.get("email")]}
