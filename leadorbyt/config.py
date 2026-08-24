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

# --- HTTP transport (multi-tenant server) ---
HOST = os.environ.get("LEADORBYT_HOST", "0.0.0.0")
PORT = int(os.environ.get("LEADORBYT_PORT", "8000"))

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
