"""Runtime configuration for leadorbyt spiders and fetchers.

All values can be overridden with environment variables of the same name.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Loads a `.env` file from the current working directory (if present) into
# os.environ before any of the lookups below run. Never overrides a variable
# already set in the real environment (e.g. by systemd/Docker), so deployed
# setups that already export these keys are unaffected.
load_dotenv()

# --- Networking / politeness ---
USER_AGENT = os.environ.get(
    "LEADORBYT_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)
DOWNLOAD_DELAY = float(os.environ.get("LEADORBYT_DOWNLOAD_DELAY", "1.0"))
MAX_CONCURRENCY = int(os.environ.get("LEADORBYT_MAX_CONCURRENCY", "8"))
ROBOTS_TXT_OBEY = os.environ.get("LEADORBYT_ROBOTS_TXT_OBEY", "true").lower() != "false"
REQUEST_TIMEOUT = float(os.environ.get("LEADORBYT_REQUEST_TIMEOUT", "20"))

# --- AutoThrottle ---
AUTOTHROTTLE_ENABLED = os.environ.get("LEADORBYT_AUTOTHROTTLE_ENABLED", "true").lower() != "false"
AUTOTHROTTLE_START_DELAY = float(os.environ.get("LEADORBYT_AUTOTHROTTLE_START_DELAY", "3.0"))
AUTOTHROTTLE_MAX_DELAY = float(os.environ.get("LEADORBYT_AUTOTHROTTLE_MAX_DELAY", "30.0"))

# --- Scale: browser pooling + job queue ---
# Number of long-lived stealth-browser sessions kept warm and reused across
# searches/enrichment escalations, instead of launching a fresh browser per call.
DISCOVERY_POOL_SIZE = int(os.environ.get("LEADORBYT_DISCOVERY_POOL_SIZE", "4"))
# Number of search-job worker coroutines pulling from the queue; defaults to
# the pool size since each in-flight search holds one pooled browser session.
SEARCH_WORKERS = int(os.environ.get("LEADORBYT_SEARCH_WORKERS", str(DISCOVERY_POOL_SIZE)))

# --- Persistent cache / dedup (SQLite, zero-cost, survives restarts) ---
DB_PATH = Path(os.environ.get("LEADORBYT_DB_PATH", str(Path.cwd() / "leadorbyt.db")))
CACHE_TTL_DAYS = float(os.environ.get("LEADORBYT_CACHE_TTL_DAYS", "7"))

# --- Output ---
OUTPUT_DIR = Path(os.environ.get("LEADORBYT_OUTPUT_DIR", str(Path.cwd() / "leads_output")))
# Result CSVs under OUTPUT_DIR/<user_id>/ older than this are deleted by
# `python -m leadorbyt.cleanup` (run on a schedule -- see docs/cleanup.md).
# Not enforced by the server process itself; an external cron owns timing.
RESULT_RETENTION_DAYS = float(os.environ.get("LEADORBYT_RESULT_RETENTION_DAYS", "7"))

# --- Pre-enrichment qualification gate (see qualify.py) ---
# Almost every paid source in sources/registry.py is keyed on domain (Apollo,
# Hunter, Clearbit, ...), so a business with no website wastes a paid call on
# every one of them. On by default; set to "false" to fan out regardless.
REQUIRE_WEBSITE_FOR_EXTRAS = os.environ.get("LEADORBYT_REQUIRE_WEBSITE_FOR_EXTRAS", "true").lower() != "false"
# Comma-separated, case-insensitive substrings matched against the discovered
# Google Maps `category` field; a match skips the paid fan-out entirely (e.g.
# "cemetery,parking lot" for a niche where those categories never convert).
# Blank (default) filters nothing.
EXCLUDE_CATEGORIES = {
    c.strip().lower()
    for c in os.environ.get("LEADORBYT_EXCLUDE_CATEGORIES", "").split(",")
    if c.strip()
}

# --- ML lead qualification against a caller-supplied ICP (see qualify_ml.py) ---
# No API key, ever -- leadorbyt is an MCP server, so the calling agent (which
# already has its own reasoning) supplies verdicts directly via
# submit_lead_verdicts rather than leadorbyt spending an AI API key of its
# own. These only tune the fully-offline confidence gate that decides
# whether enough agent-supplied verdicts exist to trust a decision without
# asking the agent about a given lead again.
# How many agent-supplied verdicts must exist for a given (user, ICP) pair
# before the confidence gate is trusted to decide at all; below this, every
# lead comes back agent_pending (there's nothing yet to learn from).
QUALIFY_MIN_LABELS = int(os.environ.get("LEADORBYT_QUALIFY_MIN_LABELS", "8"))
# Posterior mean must clear this threshold (or 1 - threshold, for a
# confident reject) for the gate to decide a lead on its own.
QUALIFY_GATE_CONFIDENCE = float(os.environ.get("LEADORBYT_QUALIFY_GATE_CONFIDENCE", "0.85"))
# Posterior std must be at or below this for the gate to trust its own mean
# enough to decide -- a wide/uncertain posterior always stays agent_pending.
QUALIFY_GATE_MAX_STD = float(os.environ.get("LEADORBYT_QUALIFY_GATE_MAX_STD", "0.15"))

# --- Adaptive search expansion for find_people_leads (see query_expansion.py) ---
# Only active when a caller sets goal_new_leads; each round adds up to this
# many Thompson-sampled title tokens (learned from prior qualified/rejected
# leads for the same user+ICP) to the search, capped at this many rounds.
QUERY_EXPANSION_MAX_ROUNDS = int(os.environ.get("LEADORBYT_QUERY_EXPANSION_MAX_ROUNDS", "2"))
QUERY_EXPANSION_TOKENS_PER_ROUND = int(os.environ.get("LEADORBYT_QUERY_EXPANSION_TOKENS_PER_ROUND", "3"))

# --- Web/social intent search (see web_signals.py / web_jobs.py) ---
# find_web_signals searches with site: filters for LinkedIn, Reddit, X, and
# Facebook, separate from find_leads_maps (Google Maps). Google's /search is
# not used: that path is disallowed by robots.txt, which this project
# already obeys.
#
# Serper (google.serper.dev) is used when SERPER_API_KEY is set (documented
# REST JSON endpoint over Google results, no bot-detection risk). Without a
# key, this falls back to scraping DuckDuckGo's public HTML SERP (scrapling
# Fetcher, stealth if blocked) -- unauthenticated and free, but DDG
# increasingly serves an "anomaly" bot-check page instead of results to
# datacenter IPs, which the fast path can't always distinguish from a real
# empty result.
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
WEB_SIGNALS_ENABLED = os.environ.get("LEADORBYT_WEB_SIGNALS_ENABLED", "true").lower() != "false"
WEB_SIGNAL_SITES = {
    s.strip().lower()
    for s in os.environ.get(
        "LEADORBYT_WEB_SIGNAL_SITES", "linkedin,reddit,x,facebook"
    ).split(",")
    if s.strip()
}

# --- Person-lead discovery spend guardrail (see people_jobs.py) ---
# A hard server-side ceiling on find_people_leads'/submit_people_search's
# max_paid_lookups, applied regardless of what a caller requests. Apollo's
# email reveal only bills on a verified hit (a miss costs nothing), so this
# isn't about avoiding paying for misses -- it bounds the worst case (every
# lookup hits) and the total call volume/latency an agent can trigger in
# one request, defense-in-depth on top of the per-call cap the tool itself accepts.
MAX_PAID_LOOKUPS_CEILING = int(os.environ.get("LEADORBYT_MAX_PAID_LOOKUPS_CEILING", "50"))

# --- Reddit signal search (see sources/reddit.py) ---
# COMPLIANCE NOTE: Reddit's API terms name "lead generation" as commercial
# use requiring Reddit's paid/contracted API access -- this free-tier
# integration is used outside that licensed scope; a deliberate, disclosed
# choice, not an oversight. See sources/reddit.py's module docstring.
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
# Reddit rejects generic/default user agents -- set something identifying,
# e.g. "leadorbyt/0.1 by u/yourusername".
REDDIT_USER_AGENT = os.environ.get("REDDIT_USER_AGENT", "")
# Kept below Reddit's ~100 req/min free-tier ceiling to leave headroom for
# clock/measurement drift, not because leadorbyt has its own quota.
REDDIT_MAX_REQUESTS_PER_MINUTE = int(os.environ.get("LEADORBYT_REDDIT_MAX_RPM", "60"))

# --- HTTP transport (multi-tenant server) ---
HOST = os.environ.get("LEADORBYT_HOST", "0.0.0.0")
PORT = int(os.environ.get("LEADORBYT_PORT", "8000"))
# Public origin shown to users on the signup page (no trailing slash).
# Example: https://leads.example.com
PUBLIC_URL = os.environ.get("LEADORBYT_PUBLIC_URL", f"http://127.0.0.1:{PORT}").rstrip("/")
# Self-serve key minting from the hosted UI. Operators can still use leadorbyt-admin.
SIGNUP_ENABLED = os.environ.get("LEADORBYT_SIGNUP_ENABLED", "true").lower() != "false"
SIGNUP_PER_HOUR = int(os.environ.get("LEADORBYT_SIGNUP_PER_HOUR", "8"))
# Transactional email (Resend) for the signup verification link. Same names as
# Mail Orbyt so a shared Resend project can feed both products. Leave the API
# key blank to log the message instead of sending it (local dev).
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM_ADDRESS = os.environ.get("EMAIL_FROM_ADDRESS", "Lead Orbyt <onboarding@resend.dev>")
# Fernet-compatible key, or any long secret (SHA-256 derived). Required to
# store Reddit/X OAuth tokens. Generate with:
# python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
TOKEN_ENCRYPTION_KEY = os.environ.get("LEADORBYT_TOKEN_ENCRYPTION_KEY", "")
# X OAuth 2.0 user login (official API). Separate from X_BEARER_TOKEN app lookup.
X_OAUTH_CLIENT_ID = os.environ.get("X_OAUTH_CLIENT_ID", "")
X_OAUTH_CLIENT_SECRET = os.environ.get("X_OAUTH_CLIENT_SECRET", "")

# --- Logging ---
LOG_LEVEL = os.environ.get("LEADORBYT_LOG_LEVEL", "INFO")

# --- Extra data-source API keys (see leadorbyt/sources/) -------------------
# Every source in leadorbyt/sources/ reads its key from here and disables
# itself (returns None, logs once) when the key is blank -- nothing is ever
# called without a configured credential. Copy `.env.example` to `.env` and
# fill in whichever keys you have; unset ones are simply skipped.
SOURCE_HTTP_TIMEOUT = float(os.environ.get("LEADORBYT_SOURCE_HTTP_TIMEOUT", "15"))

# Places / local business data
GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
YELP_API_KEY = os.environ.get("YELP_API_KEY", "")
FOURSQUARE_API_KEY = os.environ.get("FOURSQUARE_API_KEY", "")
# OpenStreetMap/Overpass is free/keyless; only the endpoint is configurable.
OVERPASS_API_URL = os.environ.get("OVERPASS_API_URL", "https://overpass-api.de/api/interpreter")

# Corporate / firmographic data
# SEC EDGAR is free/keyless but requires a contact-identifying User-Agent per
# https://www.sec.gov/os/webmaster-faq#developers -- set this to "Your Name your@email.com".
SEC_EDGAR_USER_AGENT = os.environ.get("SEC_EDGAR_USER_AGENT", "")
OPENCORPORATES_API_KEY = os.environ.get("OPENCORPORATES_API_KEY", "")  # optional; works keyless at low volume
CRUNCHBASE_API_KEY = os.environ.get("CRUNCHBASE_API_KEY", "")
CLEARBIT_API_KEY = os.environ.get("CLEARBIT_API_KEY", "")

# Tech stack detection
BUILTWITH_API_KEY = os.environ.get("BUILTWITH_API_KEY", "")
WAPPALYZER_API_KEY = os.environ.get("WAPPALYZER_API_KEY", "")
# Shopify/WooCommerce detection is a free HTML-signature check (see
# sources/storefront.py) -- no API key needed, always runs.

# Job boards
# Greenhouse/Lever/Ashby are public per-company JSON boards keyed by the
# company's own board token/slug, not a global API key -- no key needed.
THEMUSE_API_KEY = os.environ.get("THEMUSE_API_KEY", "")  # optional; works keyless at low volume

# News / social
NEWSAPI_ORG_API_KEY = os.environ.get("NEWSAPI_ORG_API_KEY", "")
X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")

# Contact / people enrichment
HUNTER_API_KEY = os.environ.get("HUNTER_API_KEY", "")
APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")
SNOV_CLIENT_ID = os.environ.get("SNOV_CLIENT_ID", "")
SNOV_CLIENT_SECRET = os.environ.get("SNOV_CLIENT_SECRET", "")
ROCKETREACH_API_KEY = os.environ.get("ROCKETREACH_API_KEY", "")
PEOPLEDATALABS_API_KEY = os.environ.get("PEOPLEDATALABS_API_KEY", "")
LUSHA_API_KEY = os.environ.get("LUSHA_API_KEY", "")
COGNISM_API_KEY = os.environ.get("COGNISM_API_KEY", "")
# ZoomInfo uses a JWT-signed token exchange, not a plain key -- see
# sources/zoominfo.py for the signing flow.
ZOOMINFO_USERNAME = os.environ.get("ZOOMINFO_USERNAME", "")
ZOOMINFO_CLIENT_ID = os.environ.get("ZOOMINFO_CLIENT_ID", "")
ZOOMINFO_PRIVATE_KEY = os.environ.get("ZOOMINFO_PRIVATE_KEY", "")  # PEM contents, or path to a PEM file
# BetterContact: the priority backend for find_people_leads (see
# sources/bettercontact.py, sources/person_search.py) -- its free search
# always includes full name, LinkedIn URL, and company domain, unlike
# Apollo's, which withholds all three behind the paid match/enrich call.
# When both this and APOLLO_API_KEY are set, BetterContact is used.
BETTERCONTACT_API_KEY = os.environ.get("BETTERCONTACT_API_KEY", "")
BETTERCONTACT_POLL_INTERVAL_SECONDS = float(os.environ.get("LEADORBYT_BETTERCONTACT_POLL_INTERVAL", "3.0"))
BETTERCONTACT_MAX_POLL_ATTEMPTS = int(os.environ.get("LEADORBYT_BETTERCONTACT_MAX_POLL_ATTEMPTS", "40"))
FINDYMAIL_API_KEY = os.environ.get("FINDYMAIL_API_KEY", "")
LEADMAGIC_API_KEY = os.environ.get("LEADMAGIC_API_KEY", "")
WIZA_API_KEY = os.environ.get("WIZA_API_KEY", "")
PROSPEO_API_KEY = os.environ.get("PROSPEO_API_KEY", "")

# No public self-serve API exists for these two (Indeed's Publisher API is
# invite-only/discontinued for most applicants; LinkedIn has no public
# company/people search API without a restricted partner agreement), and
# scraping either would violate their Terms of Service. The keys are wired
# through so they're ready if you obtain partner/publisher access, but
# sources/indeed.py and sources/linkedin.py always report "not integrated".
INDEED_PUBLISHER_ID = os.environ.get("INDEED_PUBLISHER_ID", "")
LINKEDIN_API_KEY = os.environ.get("LINKEDIN_API_KEY", "")
