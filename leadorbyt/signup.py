"""Hosted signup page: email in, verification link, then MCP URL + API key.

Public by design. MCP tools stay behind Bearer auth. This module mints a
tenant key the same way `leadorbyt-admin create-user` does, but only after
the visitor confirms a Resend (or console-logged) verification link — the
Mail Orbyt pattern: GET renders a confirm button, POST consumes the token,
so email-client prefetch cannot burn it. Returning emails reuse the tenant
and mint a new key (the previous raw key cannot be retrieved).
"""

from __future__ import annotations

import html
import logging
import re
import secrets
import time
from dataclasses import dataclass, field

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from . import auth, config, email as transactional_email, store
from .errors import ErrorType, LeadOrbytError

logger = logging.getLogger("leadorbyt.signup")

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_registered = False


_PETAL = "M16 16C12.3 12.6 12.3 7 16 3.6 19.7 7 19.7 12.6 16 16Z"

def _mark(css_class: str = "") -> str:
    """Brand mark: a five-petal blossom.

    One petal path rotated in 72-degree steps; the washed fill keeps the
    overlap at the centre readable at the 28px topbar size.
    """
    class_attr = f' class="{css_class}"' if css_class else ""
    petals = "\n".join(
        f'      <path d="{_PETAL}" transform="rotate({angle} 16 16)"/>'
        for angle in (0, 72, 144, 216, 288)
    )
    return f"""<svg{class_attr} viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <g fill="currentColor" fill-opacity="0.22" stroke="currentColor"
         stroke-width="1.3" stroke-linejoin="round">
{petals}
      </g>
      <circle cx="16" cy="16" r="1.9" fill="currentColor"/>
    </svg>"""


@dataclass
class _RateWindow:
    hits: list[float] = field(default_factory=list)


_ip_hits: dict[str, _RateWindow] = {}
_email_hits: dict[str, _RateWindow] = {}


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_ok(bucket: dict[str, _RateWindow], key: str, limit: int) -> bool:
    now = time.time()
    window = bucket.setdefault(key, _RateWindow())
    window.hits = [ts for ts in window.hits if now - ts < 3600]
    if len(window.hits) >= limit:
        return False
    window.hits.append(now)
    return True


def normalize_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise LeadOrbytError(ErrorType.INVALID_INPUT, "enter a valid email address")
    return email


def mcp_url() -> str:
    return f"{config.PUBLIC_URL}/mcp"


def claude_desktop_config(api_key: str) -> str:
    return (
        "{\n"
        '  "mcpServers": {\n'
        '    "leadorbyt": {\n'
        '      "command": "npx",\n'
        '      "args": [\n'
        '        "-y",\n'
        '        "mcp-remote",\n'
        f'        "{mcp_url()}",\n'
        '        "--header",\n'
        f'        "Authorization: Bearer {api_key}"\n'
        "      ]\n"
        "    }\n"
        "  }\n"
        "}"
    )


def issue_access(email: str) -> dict:
    """Create or reuse a tenant for `email` and mint a new API key.

    Called only after a verification token is consumed. Operators using
    `leadorbyt-admin` still mint keys without this path.
    """
    if not config.SIGNUP_ENABLED:
        raise LeadOrbytError(ErrorType.INVALID_INPUT, "self-serve signup is disabled")
    normalized = normalize_email(email)
    try:
        user_id, created = store.get_or_create_user_by_email(normalized)
    except ValueError as exc:
        raise LeadOrbytError(ErrorType.INVALID_INPUT, str(exc)) from exc
    store.mark_email_verified(user_id)
    api_key = store.create_api_key(user_id)
    logger.info("Issued MCP key for %s (new_tenant=%s)", normalized, created)
    return {
        "email": normalized,
        "user_id": user_id,
        "created": created,
        "api_key": api_key,
        "mcp_url": mcp_url(),
        "claude_desktop_config": claude_desktop_config(api_key),
        "status": "verified",
    }


