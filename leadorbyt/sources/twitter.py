"""X (Twitter) API v2 -- resolve a business's account by @handle or search by name.

https://developer.x.com/en/docs/x-api/users/lookup/api-reference/get-users-by-username-username
"""

from .. import config
from .base import get_json

USER_FIELDS = "public_metrics,description,verified,url"


def enabled() -> bool:
    return bool(config.X_BEARER_TOKEN)


async def lookup_by_handle(handle: str) -> dict | None:
    if not enabled() or not handle:
        return None

    handle = handle.lstrip("@")
    data = await get_json(
        f"https://api.x.com/2/users/by/username/{handle}",
        headers={"Authorization": f"Bearer {config.X_BEARER_TOKEN}"},
        params={"user.fields": USER_FIELDS},
        source="x_twitter",
    )
    user = (data or {}).get("data")
    if not user:
        return None

    metrics = user.get("public_metrics", {})
    return {
        "x_handle": user.get("username", ""),
        "x_name": user.get("name", ""),
        "x_description": user.get("description", ""),
        "x_verified": user.get("verified"),
        "x_followers_count": metrics.get("followers_count"),
        "x_following_count": metrics.get("following_count"),
        "x_tweet_count": metrics.get("tweet_count"),
    }
