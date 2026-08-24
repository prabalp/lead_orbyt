"""Findymail domain email finder.

https://findymail.com/api-documentation
"""

from .. import config
from .base import post_json


def enabled() -> bool:
    return bool(config.FINDYMAIL_API_KEY)


async def find_domain_emails(domain: str) -> dict | None:
    if not enabled() or not domain:
        return None

    data = await post_json(
        "https://app.findymail.com/api/search/domain",
        headers={"Authorization": f"Bearer {config.FINDYMAIL_API_KEY}"},
        json_body={"domain": domain},
        source="findymail",
    )
    contacts = (data or {}).get("contacts") or []
    if not contacts:
        return None
    return {"findymail_emails": [c.get("email") for c in contacts if c.get("email")]}