def request_access(email: str) -> dict:
    """Send a verification email. Does not mint a key until the link is confirmed."""
    if not config.SIGNUP_ENABLED:
        raise LeadOrbytError(ErrorType.INVALID_INPUT, "self-serve signup is disabled")
    normalized = normalize_email(email)
    existing = store.get_user_by_email(normalized)
    if existing is not None and existing["disabled_at"]:
        raise LeadOrbytError(ErrorType.INVALID_INPUT, "this account is disabled")
    raw_token = secrets.token_urlsafe(32)
    token_hash = auth.hash_key(raw_token)
    expires_at = time.time() + transactional_email.EMAIL_VERIFICATION_TTL_SECONDS
    store.create_email_verification(normalized, token_hash, expires_at)
    message = transactional_email.verification_email(
        to=normalized, app_url=config.PUBLIC_URL, raw_token=raw_token
    )
    try:
        transactional_email.get_email_sender().send(message)
    except transactional_email.ResendSendError as exc:
        store.delete_email_verification(token_hash)
        logger.warning("Verification email failed for %s: %s", normalized, exc)
        raise LeadOrbytError(
            ErrorType.TRANSPORT_ERROR,
            "could not send the verification email; try again shortly",
            retryable=True,
        ) from exc
    logger.info("Sent verification email to %s", normalized)
    return {"email": normalized, "status": "pending"}


def preview_verification(raw_token: str) -> str:
    """Return the email for a still-valid token without consuming it."""
    token = (raw_token or "").strip()
    if not token:
        raise LeadOrbytError(ErrorType.INVALID_INPUT, "This link is missing its verification token.")
    row = store.get_email_verification(auth.hash_key(token))
    if row is None or row["consumed_at"]:
        raise LeadOrbytError(
            ErrorType.INVALID_INPUT,
            "This verification link is invalid or has already been used",
        )
    if row["expires_at"] <= time.time():
        raise LeadOrbytError(ErrorType.INVALID_INPUT, "This verification link has expired")
    return row["email"]


def confirm_verification(raw_token: str) -> dict:
    """Consume the token (POST only) and mint the API key."""
    email = preview_verification(raw_token)
    store.consume_email_verification(auth.hash_key(raw_token.strip()))
    return issue_access(email)


