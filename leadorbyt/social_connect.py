"""Official OAuth login for social networks that have a public user API.

This is NOT "type your LinkedIn password into our stealth browser." That
would scrape authenticated pages those networks forbid. The only logins we
run are documented OAuth 2 authorization-code flows:

- Reddit (`identity` + `read`) — same API `sources/reddit.py` already uses
- X (`tweet.read users.read offline.access`) — recent-search API

LinkedIn and Facebook have no self-serve lead-search API. Public `site:`
results still work without login. Connecting them here would only collect
credentials we cannot lawfully use to scrape their apps.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from urllib.parse import urlencode

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import auth, config, store
from .errors import ErrorType, LeadOrbytError

logger = logging.getLogger("leadorbyt.social_connect")

TICKET_TTL_SECONDS = 30 * 60
STATE_TTL_SECONDS = 15 * 60
CONNECTABLE = ("reddit", "x")
UNSUPPORTED = {
    "linkedin": (
        "LinkedIn has no public self-serve search API. Logging into LinkedIn "
        "so a scraper can read your feed would violate their Terms of Service. "
        "find_web_signals still lists public LinkedIn posts via site: search."
    ),
    "facebook": (
        "Facebook/Meta does not offer a self-serve API for searching other "
        "people's posts as leads. We do not collect Facebook passwords or "
        "session cookies. Public Facebook URLs still appear in site: search."
    ),
}

_registered = False


def _fernet():
    secret = (config.TOKEN_ENCRYPTION_KEY or "").encode("utf-8")
    if not secret:
        return None
    from cryptography.fernet import Fernet

    try:
        return Fernet(secret)
    except Exception:
        digest = hashlib.sha256(secret).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plain: str) -> str:
    box = _fernet()
    if box is None:
        raise LeadOrbytError(
            ErrorType.INVALID_INPUT,
            "social login is disabled until LEADORBYT_TOKEN_ENCRYPTION_KEY is set",
        )
    return box.encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    box = _fernet()
    if box is None or not token:
        return ""
    return box.decrypt(token.encode("utf-8")).decode("utf-8")


def encryption_ready() -> bool:
    return bool(config.TOKEN_ENCRYPTION_KEY)


def redirect_uri(provider: str) -> str:
    return f"{config.PUBLIC_URL}/connect/{provider}/callback"


def provider_configured(provider: str) -> bool:
    if provider == "reddit":
        return bool(config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET and config.REDDIT_USER_AGENT)
    if provider == "x":
        return bool(config.X_OAUTH_CLIENT_ID)
    return False


def connection_summary(user_id: str) -> dict:
    connected = {row["provider"]: row for row in store.list_connected_accounts(user_id)}
    providers = []
    for name in CONNECTABLE:
        row = connected.get(name)
        providers.append(
            {
                "provider": name,
                "connectable": True,
                "configured": provider_configured(name),
                "connected": row is not None,
                "account_label": (row or {}).get("account_label") or "",
            }
        )
    for name, reason in UNSUPPORTED.items():
        providers.append(
            {
                "provider": name,
                "connectable": False,
                "configured": False,
                "connected": False,
                "account_label": "",
                "reason": reason,
            }
        )
    return {
        "encryption_ready": encryption_ready(),
        "providers": providers,
    }


def start_connect(user_id: str, provider: str) -> dict:
    name = (provider or "").strip().lower()
    if name in UNSUPPORTED:
        return {"status": "unsupported", "provider": name, "reason": UNSUPPORTED[name]}
    if name not in CONNECTABLE:
        raise LeadOrbytError(ErrorType.INVALID_INPUT, f"unknown provider {provider!r}")
    if not encryption_ready():
        raise LeadOrbytError(
            ErrorType.INVALID_INPUT,
            "operator must set LEADORBYT_TOKEN_ENCRYPTION_KEY before social login",
        )
    if not provider_configured(name):
        raise LeadOrbytError(
            ErrorType.INVALID_INPUT,
            f"{name} OAuth is not configured on this host (client id/secret)",
        )
    raw = secrets.token_urlsafe(32)
    store.create_oauth_ticket(
        user_id, name, auth.hash_key(raw), time.time() + TICKET_TTL_SECONDS
    )
    login_url = f"{config.PUBLIC_URL}/connect/{name}?ticket={raw}"
    return {
        "status": "login_required",
        "provider": name,
        "login_url": login_url,
        "expires_in_seconds": TICKET_TTL_SECONDS,
        "next_action": (
            f"Ask the user to open login_url in their browser and sign in to {name}. "
            "Then retry the search. Do not ask them for a password to paste here."
        ),
    }


def valid_access_token(user_id: str, provider: str) -> str | None:
    row = store.get_connected_account(user_id, provider)
    if row is None:
        return None
    expires_at = row.get("expires_at") or 0
    if expires_at and expires_at < time.time() + 60:
        refreshed = _refresh_token(user_id, provider, row)
        if not refreshed:
            return None
        return refreshed
    try:
        return decrypt_secret(row["access_token_enc"])
    except Exception:
        logger.warning("Could not decrypt %s token for user %s", provider, user_id)
        return None


def _refresh_token(user_id: str, provider: str, row: dict) -> str | None:
    refresh = ""
    try:
        refresh = decrypt_secret(row.get("refresh_token_enc") or "")
    except Exception:
        return None
    if not refresh:
        return None
    try:
        if provider == "reddit":
            tokens = _reddit_refresh(refresh)
        elif provider == "x":
            tokens = _x_refresh(refresh)
        else:
            return None
    except Exception:
        logger.exception("Refresh failed for %s", provider)
        return None
    _store_tokens(user_id, provider, tokens, row.get("account_label") or "")
    return tokens["access_token"]


def _store_tokens(user_id: str, provider: str, tokens: dict, account_label: str) -> None:
    expires_in = int(tokens.get("expires_in") or 3600)
    refresh = tokens.get("refresh_token") or ""
    store.upsert_connected_account(
        user_id,
        provider,
        encrypt_secret(tokens["access_token"]),
        encrypt_secret(refresh) if refresh else "",
        time.time() + expires_in,
        account_label,
    )


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _authorize_url(provider: str, state: str, code_challenge: str) -> str:
    if provider == "reddit":
        params = {
            "client_id": config.REDDIT_CLIENT_ID,
            "response_type": "code",
            "state": state,
            "redirect_uri": redirect_uri("reddit"),
            "duration": "permanent",
            "scope": "identity read",
        }
        return f"https://www.reddit.com/api/v1/authorize?{urlencode(params)}"
    params = {
        "response_type": "code",
        "client_id": config.X_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri("x"),
        "scope": "tweet.read users.read offline.access",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"https://twitter.com/i/oauth2/authorize?{urlencode(params)}"


def _reddit_exchange(code: str) -> dict:
    response = httpx.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri("reddit"),
        },
        headers={"User-Agent": config.REDDIT_USER_AGENT},
        timeout=config.SOURCE_HTTP_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _reddit_refresh(refresh_token: str) -> dict:
    response = httpx.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET),
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        headers={"User-Agent": config.REDDIT_USER_AGENT},
        timeout=config.SOURCE_HTTP_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _reddit_label(access_token: str) -> str:
    try:
        response = httpx.get(
            "https://oauth.reddit.com/api/v1/me",
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": config.REDDIT_USER_AGENT,
            },
            timeout=config.SOURCE_HTTP_TIMEOUT,
        )
        if response.status_code >= 400:
            return ""
        return (response.json() or {}).get("name") or ""
    except httpx.HTTPError:
        return ""


def _x_headers() -> dict:
    if config.X_OAUTH_CLIENT_SECRET:
        basic = base64.b64encode(
            f"{config.X_OAUTH_CLIENT_ID}:{config.X_OAUTH_CLIENT_SECRET}".encode()
        ).decode()
        return {"Authorization": f"Basic {basic}"}
    return {}


def _x_exchange(code: str, verifier: str) -> dict:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri("x"),
        "code_verifier": verifier,
        "client_id": config.X_OAUTH_CLIENT_ID,
    }
    response = httpx.post(
        "https://api.x.com/2/oauth2/token",
        data=data,
        headers=_x_headers(),
        timeout=config.SOURCE_HTTP_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _x_refresh(refresh_token: str) -> dict:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": config.X_OAUTH_CLIENT_ID,
    }
    response = httpx.post(
        "https://api.x.com/2/oauth2/token",
        data=data,
        headers=_x_headers(),
        timeout=config.SOURCE_HTTP_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _x_label(access_token: str) -> str:
    try:
        response = httpx.get(
            "https://api.x.com/2/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=config.SOURCE_HTTP_TIMEOUT,
        )
        if response.status_code >= 400:
            return ""
        return ((response.json() or {}).get("data") or {}).get("username") or ""
    except httpx.HTTPError:
        return ""


def _html(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>{title}</title>
<style>
body {{ font-family: Inter, system-ui, sans-serif; background:#000; color:#fff;
  max-width: 480px; margin: 64px auto; padding: 0 20px; }}
a {{ color: #95ABE6; }}
</style></head><body>{body}</body></html>""",
        status_code=status,
    )


