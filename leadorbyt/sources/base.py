"""Shared async HTTP helper for the third-party data-source clients in this package.

Every module in `leadorbyt.sources` follows the same shape: check its own
API key in `config`, return `None` immediately if unset, otherwise make one
or two REST calls with this helper and return a plain dict (or `None` on
failure). None of these calls go through `backoff.py`/`browser_pool.py` --
those exist for scraping sites that actively fingerprint and block; these
are documented REST APIs with their own rate limits, so a plain timeout +
status check is enough.
"""

import logging

import httpx

from .. import config

logger = logging.getLogger("leadorbyt.sources")


async def request_json(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    json_body: dict | None = None,
    source: str = "",
) -> dict | list | None:
    """Make one HTTP request and return the parsed JSON body, or None on any failure.

    Failures (network error, timeout, non-2xx status, non-JSON body) are
    logged at warning level and swallowed -- a broken/rate-limited third-party
    source should never take down a discovery/enrichment run.
    """
    try:
        async with httpx.AsyncClient(timeout=config.SOURCE_HTTP_TIMEOUT) as client:
            response = await client.request(method, url, headers=headers, params=params, json=json_body)
        if response.status_code >= 400:
            logger.warning(f"{source or url}: HTTP {response.status_code}: {response.text[:200]}")
            return None
        return response.json()
    except httpx.HTTPError as exc:
        logger.warning(f"{source or url}: request failed: {exc}")
        return None
    except ValueError:
        logger.warning(f"{source or url}: non-JSON response")
        return None


async def get_json(url: str, **kwargs) -> dict | list | None:
    return await request_json("GET", url, **kwargs)


async def post_json(url: str, **kwargs) -> dict | list | None:
    return await request_json("POST", url, **kwargs)
