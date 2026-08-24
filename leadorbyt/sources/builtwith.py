"""BuiltWith Free/Domain API -- technology stack lookup by domain.

https://api.builtwith.com/free-api
"""

from .. import config
from .base import get_json


def enabled() -> bool:
    return bool(config.BUILTWITH_API_KEY)


async def lookup_by_domain(domain: str) -> dict | None:
    if not enabled() or not domain:
        return None

    data = await get_json(
        "https://api.builtwith.com/free1/api.json",
        params={"KEY": config.BUILTWITH_API_KEY, "LOOKUP": domain},
        source="builtwith",
    )
    groups = (data or {}).get("Results", [{}])[0].get("Result", {}).get("Paths", [{}])
    technologies: list[str] = []
    for path in groups:
        for tech in path.get("Technologies", []):
            name = tech.get("Name")
            if name:
                technologies.append(name)
    if not technologies:
        return None
    return {"builtwith_technologies": sorted(set(technologies))}