def _page(
    result: dict | None = None,
    error: str = "",
    pending_email: str = "",
    verify_token: str = "",
    verify_email: str = "",
) -> str:
    error_html = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    pending_html = ""
    if pending_email:
        pending_html = f"""
        <section class="card result">
          <div class="card-head">
            <p class="eyebrow">Check your inbox</p>
            <h2>Verify your email</h2>
          </div>
          <div class="card-body">
            <p class="note">We sent a verification link to <strong>{html.escape(pending_email)}</strong>.
            Open it and click confirm to get your MCP URL and API key. The link expires in 24 hours.</p>
          </div>
        </section>
        """
    verify_html = ""
    if verify_token:
        verify_html = f"""
        <section class="card result">
          <div class="card-head">
            <p class="eyebrow">Confirm</p>
            <h2>Verify your email</h2>
          </div>
          <div class="card-body stack">
            <p class="note">Click below to confirm <strong>{html.escape(verify_email)}</strong> and receive your API key.
            Opening this page does not activate the link — email scanners cannot use it for you.</p>
            {error_html}
            <form method="post" action="/verify-email" class="stack">
              <input type="hidden" name="token" value="{html.escape(verify_token)}">
              <button type="submit">Confirm my email</button>
            </form>
          </div>
        </section>
        """
        error_html = ""
    result_html = ""
    if result:
        config_json = html.escape(result["claude_desktop_config"])
        returning = "" if result["created"] else (
            "<p class='note'>This email already has an account. A new key was created; "
            "older keys still work until an operator revokes them.</p>"
        )
        result_html = f"""
        <section class="card result">
          <div class="card-head">
            <p class="eyebrow">Ready</p>
            <h2>Your Claude connection</h2>
          </div>
          <div class="card-body">
            {returning}
            <label for="mcp-url">MCP URL</label>
            <div class="copy-row">
              <input id="mcp-url" readonly value="{html.escape(result["mcp_url"])}">
              <button type="button" class="ghost" data-copy="mcp-url">Copy</button>
            </div>
            <label for="api-key">API key — shown once</label>
            <div class="copy-row">
              <input id="api-key" readonly value="{html.escape(result["api_key"])}">
              <button type="button" class="ghost" data-copy="api-key">Copy</button>
            </div>
            <p class="note">Save the key now. It cannot be looked up later.</p>
            <label for="claude-config">Claude Desktop config</label>
            <p class="note">Paste into <code>claude_desktop_config.json</code>, then restart Claude Desktop.</p>
            <textarea id="claude-config" readonly rows="14">{config_json}</textarea>
            <button type="button" class="ghost wide" data-copy="claude-config">Copy config</button>
          </div>
        </section>
        """

    disabled = "" if config.SIGNUP_ENABLED else "disabled"
    disabled_note = (
        ""
        if config.SIGNUP_ENABLED
        else "<p class='error' role='alert'>Self-serve signup is turned off on this host.</p>"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>Lead Orbyt — connect Claude</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500;1,9..144,400&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {{
      --primary: #333333;
      --on-primary: #ffffff;
      --secondary: #1E4DF6;
      --bg: #ffffff;
      --on-bg: #111111;
      --surface: #F9FAFB;
      --on-surface: #333333;
      --on-variant: #646466;
      --outline: #e4e4e6;
      --error: #FF653F;
      --ready-bg: #ecfdf5;
      --ready-text: #047857;
      --radius: 12px;
      --sans: Inter, ui-sans-serif, system-ui, sans-serif;
      --serif: Fraunces, "Iowan Old Style", Georgia, serif;
      --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --primary: #DADDE6;
        --on-primary: #44464C;
        --secondary: #95ABE6;
        --bg: #000000;
        --on-bg: #ffffff;
        --surface: #0E0E0E;
        --on-surface: #E2E2E2;
        --on-variant: #e3e4e6;
        --outline: #444444;
        --ready-bg: #052e16;
        --ready-text: #6ee7b7;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: var(--sans);
      background: var(--bg);
      color: var(--on-bg);
    }}
    .topbar {{
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 16px 24px;
      border-bottom: 1px solid var(--outline);
      background: var(--surface);
    }}
    .brand {{
      font-family: var(--serif);
      font-size: 18px;
      font-weight: 500;
      letter-spacing: -0.02em;
    }}
    .mark {{
      width: 28px;
      height: 28px;
      color: var(--on-bg);
    }}
    main {{
      max-width: 440px;
      margin: 0 auto;
      padding: 48px 20px 80px;
    }}
    .hero {{
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
      margin-bottom: 28px;
    }}
    .icon-wrap {{
      width: 88px;
      height: 88px;
      border-radius: 44px;
      display: grid;
      place-items: center;
      background: var(--surface);
      border: 1px solid var(--outline);
      margin-bottom: 24px;
      color: var(--on-bg);
    }}
    .icon-wrap svg {{ width: 40px; height: 40px; }}
    h1 {{
      font-family: var(--serif);
      font-size: 32px;
      font-weight: 400;
      line-height: 1.15;
      letter-spacing: -0.03em;
      margin: 0 0 12px;
    }}
    h1 em {{ font-style: italic; font-weight: 400; }}
    .lede {{
      margin: 0;
      color: var(--on-variant);
      font-size: 15px;
      line-height: 24px;
      max-width: 34rem;
    }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--outline);
      border-radius: var(--radius);
      overflow: hidden;
    }}
    .card-head {{
      padding: 16px 18px 12px;
      border-bottom: 1px solid var(--outline);
      background: color-mix(in srgb, var(--primary) 4%, var(--surface));
    }}
    .card-body {{ padding: 18px; }}
    .eyebrow {{
      margin: 0 0 4px;
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--ready-text);
    }}
    h2 {{
      font-family: var(--sans);
      font-size: 16px;
      font-weight: 600;
      margin: 0;
      color: var(--on-surface);
    }}
    label {{
      display: block;
      font-size: 12px;
      font-weight: 500;
      color: var(--on-surface);
      margin: 0 0 6px;
    }}
    input, textarea {{
      width: 100%;
      font: 13px/20px var(--mono);
      border: 1px solid var(--outline);
      background: var(--bg);
      color: var(--on-bg);
      border-radius: 8px;
      padding: 10px 12px;
    }}
    input:focus, textarea:focus {{
      outline: 2px solid var(--secondary);
      outline-offset: 1px;
    }}
    textarea {{ resize: vertical; min-height: 180px; }}
    .stack {{ display: flex; flex-direction: column; gap: 12px; }}
    .copy-row {{ display: flex; gap: 8px; margin-bottom: 14px; }}
    .copy-row input {{ flex: 1; min-width: 0; }}
    button {{
      font: 600 13px/1 var(--sans);
      border: 0;
      border-radius: 20px;
      background: var(--primary);
      color: var(--on-primary);
      padding: 12px 16px;
      cursor: pointer;
    }}
    button:hover {{ filter: brightness(1.08); }}
    button.ghost {{
      background: transparent;
      color: var(--on-surface);
      border: 1px solid var(--outline);
      border-radius: 8px;
      padding: 10px 12px;
      white-space: nowrap;
    }}
    button.wide {{ width: 100%; margin-top: 8px; }}
    button:disabled {{ opacity: 0.45; cursor: not-allowed; }}
    .error {{ color: var(--error); font-size: 13px; margin: 0 0 8px; }}
    .note {{ color: var(--on-variant); font-size: 12px; line-height: 18px; margin: 0 0 12px; }}
    .result {{ margin-top: 16px; }}
    .steps {{
      display: grid;
      gap: 8px;
      margin: 20px 0 0;
      padding: 0;
      list-style: none;
    }}
    .steps li {{
      display: grid;
      grid-template-columns: 28px 1fr;
      gap: 10px;
      align-items: start;
      color: var(--on-variant);
      font-size: 13px;
      line-height: 20px;
    }}
    .num {{
      width: 28px;
      height: 28px;
      border-radius: 14px;
      display: grid;
      place-items: center;
      font-size: 12px;
      font-weight: 600;
      color: var(--on-surface);
      background: var(--surface);
      border: 1px solid var(--outline);
    }}
    footer {{
      margin-top: 28px;
      text-align: center;
      color: var(--on-variant);
      font-size: 12px;
    }}
    code {{ font-family: var(--mono); font-size: 0.92em; }}
  </style>
