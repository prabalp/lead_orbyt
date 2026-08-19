"""Persistent, zero-cost cache/dedup/backoff state, backed by stdlib sqlite3.

Replaces the plain in-memory dicts the server used to keep per-session caches
in: a sqlite file survives restarts and is shared across every concurrent
call in the process, which is what makes the persistent caching (and the
shared per-domain backoff bookkeeping) actually cut down on repeat scraping
at volume instead of just within one session.

All public functions are synchronous (sqlite3 has no async API) -- callers
run them via `asyncio.to_thread`, same pattern already used in server.py for
`enrich_website`.
"""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS enrichment_cache (
    domain TEXT PRIMARY KEY,
    data_json TEXT NOT NULL,
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS domain_backoff (
    domain TEXT PRIMARY KEY,
    next_allowed_at REAL NOT NULL,
    consecutive_blocks INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    created_at REAL NOT NULL,
    disabled_at REAL
);

CREATE TABLE IF NOT EXISTS api_keys (
    key_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL,
    revoked_at REAL
);

CREATE TABLE IF NOT EXISTS search_cache (
    user_id TEXT NOT NULL,
    niche TEXT NOT NULL,
    location TEXT NOT NULL,
    max_results INTEGER NOT NULL,
    result_path TEXT NOT NULL,
    finished_at REAL NOT NULL,
    PRIMARY KEY (user_id, niche, location, max_results)
);

CREATE TABLE IF NOT EXISTS search_jobs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    niche TEXT NOT NULL,
    location TEXT NOT NULL,
    max_results INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    result_path TEXT,
    error TEXT
);
"""

_connection: sqlite3.Connection | None = None


def _migrate_to_multitenant(conn: sqlite3.Connection) -> None:
    """Drop and recreate the two per-user tables if they predate multitenancy.

    Both tables are pure disposable cache/observability data (TTL'd,
    reproducible by re-running a search) -- safe to rebuild under the new
    (user_id, ...) schema instead of writing a data-preserving migration.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(search_cache)").fetchall()}
    if cols and "user_id" not in cols:
        conn.executescript("DROP TABLE IF EXISTS search_cache; DROP TABLE IF EXISTS search_jobs;")
        conn.commit()


def normalize_domain(url: str) -> str:
    """Normalize a URL down to a bare, lowercased, `www.`-stripped domain.

    The single shared dedup key for both the enrichment cache and the
    discovery/enrichment join in merge.py -- one definition, imported
    everywhere, so a URL always hashes to the same cache row.
    """
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"http://{url}")
    return parsed.netloc.lower().removeprefix("www.")


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open (or return the already-open) module-level connection."""
    global _connection
    if _connection is not None:
        return _connection

    path = Path(db_path) if db_path is not None else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _migrate_to_multitenant(conn)
    conn.executescript(_SCHEMA)
    conn.commit()
    _connection = conn
    return conn


@contextmanager
def _cursor():
    conn = connect()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    finally:
        cur.close()


# --- Enrichment cache -------------------------------------------------------

def get_enrichment(domain: str) -> dict | None:
    if not domain:
        return None
    ttl_seconds = config.CACHE_TTL_DAYS * 86400
    cutoff = time.time() - ttl_seconds
    with _cursor() as cur:
        row = cur.execute(
            "SELECT data_json, fetched_at FROM enrichment_cache WHERE domain = ?",
            (domain,),
        ).fetchone()
    if row is None:
        return None
    data_json, fetched_at = row
    if fetched_at < cutoff:
        return None
    return json.loads(data_json)


def put_enrichment(domain: str, data: dict) -> None:
    if not domain:
        return
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO enrichment_cache (domain, data_json, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(domain) DO UPDATE SET data_json = excluded.data_json, "
            "fetched_at = excluded.fetched_at",
            (domain, json.dumps(data), time.time()),
        )


# --- Search cache (per-user: never shared across tenants) -------------------

def get_search(user_id: str, niche: str, location: str, max_results: int) -> str | None:
    ttl_seconds = config.CACHE_TTL_DAYS * 86400
    cutoff = time.time() - ttl_seconds
    with _cursor() as cur:
        row = cur.execute(
            "SELECT result_path, finished_at FROM search_cache "
            "WHERE user_id = ? AND niche = ? AND location = ? AND max_results = ?",
            (user_id, niche, location, max_results),
        ).fetchone()
    if row is None:
        return None
    result_path, finished_at = row
    if finished_at < cutoff:
        return None
    return result_path


def put_search(user_id: str, niche: str, location: str, max_results: int, result_path: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO search_cache (user_id, niche, location, max_results, result_path, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, niche, location, max_results) DO UPDATE SET "
            "result_path = excluded.result_path, finished_at = excluded.finished_at",
            (user_id, niche, location, max_results, result_path, time.time()),
        )


# --- Domain backoff (shared politeness state) -------------------------------

def get_backoff_wait(domain: str) -> float:
    """Seconds the caller should wait before hitting `domain` again (0 if clear)."""
    if not domain:
        return 0.0
    with _cursor() as cur:
        row = cur.execute(
            "SELECT next_allowed_at FROM domain_backoff WHERE domain = ?",
            (domain,),
        ).fetchone()
    if row is None:
        return 0.0
    return max(0.0, row[0] - time.time())


def bump_block_count(domain: str) -> int:
    """Increment `domain`'s consecutive-block count and return the new value."""
    with _cursor() as cur:
        row = cur.execute(
            "SELECT consecutive_blocks FROM domain_backoff WHERE domain = ?",
            (domain,),
        ).fetchone()
        consecutive = (row[0] if row else 0) + 1
        cur.execute(
            "INSERT INTO domain_backoff (domain, next_allowed_at, consecutive_blocks) "
            "VALUES (?, 0, ?) "
            "ON CONFLICT(domain) DO UPDATE SET consecutive_blocks = excluded.consecutive_blocks",
            (domain, consecutive),
        )
    return consecutive


