"""X recent-search via a user OAuth token (official API v2).

Only used when the tenant has completed `/connect/x`. App-only
`X_BEARER_TOKEN` lookups in twitter.py stay separate.
https://developer.x.com/en/docs/twitter-api/tweets/search/api-reference/get-tweets-search-recent
"""

from __future__ import annotations

import httpx

from .. import config


async def search_recent(query: str, access_token: str, max_results: int = 10) -> list[dict]:
    if not query or not access_token:
        return []
    limit = max(10, min(max_results, 100))
    try:
        async with httpx.AsyncClient(timeout=config.SOURCE_HTTP_TIMEOUT) as client:
            response = await client.get(
                "https://api.x.com/2/tweets/search/recent",
                params={
                    "query": query,
                    "max_results": str(limit),
                    "tweet.fields": "created_at,author_id",
                    "expansions": "author_id",
                    "user.fields": "username",
                },
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.status_code >= 400:
            return []
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return []

    users = {
        u.get("id"): u.get("username", "")
        for u in (payload.get("includes") or {}).get("users") or []
    }
    rows: list[dict] = []
    for tweet in payload.get("data") or []:
        tweet_id = tweet.get("id", "")
        username = users.get(tweet.get("author_id"), "")
        url = f"https://x.com/{username}/status/{tweet_id}" if username and tweet_id else ""
        if not url:
            continue
        rows.append(
            {
                "source": "x",
                "author": username,
                "community": "x",
                "post_title": (tweet.get("text") or "")[:120],
                "post_body": (tweet.get("text") or "")[:500],
                "url": url,
                "email": "",
                "discovered_by_query": query,
            }
        )
    return rows