</head>
<body>
  <header class="topbar">
    {_mark("mark")}
    <span class="brand">Lead Orbyt</span>
  </header>
  <main>
    <div class="hero">
      <div class="icon-wrap" aria-hidden="true">
        {_mark()}
      </div>
      <h1>Connect Lead Orbyt to <em>Claude</em></h1>
      <p class="lede">Enter your email. We send a verification link; after you confirm, you get an MCP URL and API key. Discovery runs first; enrichment only happens if you ask for it.</p>
    </div>
    {disabled_note}
    {"" if (pending_email or verify_token or result) else '''
    <section class="card">
      <div class="card-body stack">
        <form method="post" action="/signup" class="stack">
          <div>
            <label for="email">Email</label>
            <input id="email" name="email" type="email" required placeholder="you@company.com" autocomplete="email" ''' + disabled + '''>
          </div>
          ''' + error_html + '''
          <button type="submit" ''' + disabled + '''>Send verification link</button>
        </form>
      </div>
    </section>
    '''}
    {pending_html}
    {verify_html}
    {result_html}
    <ol class="steps">
      <li><span class="num">1</span><span>Confirm the link we email you.</span></li>
      <li><span class="num">2</span><span>Copy the config into Claude Desktop MCP settings.</span></li>
      <li><span class="num">3</span><span>Restart Claude Desktop and ask: “Find 10 coffee shops in Austin, TX.”</span></li>
    </ol>
    <footer>The API key is shown once, after you confirm your email.</footer>
  </main>
  <script>
    document.querySelectorAll("[data-copy]").forEach((btn) => {{
      btn.addEventListener("click", async () => {{
        const el = document.getElementById(btn.dataset.copy);
        const text = el.value || el.textContent;
        await navigator.clipboard.writeText(text);
        const original = btn.textContent;
        btn.textContent = "Copied";
        setTimeout(() => {{ btn.textContent = original; }}, 1200);
      }});
    }});
  </script>