async def connect_start(request: Request) -> Response:
    provider = (request.path_params.get("provider") or "").lower()
    ticket = request.query_params.get("ticket") or ""
    if provider not in CONNECTABLE:
        reason = UNSUPPORTED.get(provider, "Unknown provider")
        return _html("Cannot connect", f"<h1>Cannot connect {provider}</h1><p>{reason}</p>", 400)
    claimed = store.consume_oauth_ticket(auth.hash_key(ticket)) if ticket else None
    if claimed is None or claimed["provider"] != provider:
        return _html("Link expired", "<h1>This login link is invalid or has expired.</h1>", 400)
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    store.create_oauth_state(
        auth.hash_key(state),
        claimed["user_id"],
        provider,
        encrypt_secret(verifier),
        time.time() + STATE_TTL_SECONDS,
    )
    return RedirectResponse(_authorize_url(provider, state, challenge), status_code=302)


async def connect_callback(request: Request) -> Response:
    provider = (request.path_params.get("provider") or "").lower()
    error = request.query_params.get("error")
    if error:
        return _html("Login cancelled", f"<h1>Login cancelled</h1><p>{error}</p>", 400)
    state = request.query_params.get("state") or ""
    code = request.query_params.get("code") or ""
    saved = store.take_oauth_state(auth.hash_key(state)) if state else None
    if saved is None or saved["provider"] != provider or not code:
        return _html("Login failed", "<h1>This login session is invalid or expired.</h1>", 400)
    try:
        verifier = decrypt_secret(saved["code_verifier_enc"]) if saved["code_verifier_enc"] else ""
        if provider == "reddit":
            tokens = _reddit_exchange(code)
            label = _reddit_label(tokens.get("access_token", ""))
        else:
            tokens = _x_exchange(code, verifier)
            label = _x_label(tokens.get("access_token", ""))
        _store_tokens(saved["user_id"], provider, tokens, label)
    except Exception:
        logger.exception("OAuth callback failed for %s", provider)
        return _html("Login failed", "<h1>Could not complete login. Try a new link from Claude.</h1>", 502)
    return _html(
        "Connected",
        f"<h1>Connected {provider}</h1><p>You can close this tab and continue in Claude.</p>"
        + (f"<p>Signed in as <strong>{label}</strong>.</p>" if label else ""),
    )


def register(mcp_server) -> None:
    global _registered
    if _registered:
        return
    mcp_server.custom_route("/connect/{provider}", methods=["GET"])(connect_start)
    mcp_server.custom_route("/connect/{provider}/callback", methods=["GET"])(connect_callback)
    _registered = True
