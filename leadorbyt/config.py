"""Runtime configuration for leadorbyt spiders and fetchers.

All values can be overridden with environment variables of the same name.
"""

import os
from pathlib import Path

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
