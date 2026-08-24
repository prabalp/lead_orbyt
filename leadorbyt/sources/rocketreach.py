"""RocketReach company lookup by domain.

https://rocketreach.co/api/v2/docs
"""

from .. import config
from .base import get_json


def enabled() -> bool:
    return bool(config.ROCKETREACH_API_KEY)


async def lookup_company(domain: str) -> dict | None:
    if not enabled() or not domain:
        return None

    data = await get_json(
        "https://api.rocketreach.co/api/v2/company/lookup",
        headers={"Api-Key": config.ROCKETREACH_API_KEY},
        params={"domain": domain},
        source="rocketreach",
    )
    if not data or not data.get("name"):
        return None
    return {
        "rocketreach_name": data.get("name", ""),
        "rocketreach_industry": data.get("industry", ""),
        "rocketreach_employees": data.get("employees"),
        "rocketreach_linkedin_url": data.get("linkedin_url", ""),
    }
