"""Snov.io domain email search -- OAuth2 client-credentials auth (no static API key).

https://snov.io/api
"""

import time

from .. import config
from .base import get_json, post_json

_token_cache: dict = {"token": None, "expires_at": 0.0}


def enabled() -> bool:
    return bool(config.SNOV_CLIENT_ID and config.SNOV_CLIENT_SECRET)


async def _access_token() -> str | None:
    if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"]

    data = await post_json(
        "https://api.snov.io/v1/oauth/access_token",
        json_body={
            "grant_type": "client_credentials",
            "client_id": config.SNOV_CLIENT_ID,
            "client_secret": config.SNOV_CLIENT_SECRET,
        },
        source="snov",
    )
    token = (data or {}).get("access_token")
    if not token:
        return None
    _token_cache["token"] = token
    _token_cache["expires_at"] = time.time() + float((data or {}).get("expires_in", 3600)) - 60
    return token


async def domain_search(domain: str) -> dict | None:
    if not enabled() or not domain:
        return None

    token = await _access_token()
    if not token:
        return None

    data = await get_json(
        "https://api.snov.io/v2/domain-emails-with-info",
        params={"domain": domain, "type": "all", "limit": 20, "access_token": token},
        source="snov",
    )
    emails = (data or {}).get("emails") or []
    if not emails:
        return None
    return {"snov_emails": [e.get("email") for e in emails if e.get("email")]}
