"""ZoomInfo Enrich API -- PKI/JWT authentication flow.

ZoomInfo doesn't take a plain API key: you sign a short-lived RS256 JWT with
your account's private key (username + client_id as claims), exchange that
for an access token via /authenticate, then bearer-auth the real API calls
with the returned token. See https://api-docs.zoominfo.com/#authentication.

Requires `config.ZOOMINFO_USERNAME`, `config.ZOOMINFO_CLIENT_ID`, and
`config.ZOOMINFO_PRIVATE_KEY` (PEM contents, or a path to a `.pem` file).
"""

import time
from pathlib import Path

import jwt

from .. import config
from .base import get_json, post_json

_token_cache: dict = {"token": None, "expires_at": 0.0}


def enabled() -> bool:
    return bool(config.ZOOMINFO_USERNAME and config.ZOOMINFO_CLIENT_ID and config.ZOOMINFO_PRIVATE_KEY)


def _load_private_key() -> str:
    raw = config.ZOOMINFO_PRIVATE_KEY
    maybe_path = Path(raw)
    if maybe_path.is_file():
        return maybe_path.read_text()
    return raw


def _sign_jwt() -> str:
    now = int(time.time())
    claims = {
        "aud": "enterprise_api",
        "iss": config.ZOOMINFO_CLIENT_ID,
        "username": config.ZOOMINFO_USERNAME,
        "client_id": config.ZOOMINFO_CLIENT_ID,
        "iat": now,
        "exp": now + 300,
    }
    return jwt.encode(claims, _load_private_key(), algorithm="RS256")


async def _access_token() -> str | None:
    if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"]

    signed_jwt = _sign_jwt()
    data = await post_json(
        "https://api.zoominfo.com/authenticate",
        json_body={
            "username": config.ZOOMINFO_USERNAME,
            "clientId": config.ZOOMINFO_CLIENT_ID,
            "jwt": signed_jwt,
        },
        source="zoominfo_auth",
    )
    token = (data or {}).get("jwt")
    if not token:
        return None
    _token_cache["token"] = token
    _token_cache["expires_at"] = time.time() + 3300  # ZoomInfo access tokens last ~1h; refresh 5min early
    return token


async def enrich_company(domain: str) -> dict | None:
    if not enabled() or not domain:
        return None

    token = await _access_token()
    if not token:
        return None

    data = await post_json(
        "https://api.zoominfo.com/enrich/company",
        headers={"Authorization": f"Bearer {token}"},
        json_body={"matchCompanyInput": [{"companyWebsite": domain}]},
        source="zoominfo",
    )
    matches = (data or {}).get("result") or []
    if not matches or not matches[0].get("data"):
        return None

    company = matches[0]["data"][0]
    return {
        "zoominfo_name": company.get("name", ""),
        "zoominfo_industry": company.get("primaryIndustry", ""),
        "zoominfo_employee_count": company.get("employeeCount"),
        "zoominfo_revenue": company.get("revenue"),
    }
