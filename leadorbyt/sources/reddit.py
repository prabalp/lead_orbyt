"""Reddit signal search: finds posts/comments matching a keyword query, for
the signal-based lead discovery path (see signal_jobs.py).

COMPLIANCE NOTE, deliberate and disclosed (not an oversight): Reddit's API
terms name "lead generation" as commercial use requiring Reddit's
paid/contracted API access -- the free tier (~100 req/min, no dollar cost)
is scoped to personal/non-commercial use (research, bots, moderation). This
module authenticates as a normal OAuth "script" app on that free tier and
uses it for exactly the purpose ("lead generation") Reddit's terms say
requires their commercial agreement. No workaround or evasion of any kind --
same credentials, same documented endpoints, just outside the licensed
scope for this tier. The operator (not leadorbyt) is responsible for
Reddit's terms; this note exists so that choice stays visible in the code,
not just in a chat transcript.

Confirmed against Reddit's own OAuth2 docs, not assumed:
    https://github.com/reddit-archive/reddit/wiki/oauth2

- Token endpoint: `POST https://www.reddit.com/api/v1/access_token`,
  `grant_type=client_credentials`, HTTP Basic auth (client_id as username,
  client_secret as password). Tokens last 1 hour; app-only OAuth never
  returns a refresh_token, so a new token is simply requested again on expiry.
- Search: `GET https://oauth.reddit.com/search` (global) or
  `GET https://oauth.reddit.com/r/{subreddit}/search` with
  `restrict_sr=true` (subreddit-scoped), paginated via the `after` cursor.
- Reddit rejects generic/default User-Agent strings -- REDDIT_USER_AGENT
  must be set to something identifying (e.g. "leadorbyt/0.1 by u/yourname").

Rate limiting here is a plain pacing gate, not backoff.py's block-detection
apparatus -- backoff.py exists for scrape targets that actively fingerprint
and fight back; this is a documented, known rate limit (not an adversary),
so a minimum-interval sleep floor between calls is enough.
"""

import asyncio
import time

import httpx

from .. import config

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"

_token: str | None = None
_token_expires_at: float = 0.0
_token_lock = asyncio.Lock()

_rate_lock = asyncio.Lock()
_last_request_at: float = 0.0


def enabled() -> bool:
    return bool(config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET and config.REDDIT_USER_AGENT)


async def _get_token() -> str | None:
    global _token, _token_expires_at
    async with _token_lock:
        if _token is not None and time.time() < _token_expires_at - 60:
            return _token

        try:
            async with httpx.AsyncClient(timeout=config.SOURCE_HTTP_TIMEOUT) as client:
                response = await client.post(
                    TOKEN_URL,
                    auth=(config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET),
                    data={"grant_type": "client_credentials"},
                    headers={"User-Agent": config.REDDIT_USER_AGENT},
                )
            if response.status_code >= 400:
                return None
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return None

        _token = data.get("access_token")
        _token_expires_at = time.time() + data.get("expires_in", 3600)
        return _token


async def _paced_get(url: str, params: dict, token: str) -> dict | None:
    """Enforce a minimum interval between requests (config.REDDIT_MAX_REQUESTS_PER_MINUTE)."""
    global _last_request_at
    min_interval = 60.0 / config.REDDIT_MAX_REQUESTS_PER_MINUTE
    async with _rate_lock:
        wait = _last_request_at + min_interval - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = time.time()

    try:
        async with httpx.AsyncClient(timeout=config.SOURCE_HTTP_TIMEOUT) as client:
            response = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}", "User-Agent": config.REDDIT_USER_AGENT},
            )
        if response.status_code >= 400:
            return None
        return response.json()
    except (httpx.HTTPError, ValueError):
        return None


def _normalize_post(post: dict, matched_query: str) -> dict | None:
    author = post.get("author", "")
    if not author or author == "[deleted]":
        return None
    permalink = post.get("permalink", "")
    return {
        "reddit_post_id": post.get("id", ""),
        "reddit_username": author,
        "subreddit": post.get("subreddit", ""),
        "post_title": post.get("title", ""),
        "post_body": (post.get("selftext") or "")[:500],
        "permalink": f"https://reddit.com{permalink}" if permalink else "",
        "created_utc": post.get("created_utc"),
        "matched_query": matched_query,
    }


async def search_signals(
    query: str,
    subreddits: list[str] | None,
    sort: str = "new",
    time_filter: str = "week",
    max_results: int = 20,
    access_token: str | None = None,
) -> list[dict]:
    """Search Reddit posts matching `query`, optionally restricted to `subreddits`.

    Global search when `subreddits` is empty/None; otherwise queries each
    subreddit separately (Reddit's restrict_sr search has no cross-subreddit
    OR mode) and merges results, paginating via `after` within each.
    `access_token` is a user OAuth token from social_connect; otherwise the
    app-only client_credentials token is used when configured.
    """
    if not query:
        return []

    token = access_token
    if not token:
        if not enabled():
            return []
        token = await _get_token()
    if token is None:
        return []

    targets = [f"{API_BASE}/r/{sub}/search" for sub in subreddits] if subreddits else [f"{API_BASE}/search"]

    results: list[dict] = []
    for url in targets:
        after = None
        while len(results) < max_results:
            params = {
                "q": query,
                "sort": sort,
                "t": time_filter,
                "limit": min(max_results - len(results), 100),
                "restrict_sr": "true" if subreddits else "false",
            }
            if after:
                params["after"] = after

            data = await _paced_get(url, params, token)
            if not data:
                break

            listing = (data.get("data") or {}).get("children", [])
            if not listing:
                break

            for child in listing:
                normalized = _normalize_post(child.get("data", {}), query)
                if normalized is not None:
                    results.append(normalized)
                if len(results) >= max_results:
                    break

            after = (data.get("data") or {}).get("after")
            if not after:
                break

    return results[:max_results]