def set_backoff_until(domain: str, delay_seconds: float) -> None:
    """Set `domain`'s cool-down to expire `delay_seconds` from now."""
    next_allowed_at = time.time() + delay_seconds
    with _cursor() as cur:
        cur.execute(
            "UPDATE domain_backoff SET next_allowed_at = ? WHERE domain = ?",
            (next_allowed_at, domain),
        )


def record_success(domain: str) -> None:
    """Clear a domain's block streak after a clean response."""
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO domain_backoff (domain, next_allowed_at, consecutive_blocks) "
            "VALUES (?, 0, 0) "
            "ON CONFLICT(domain) DO UPDATE SET next_allowed_at = 0, consecutive_blocks = 0",
            (domain,),
        )


# --- Search jobs (observability for the queue; per-user) --------------------

def create_job(job_id: str, user_id: str, niche: str, location: str, max_results: int) -> None:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO search_jobs (id, user_id, niche, location, max_results, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'queued', ?)",
            (job_id, user_id, niche, location, max_results, time.time()),
        )


def start_job(job_id: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE search_jobs SET status = 'running', started_at = ? WHERE id = ?",
            (time.time(), job_id),
        )


def finish_job(job_id: str, result_path: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE search_jobs SET status = 'done', finished_at = ?, result_path = ? WHERE id = ?",
            (time.time(), result_path, job_id),
        )


def fail_job(job_id: str, error: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE search_jobs SET status = 'error', finished_at = ?, error = ? WHERE id = ?",
            (time.time(), error, job_id),
        )


def get_job(job_id: str, user_id: str) -> dict | None:
    """Scoped by user_id so a guessed/leaked job id from another tenant can't be polled."""
    with _cursor() as cur:
        row = cur.execute(
            "SELECT id, niche, location, max_results, status, result_path, error "
            "FROM search_jobs WHERE id = ? AND user_id = ?",
            (job_id, user_id),
        ).fetchone()
    if row is None:
        return None
    keys = ("id", "niche", "location", "max_results", "status", "result_path", "error")
    return dict(zip(keys, row))


# --- Users / API keys ---------------------------------------------------------

def create_user(label: str) -> str:
    user_id = uuid.uuid4().hex
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, label, created_at) VALUES (?, ?, ?)",
            (user_id, label, time.time()),
        )
    return user_id


def create_api_key(user_id: str) -> str:
    """Generate and store a new key for `user_id`; returns the raw key (shown once)."""
    from . import auth  # local import: auth imports store, avoid a cycle at module load

    raw_key = auth.generate_key()
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO api_keys (key_hash, user_id, created_at) VALUES (?, ?, ?)",
            (auth.hash_key(raw_key), user_id, time.time()),
        )
    return raw_key


def revoke_api_key(key_hash: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE key_hash = ?",
            (time.time(), key_hash),
        )


def get_user_for_key(key_hash: str) -> str | None:
    """Resolve a hashed API key to its user_id, or None if invalid/revoked/disabled."""
    with _cursor() as cur:
        row = cur.execute(
            "SELECT api_keys.user_id FROM api_keys JOIN users ON users.id = api_keys.user_id "
            "WHERE api_keys.key_hash = ? AND api_keys.revoked_at IS NULL AND users.disabled_at IS NULL",
            (key_hash,),
        ).fetchone()
    return row[0] if row else None


def list_users() -> list[dict]:
    with _cursor() as cur:
        rows = cur.execute("SELECT id, label, created_at, disabled_at FROM users ORDER BY created_at").fetchall()
    keys = ("id", "label", "created_at", "disabled_at")
    return [dict(zip(keys, row)) for row in rows]
