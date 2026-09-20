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
# Light to deep, so the blossom reads as shades of one green rather than a flat fill.
_PETAL_GREENS = ("#6ec48c", "#49a971", "#2f8f5b", "#227049", "#17512f")

def _mark(css_class: str = "") -> str:
    """Brand mark: a five-petal blossom, one green shade per petal.

    One petal path rotated in 72-degree steps; the washed fill keeps the
    overlap at the centre readable at the 28px topbar size. Colours are
    literal rather than `currentColor` so the petals keep their own shades
    wherever the mark is placed.
    """
    class_attr = f' class="{css_class}"' if css_class else ""
    petals = "\n".join(
        f'      <path d="{_PETAL}" transform="rotate({angle} 16 16)" fill="{shade}"/>'
        for angle, shade in zip((0, 72, 144, 216, 288), _PETAL_GREENS)
    )
    return f"""<svg{class_attr} viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <g fill-opacity="0.92">
{petals}
      </g>
      <circle cx="16" cy="16" r="1.9" fill="#17512f"/>
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
        <section class="card result" id="setup">
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
        <section class="card result" id="setup">
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
        <section class="card result" id="setup">
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

    focused = bool(pending_email or verify_token or result)
    disabled = "" if config.SIGNUP_ENABLED else "disabled"
    disabled_note = (
        ""
        if config.SIGNUP_ENABLED
        else "<p class='error' role='alert'>Self-serve signup is turned off on this host.</p>"
    )
    signup_form = (
        ""
        if focused
        else f"""
    <section class="setup-section" id="setup">
      <div class="setup-copy">
        <p class="section-kicker">Get connected</p>
        <h2 class="display-title">Give Claude a research desk.</h2>
        <p>One email verification gets you the MCP URL, an API key, and a ready-to-paste Claude Desktop config.</p>
        <ul class="check-list">
          <li>No credit card required for setup</li>
          <li>Your API key is shown only once</li>
          <li>Enrichment stays opt-in</li>
        </ul>
      </div>
      <div class="signup-panel">
        <div class="panel-top">
          <span class="status-dot"></span>
          <span>MCP access</span>
        </div>
        <div class="panel-body">
          {disabled_note}
          <h3>Start with your work email</h3>
          <p class="note">We’ll send a secure verification link. It expires in 24 hours.</p>
          <form method="post" action="/signup" class="stack">
            <div>
              <label for="email">Work email</label>
              <input id="email" name="email" type="email" required placeholder="you@company.com" autocomplete="email" {disabled}>
            </div>
            {error_html}
            <button type="submit" {disabled}>Get MCP setup <span aria-hidden="true">→</span></button>
          </form>
          <p class="form-fine">By continuing, you’ll receive one setup email. Lead Orbyt does not send prospect outreach.</p>
        </div>
      </div>
    </section>
    """
    )
    marketing = (
        ""
        if focused
        else """
    <section class="proof-strip" aria-label="Product capabilities">
      <p>One MCP connection</p>
      <span></span>
      <p>Three independent lead sources</p>
      <span></span>
      <p>CSV output</p>
      <span></span>
      <p>Human-approved enrichment</p>
    </section>

    <section class="sources-section" id="sources">
      <div class="section-heading">
        <div>
          <p class="section-kicker">Choose the source</p>
          <h2 class="display-title">Different questions need<br>different search paths.</h2>
        </div>
        <p>Lead Orbyt keeps Maps, web intent, and people search separate. Claude chooses the right tool from your request, or runs them one after another when you want both.</p>
      </div>
      <div class="source-grid">
        <article class="source-card source-maps">
          <div class="source-icon" aria-hidden="true">⌖</div>
          <div class="source-number">01</div>
          <p class="tool-name">find_leads_maps</p>
          <h3>Build a local market list.</h3>
          <p>Research businesses by niche and location using Google Maps. Capture company details, contact fields, Maps URLs, and coordinates without visiting their websites.</p>
          <div class="source-example">“Find 30 commercial HVAC contractors in Dallas.”</div>
        </article>
        <article class="source-card source-web">
          <div class="source-icon" aria-hidden="true">↗</div>
          <div class="source-number">02</div>
          <p class="tool-name">find_web_signals</p>
          <h3>Find demand already in motion.</h3>
          <p>Surface public posts on LinkedIn, Reddit, X, and Facebook where people are actively asking, comparing, hiring, or looking for recommendations.</p>
          <div class="source-example">“Find people looking for a keynote speaker.”</div>
        </article>
        <article class="source-card source-people">
          <div class="source-icon" aria-hidden="true">◎</div>
          <div class="source-number">03</div>
          <p class="tool-name">find_people_leads</p>
          <h3>Reach the right role.</h3>
          <p>Search for named decision-makers by title, company, location, industry, seniority, headcount, or technology using BetterContact or Apollo.</p>
          <div class="source-example">“Find IT directors at US logistics companies.”</div>
        </article>
      </div>
    </section>

    <section class="workflow-section" id="how">
      <div class="workflow-copy">
        <p class="section-kicker">How it works</p>
        <h2 class="display-title">Ask naturally.<br>Stay in control.</h2>
        <p>Lead Orbyt gives your agent purpose-built tools and explicit boundaries. It researches first, shows its work, and waits before spending enrichment credits.</p>
        <div class="guardrail">
          <span class="guardrail-icon" aria-hidden="true">✓</span>
          <div>
            <strong>Approval is part of the workflow</strong>
            <p>Website enrichment and paid email reveals do not happen from a bare “find leads” request.</p>
          </div>
        </div>
      </div>
      <div class="steps-panel">
        <div class="workflow-step">
          <span>1</span>
          <div><strong>Describe the lead</strong><p>Use the same language you would use with a researcher.</p></div>
        </div>
        <div class="workflow-line"></div>
        <div class="workflow-step">
          <span>2</span>
          <div><strong>Claude selects a tool</strong><p>Maps, web signals, or people — one source at a time.</p></div>
        </div>
        <div class="workflow-line"></div>
        <div class="workflow-step">
          <span>3</span>
          <div><strong>Review the CSV</strong><p>See source data and qualification before enrichment.</p></div>
        </div>
        <div class="workflow-line"></div>
        <div class="workflow-step">
          <span>4</span>
          <div><strong>Approve the next step</strong><p>Only enrich or reveal emails when you choose to.</p></div>
        </div>
      </div>
    </section>

    <section class="safety-section">
      <div>
        <p class="section-kicker">Built for responsible research</p>
        <h2 class="display-title">A lead finder,<br>not an outreach machine.</h2>
      </div>
      <div class="safety-list">
        <div><strong>No outbound campaigns</strong><p>Lead Orbyt researches and exports. It never emails prospects.</p></div>
        <div><strong>No social passwords</strong><p>Reddit and X use official OAuth. LinkedIn and Facebook stay public-search only.</p></div>
        <div><strong>No surprise credit use</strong><p>People email reveal defaults to zero. Paid enrichment needs a clear request.</p></div>
      </div>
    </section>
    """
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="description" content="Lead Orbyt finds local businesses, web intent posts, and named people for Claude via MCP. You get CSVs. It does not send outreach.">
  <title>Lead Orbyt — lead discovery for Claude</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500;1,9..144,400&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {{
      --primary: #183f2d;
      --on-primary: #fffdf6;
      --secondary: #c75b36;
      --bg: #f4f1e8;
      --on-bg: #18201b;
      --surface: #fffdf7;
      --on-surface: #243028;
      --on-variant: #647068;
      --outline: #d9d8ce;
      --error: #b63d2f;
      --ready-text: #2a7452;
      --mint: #dcecdf;
      --peach: #f4dfd3;
      --blue: #dfe8ef;
      --radius: 18px;
      --sans: Inter, ui-sans-serif, system-ui, sans-serif;
      --serif: Fraunces, "Iowan Old Style", Georgia, serif;
      --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: var(--sans);
      background: var(--bg);
      color: var(--on-bg);
      -webkit-font-smoothing: antialiased;
    }}
    a {{ color: var(--secondary); }}
    .skip {{
      position: absolute;
      left: -999px;
      top: 8px;
    }}
    .skip:focus {{ left: 12px; z-index: 20; background: var(--bg); padding: 8px 12px; }}
    .topbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 15px max(24px, calc((100vw - 1180px) / 2));
      border-bottom: 1px solid var(--outline);
      background: color-mix(in srgb, var(--bg) 90%, transparent);
      position: sticky;
      top: 0;
      z-index: 10;
      backdrop-filter: blur(10px);
    }}
    .brand-lockup {{
      display: flex;
      align-items: center;
      gap: 10px;
      color: var(--primary);
      text-decoration: none;
    }}
    .brand {{
      font-family: var(--serif);
      font-size: 20px;
      font-weight: 500;
      letter-spacing: -0.02em;
      color: var(--on-bg);
    }}
    .mark {{ width: 28px; height: 28px; color: var(--primary); }}
    .nav {{
      display: flex;
      align-items: center;
      gap: 18px;
      font-size: 13px;
      font-weight: 500;
    }}
    .nav a {{ color: var(--on-variant); text-decoration: none; }}
    .nav a:hover {{ color: var(--on-bg); }}
    .nav .cta {{
      display: inline-flex;
      align-items: center;
      background: var(--primary);
      color: var(--on-primary);
      border-radius: 999px;
      padding: 11px 18px;
      text-decoration: none;
    }}
    main {{
      max-width: {"560px" if focused else "1180px"};
      margin: 0 auto;
      padding: {"56px 20px 88px" if focused else "0 24px 0"};
    }}
    .hero {{
      display: grid;
      grid-template-columns: {"1fr" if focused else "minmax(0, 1fr) minmax(420px, .86fr)"};
      align-items: center;
      gap: 64px;
      text-align: {"center" if focused else "left"};
      padding: {"0" if focused else "88px 0 76px"};
      min-height: {"auto" if focused else "680px"};
    }}
    .icon-wrap {{
      width: 64px;
      height: 64px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      background: var(--mint);
      border: 1px solid color-mix(in srgb, var(--primary) 22%, transparent);
      margin: {"0 auto 20px" if focused else "0 0 24px"};
      color: var(--primary);
    }}
    .icon-wrap svg {{ width: 34px; height: 34px; }}
    h1 {{
      font-family: var(--serif);
      font-size: clamp(46px, 5.5vw, 74px);
      font-weight: 400;
      line-height: 1.02;
      letter-spacing: -0.045em;
      margin: 0 0 22px;
    }}
    h1 em {{ font-style: italic; font-weight: 400; }}
    .lede {{
      margin: 0;
      color: var(--on-variant);
      font-size: 17px;
      line-height: 28px;
      max-width: 35rem;
    }}
    .hero-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 30px;
    }}
    .hero-actions a {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      font: 600 13px/1 var(--sans);
      border-radius: 999px;
      padding: 14px 21px;
      text-decoration: none;
    }}
    .hero-actions .cta {{
      background: var(--primary);
      color: var(--on-primary);
    }}
    .hero-actions .ghost {{
      border: 1px solid var(--outline);
      color: var(--on-surface);
      background: transparent;
    }}
    .hero-actions a:hover, .nav .cta:hover {{ transform: translateY(-2px); }}
    .hero-note {{
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--on-variant);
      font-size: 12px;
      margin-top: 18px;
    }}
    .hero-note::before {{
      content: "";
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: #3d9a68;
      box-shadow: 0 0 0 4px rgba(61,154,104,.12);
    }}
    .hero-kicker, .section-kicker {{
      margin: 0 0 14px;
      color: var(--ready-text);
      font-size: 11px;
      font-weight: 600;
      letter-spacing: .12em;
      text-transform: uppercase;
    }}
    .product-window {{
      position: relative;
      transform: rotate(1deg);
    }}
    /* The dark panel is its own element so the decorative shape below can sit
       behind it: `transform` on the parent makes a stacking context, where a
       negative-z ::before still paints over the parent's own background. */
    .window-frame {{
      position: relative;
      background: #202923;
      color: #eef4ee;
      border: 1px solid #35443a;
      border-radius: 24px;
      padding: 14px;
      box-shadow: 0 36px 80px rgba(39,57,45,.22), 0 3px 10px rgba(39,57,45,.12);
    }}
    .product-window::before {{
      content: "";
      position: absolute;
      z-index: -1;
      width: 72%;
      height: 62%;
      right: -28px;
      top: -30px;
      border-radius: 32px;
      background: var(--peach);
      transform: rotate(5deg);
    }}
    .window-bar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 5px 7px 14px;
      color: #d4e0d7;
      font-size: 11px;
    }}
    .window-title {{ font-weight: 500; letter-spacing: .01em; }}
    .window-status {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 999px;
      background: rgba(124,200,151,.16);
      color: #9fdcb4;
      font-size: 10px;
      font-weight: 600;
    }}
    .window-status::before {{
      content: "";
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: #6fd396;
    }}
    .window-dots {{ display: flex; gap: 6px; }}
    .window-dots i {{ width: 7px; height: 7px; border-radius: 50%; background: #59665d; }}
    .chat-body {{
      background: #151c17;
      border: 1px solid #303b33;
      border-radius: 15px;
      padding: 20px;
    }}
    .chat-label {{ color: #7f9185; font: 500 10px/1 var(--mono); text-transform: uppercase; letter-spacing: .1em; }}
    .chat-prompt {{
      margin: 9px 0 22px;
      font-family: var(--serif);
      font-size: 19px;
      line-height: 1.35;
    }}
    .tool-call {{
      padding: 13px 14px;
      border-radius: 10px;
      background: #243027;
      border: 1px solid #3a4d3f;
      font: 11px/1.7 var(--mono);
      color: #b8d7c1;
    }}
    .tool-call strong {{ color: #87d3a0; font-weight: 500; }}
    .result-row {{
      display: grid;
      grid-template-columns: 34px 1fr auto;
      gap: 11px;
      align-items: center;
      padding: 14px 0;
      border-bottom: 1px solid #2c362f;
    }}
    .result-row:last-of-type {{ border-bottom: 0; }}
    .result-avatar {{
      width: 34px;
      height: 34px;
      border-radius: 9px;
      display: grid;
      place-items: center;
      background: #33483a;
      color: #9ed8ad;
      font: 600 11px var(--mono);
    }}
    .result-row strong {{ display: block; font-size: 12px; }}
    .result-row small {{ color: #839187; font-size: 10px; }}
    .result-row em {{ color: #7bc895; font: normal 10px var(--mono); }}
    .window-footer {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 14px;
      color: #8fa096;
      font-size: 10px;
    }}
    .window-footer span:last-child {{
      color: #17231b;
      background: #a8d8b5;
      padding: 7px 10px;
      border-radius: 999px;
      font-weight: 600;
    }}
    .proof-strip {{
      min-height: 72px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 18px 24px;
      margin: 0 -24px;
      border-top: 1px solid var(--outline);
      border-bottom: 1px solid var(--outline);
      color: var(--on-variant);
      font-size: 12px;
      font-weight: 500;
      letter-spacing: .02em;
    }}
    .proof-strip p {{ margin: 0; }}
    .proof-strip span {{ width: 4px; height: 4px; border-radius: 50%; background: #a7aaa4; }}
    .sources-section {{ padding: 110px 0 120px; }}
    .section-heading {{
      display: grid;
      grid-template-columns: 1.25fr .75fr;
      align-items: end;
      gap: 72px;
      margin-bottom: 38px;
    }}
    .display-title {{
      font: 400 clamp(34px, 4vw, 54px)/1.08 var(--serif);
      letter-spacing: -.04em;
      margin: 0;
    }}
    .section-heading > p, .workflow-copy > p, .setup-copy > p {{
      color: var(--on-variant);
      font-size: 15px;
      line-height: 25px;
      margin: 0;
    }}
    .source-grid {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 16px;
    }}
    .source-card {{
      position: relative;
      min-height: 430px;
      padding: 24px;
      border: 1px solid color-mix(in srgb, var(--on-bg) 10%, transparent);
      border-radius: 22px;
      overflow: hidden;
      transition: transform .2s ease, box-shadow .2s ease;
    }}
    .source-card:hover {{ transform: translateY(-5px); box-shadow: 0 24px 45px rgba(42,52,45,.1); }}
    .source-maps {{ background: var(--mint); }}
    .source-web {{ background: var(--peach); }}
    .source-people {{ background: var(--blue); }}
    .source-icon {{
      width: 46px;
      height: 46px;
      display: grid;
      place-items: center;
      border: 1px solid rgba(24,32,27,.18);
      border-radius: 50%;
      font: 400 24px var(--serif);
      background: rgba(255,255,255,.35);
    }}
    .source-number {{
      position: absolute;
      top: 27px;
      right: 25px;
      color: rgba(24,32,27,.45);
      font: 500 11px var(--mono);
    }}
    .tool-name {{
      margin: 76px 0 11px;
      color: rgba(24,32,27,.6);
      font: 500 10px var(--mono);
      letter-spacing: .04em;
    }}
    .source-card h3 {{
      font: 400 28px/1.13 var(--serif);
      letter-spacing: -.025em;
      margin: 0 0 14px;
    }}
    .source-card > p:not(.tool-name) {{
      color: #4e5a52;
      font-size: 13px;
      line-height: 21px;
      margin: 0;
    }}
    .source-example {{
      position: absolute;
      left: 24px;
      right: 24px;
      bottom: 24px;
      padding-top: 15px;
      border-top: 1px solid rgba(24,32,27,.13);
      color: #35453b;
      font: italic 400 12px/1.55 var(--serif);
    }}
    .workflow-section {{
      display: grid;
      grid-template-columns: .82fr 1.18fr;
      gap: 90px;
      align-items: center;
      margin: 0 -24px;
      padding: 110px max(24px, calc((100vw - 1180px) / 2));
      background: #1d2e24;
      color: #f6f3e9;
      width: 100vw;
      position: relative;
      left: 50%;
      transform: translateX(-50%);
    }}
    .workflow-copy .section-kicker {{ color: #8fcca4; }}
    .workflow-copy > p {{ color: #aebbb2; margin: 22px 0 0; }}
    .guardrail {{
      display: flex;
      gap: 13px;
      margin-top: 32px;
      padding: 17px;
      border: 1px solid #3c5043;
      border-radius: 14px;
      background: #24382b;
    }}
    .guardrail-icon {{
      flex: 0 0 28px;
      width: 28px;
      height: 28px;
      display: grid;
      place-items: center;
      border-radius: 50%;
      background: #a4d2b2;
      color: #183122;
      font-size: 13px;
    }}
    .guardrail strong {{ font-size: 13px; }}
    .guardrail p {{ margin: 5px 0 0; color: #aebbb2; font-size: 11px; line-height: 17px; }}
    .steps-panel {{
      padding: 28px;
      border: 1px solid #3b4b40;
      border-radius: 24px;
      background: #17251c;
      box-shadow: 0 30px 60px rgba(0,0,0,.18);
    }}
    .workflow-step {{
      display: grid;
      grid-template-columns: 38px 1fr;
      gap: 15px;
      align-items: start;
    }}
    .workflow-step > span {{
      width: 38px;
      height: 38px;
      display: grid;
      place-items: center;
      border: 1px solid #4c6253;
      border-radius: 50%;
      color: #a5d4b5;
      font: 500 11px var(--mono);
    }}
    .workflow-step strong {{ font-size: 13px; }}
    .workflow-step p {{ margin: 5px 0 0; color: #8f9e94; font-size: 11px; line-height: 17px; }}
    .workflow-line {{ width: 1px; height: 24px; margin: 5px 0 5px 19px; background: #405046; }}
    .safety-section {{
      display: grid;
      grid-template-columns: .8fr 1.2fr;
      gap: 80px;
      padding: 110px 0;
      align-items: start;
    }}
    .safety-list {{ border-top: 1px solid var(--outline); }}
    .safety-list > div {{
      display: grid;
      grid-template-columns: 190px 1fr;
      gap: 24px;
      padding: 22px 0;
      border-bottom: 1px solid var(--outline);
    }}
    .safety-list strong {{ font-size: 13px; }}
    .safety-list p {{ margin: 0; color: var(--on-variant); font-size: 12px; line-height: 19px; }}
    .setup-section {{
      display: grid;
      grid-template-columns: .85fr 1.15fr;
      gap: 80px;
      align-items: center;
      padding: 86px;
      margin-bottom: 0;
      background: #ead9cc;
      border-radius: 28px 28px 0 0;
    }}
    .setup-copy > p {{ margin-top: 18px; }}
    .check-list {{
      display: grid;
      gap: 10px;
      padding: 0;
      margin: 26px 0 0;
      list-style: none;
      font-size: 12px;
      color: #4f5a52;
    }}
    .check-list li::before {{ content: "✓"; color: var(--ready-text); font-weight: 700; margin-right: 9px; }}
    .signup-panel {{
      border-radius: 18px;
      overflow: hidden;
      background: var(--surface);
      border: 1px solid rgba(24,32,27,.14);
      box-shadow: 0 22px 50px rgba(66,51,41,.14);
    }}
    .panel-top {{
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 13px 18px;
      border-bottom: 1px solid var(--outline);
      color: var(--on-variant);
      font: 500 10px var(--mono);
      text-transform: uppercase;
      letter-spacing: .08em;
    }}
    .status-dot {{ width: 7px; height: 7px; border-radius: 50%; background: #4ca374; }}
    .panel-body {{ padding: 26px; }}
    .panel-body h3 {{ margin: 0 0 8px; font: 400 25px var(--serif); }}
    .panel-body button {{ width: 100%; display: flex; justify-content: space-between; }}
    .form-fine {{ margin: 13px 0 0; color: #899088; font-size: 9px; line-height: 15px; }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--outline);
      border-radius: var(--radius);
      overflow: hidden;
    }}
    .card.feature {{ padding: 18px; }}
    .card.feature h3 {{
      font-size: 16px;
      font-weight: 600;
      margin: 0 0 8px;
    }}
    .card.feature p:last-child {{
      margin: 0;
      color: var(--on-variant);
      font-size: 14px;
      line-height: 22px;
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
    .note {{ color: var(--on-variant); font-size: 13px; line-height: 20px; margin: 0 0 12px; }}
    .result {{ margin-top: 16px; }}
    body.focused #setup {{ max-width: 480px; }}
    .steps {{
      display: grid;
      gap: 8px;
      margin: 20px 0 0;
      padding: 0;
      list-style: none;
      max-width: 480px;
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
    footer.page-foot {{
      margin-top: 36px;
      color: var(--on-variant);
      font-size: 12px;
    }}
    body.landing .steps {{
      max-width: none;
      grid-template-columns: repeat(3, 1fr);
      gap: 22px;
      margin: 0;
      padding: 30px 86px 20px;
      background: #ead9cc;
      border-top: 1px solid rgba(24,32,27,.1);
    }}
    body.landing footer.page-foot {{
      margin: 0;
      padding: 12px 86px 42px;
      text-align: center;
      background: #ead9cc;
      border-radius: 0 0 28px 28px;
    }}
    body.focused .nav a:not(.cta) {{ display: none; }}
    body.focused .hero h1 {{ font-size: clamp(36px, 5vw, 52px); }}
    body.focused .lede {{ margin: 0 auto; }}
    body.focused .card.result {{ text-align: left; }}
    body.focused .steps {{ margin-left: auto; margin-right: auto; }}
    body.focused footer.page-foot {{ text-align: center; }}
    code {{ font-family: var(--mono); font-size: 0.92em; }}
    @media (max-width: 900px) {{
      .hero {{ grid-template-columns: 1fr; gap: 50px; padding: 68px 0; }}
      .hero-copy {{ max-width: 660px; }}
      .product-window {{ max-width: 620px; width: 100%; margin: 0 auto; }}
      .section-heading, .workflow-section, .safety-section, .setup-section {{
        grid-template-columns: 1fr;
        gap: 40px;
      }}
      .source-grid {{ grid-template-columns: 1fr; }}
      .source-card {{ min-height: 330px; }}
      .tool-name {{ margin-top: 42px; }}
      .workflow-section {{ padding-top: 80px; padding-bottom: 80px; }}
      .setup-section {{ padding: 56px; }}
      body.landing .steps {{ padding-left: 56px; padding-right: 56px; }}
    }}
    @media (max-width: 640px) {{
      .nav a:not(.cta) {{ display: none; }}
      .topbar {{ padding: 12px 18px; }}
      main {{ padding-left: 18px; padding-right: 18px; }}
      .hero {{ min-height: auto; padding: 52px 0 64px; }}
      h1 {{ font-size: 45px; }}
      .lede {{ font-size: 15px; line-height: 25px; }}
      .product-window {{ transform: none; }}
      .product-window::before {{ display: none; }}
      .window-frame {{ padding: 9px; }}
      .chat-body {{ padding: 15px; }}
      .proof-strip {{
        justify-content: flex-start;
        overflow-x: auto;
        white-space: nowrap;
        margin: 0 -18px;
        padding-left: 18px;
      }}
      .sources-section, .safety-section {{ padding: 78px 0; }}
      .section-heading {{ gap: 22px; }}
      .display-title {{ font-size: 38px; }}
      .workflow-section {{
        padding: 72px 18px;
        margin-left: -18px;
        margin-right: -18px;
      }}
      .safety-list > div {{ grid-template-columns: 1fr; gap: 7px; }}
      .setup-section {{ padding: 38px 20px; margin: 0 -18px; border-radius: 0; }}
      body.landing .steps {{
        grid-template-columns: 1fr;
        padding: 28px 20px;
        margin: 0 -18px;
      }}
      body.landing footer.page-foot {{
        margin: 0 -18px;
        padding: 10px 20px 38px;
        border-radius: 0;
      }}
      .copy-row {{ flex-direction: column; }}
    }}
  </style>
</head>
<body class="{"focused" if focused else "landing"}">
  <a class="skip" href="#setup">Skip to MCP setup</a>
  <header class="topbar">
    <a class="brand-lockup" href="/">
      {_mark("mark")}
      <span class="brand">Lead Orbyt</span>
    </a>
    <nav class="nav" aria-label="Page">
      <a href="#sources">Sources</a>
      <a href="#how">How it works</a>
      <a class="cta" href="#setup">Get MCP access</a>
    </nav>
  </header>
  <main>
    <div class="hero">
      <div class="hero-copy">
        {f'<div class="icon-wrap" aria-hidden="true">{_mark()}</div>' if focused else ""}
        <p class="hero-kicker">Agent-native lead research</p>
        <h1>Turn a conversation into a <em>lead list.</em></h1>
        <p class="lede">Connect Lead Orbyt to Claude and research local businesses, active buying signals, or named decision-makers — without leaving the conversation.</p>
        {"" if focused else '''
        <div class="hero-actions">
          <a class="cta" href="#setup">Get MCP access <span aria-hidden="true">→</span></a>
          <a class="ghost" href="#sources">Explore the sources</a>
        </div>
        <p class="hero-note">Research first. Enrichment only with your approval.</p>
        '''}
      </div>
      {"" if focused else '''
      <div class="product-window" aria-label="Lead Orbyt product preview">
        <div class="window-frame">
        <div class="window-bar">
          <div class="window-dots"><i></i><i></i><i></i></div>
          <span class="window-title">Claude + Lead Orbyt</span>
          <span class="window-status">Connected</span>
        </div>
        <div class="chat-body">
          <span class="chat-label">You</span>
          <p class="chat-prompt">Find mid-market manufacturers in Ohio that might need a shop-floor MES.</p>
          <div class="tool-call"><strong>↳ find_leads_maps</strong><br>niche: precision manufacturer<br>location: Cleveland, OH · max_results: 20</div>
          <div class="result-row">
            <span class="result-avatar">AP</span>
            <div><strong>Apex Precision</strong><small>CNC manufacturer · Cleveland, OH</small></div>
            <em>new</em>
          </div>
          <div class="result-row">
            <span class="result-avatar">NL</span>
            <div><strong>Northline Fabrication</strong><small>Industrial fabricator · Akron, OH</small></div>
            <em>new</em>
          </div>
          <div class="window-footer"><span>20 businesses researched</span><span>CSV ready</span></div>
        </div>
        </div>
      </div>
      '''}
    </div>
    {marketing}
    {signup_form}
    {pending_html}
    {verify_html}
    {result_html}
    <ol class="steps">
      <li><span class="num">1</span><span>Confirm the link we email you.</span></li>
      <li><span class="num">2</span><span>Copy the config into Claude Desktop MCP settings.</span></li>
      <li><span class="num">3</span><span>Restart Claude Desktop and ask: “Find 10 commercial HVAC contractors in Dallas, TX.”</span></li>
    </ol>
    <footer class="page-foot">The API key is shown once, after you confirm your email. MCP tools at /mcp still require the Bearer key.</footer>
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
