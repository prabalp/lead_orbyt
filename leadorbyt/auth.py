"""Per-request API-key auth for the streamable-http transport.

Deliberately bypasses the `mcp` SDK's built-in `auth`/`token_verifier`
wiring: that path requires full `AuthSettings` with an `issuer_url` and is
built for delegating to an external OAuth authorization server, which is
overkill for static API keys issued out-of-band by `admin.py`. Instead this
is a plain ASGI middleware wrapped around `server.streamable_http_app()`.

`current_user_id` is populated once per request by the middleware and read
by each tool in server.py; it is NOT relied on inside jobs.py's long-lived
worker tasks (those were created outside any request's context, so a job
threads its owner's user_id explicitly instead -- see jobs.py).
"""

import hashlib
import secrets
from contextvars import ContextVar

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import store

current_user_id: ContextVar[str | None] = ContextVar("current_user_id", default=None)

# Signup UI and health checks are public; MCP tools stay behind a Bearer key.
PUBLIC_PATHS = frozenset({"/", "/signup", "/verify-email", "/health"})


def _is_public_path(path: str) -> bool:
    normalized = "/" if path in ("", "/") else path.rstrip("/")
    if normalized in PUBLIC_PATHS:
        return True
    return normalized == "/connect" or normalized.startswith("/connect/")


def generate_key() -> str:
    return secrets.token_urlsafe(32)


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def require_user_id() -> str:
    """Fetch the authenticated user for the in-flight request.

    Raises if called outside a request that passed `ApiKeyAuthMiddleware`
    (e.g. accidentally left un-scoped) rather than silently falling back to
    some default tenant.
    """
    user_id = current_user_id.get()
    if user_id is None:
        raise RuntimeError("no authenticated user in context")
    return user_id


class ApiKeyAuthMiddleware:
    """Verifies `Authorization: Bearer <key>` on every request and sets
    `current_user_id` for the duration of it."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or "/"
        if _is_public_path(path):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        raw_key = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""

        user_id = store.get_user_for_key(hash_key(raw_key)) if raw_key else None
        if user_id is None:
            response = PlainTextResponse("Unauthorized", status_code=401)
            await response(scope, receive, send)
            return

        token = current_user_id.set(user_id)
        try:
            await self.app(scope, receive, send)
        finally:
            current_user_id.reset(token)