</body>
</html>"""


async def home(_request: Request) -> Response:
    return HTMLResponse(_page())


async def health(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def signup(request: Request) -> Response:
    if request.method != "POST":
        return HTMLResponse(_page())

    if not _rate_ok(_ip_hits, _client_ip(request), config.SIGNUP_PER_HOUR):
        return HTMLResponse(_page(error="Too many requests from this network. Try again later."), status_code=429)

    if request.headers.get("content-type", "").startswith("application/json"):
        payload = await request.json()
        email = str(payload.get("email") or "")
        wants_json = True
    else:
        form = await request.form()
        email = str(form.get("email") or "")
        wants_json = "application/json" in request.headers.get("accept", "")

    try:
        if not _rate_ok(_email_hits, email.strip().lower() or "missing", 4):
            raise LeadOrbytError(ErrorType.INVALID_INPUT, "too many keys requested for this email")
        pending = request_access(email)
    except LeadOrbytError as exc:
        status = 400 if exc.type != ErrorType.TRANSPORT_ERROR else 502
        if wants_json:
            return JSONResponse({"error": exc.message}, status_code=status)
        return HTMLResponse(_page(error=exc.message), status_code=status)

    if wants_json:
        return JSONResponse(pending)
    return HTMLResponse(_page(pending_email=pending["email"]))


async def verify_email(request: Request) -> Response:
    if request.method == "GET":
        token = request.query_params.get("token") or ""
        try:
            email = preview_verification(token)
        except LeadOrbytError as exc:
            return HTMLResponse(_page(error=exc.message), status_code=400)
        return HTMLResponse(_page(verify_token=token, verify_email=email))

    if request.headers.get("content-type", "").startswith("application/json"):
        payload = await request.json()
        token = str(payload.get("token") or "")
        wants_json = True
    else:
        form = await request.form()
        token = str(form.get("token") or "")
        wants_json = "application/json" in request.headers.get("accept", "")

    try:
        result = confirm_verification(token)
    except LeadOrbytError as exc:
        if wants_json:
            return JSONResponse({"error": exc.message}, status_code=400)
        try:
            email = preview_verification(token)
            return HTMLResponse(
                _page(error=exc.message, verify_token=token, verify_email=email),
                status_code=400,
            )
        except LeadOrbytError:
            return HTMLResponse(_page(error=exc.message), status_code=400)

    if wants_json:
        return JSONResponse(result)
    return HTMLResponse(_page(result=result))


def register(mcp_server) -> None:
    """Attach public routes to the MCP Starlette app. Safe to call once."""
    global _registered
    if _registered:
        return
    mcp_server.custom_route("/", methods=["GET"])(home)
    mcp_server.custom_route("/signup", methods=["GET", "POST"])(signup)
    mcp_server.custom_route("/verify-email", methods=["GET", "POST"])(verify_email)
    mcp_server.custom_route("/health", methods=["GET"])(health)
    _registered = True
