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
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from . import config

_write_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS enrichment_cache (
    domain TEXT PRIMARY KEY,
    data_json TEXT NOT NULL,
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS extras_cache (
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
    disabled_at REAL,
    email_verified_at REAL
);

CREATE TABLE IF NOT EXISTS email_verification_tokens (
    token_hash TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_email_verification_email
    ON email_verification_tokens(email);

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

CREATE TABLE IF NOT EXISTS discovery_search_cache (
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
    error TEXT,
    error_type TEXT,
    stage TEXT,
    items_discovered INTEGER,
    items_enriched INTEGER,
    items_total INTEGER
);

CREATE TABLE IF NOT EXISTS leads (
    user_id TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    business_name TEXT NOT NULL,
    domain TEXT NOT NULL,
    niche TEXT NOT NULL,
    location TEXT NOT NULL,
    discovered_by_query TEXT NOT NULL,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    PRIMARY KEY (user_id, dedup_key)
);

CREATE TABLE IF NOT EXISTS qualification_labels (
    user_id TEXT NOT NULL,
    icp_hash TEXT NOT NULL,
    domain TEXT NOT NULL,
    embedding_blob BLOB NOT NULL,
    label REAL NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS person_contact_cache (
    person_id TEXT PRIMARY KEY,
    data_json TEXT NOT NULL,
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS people_leads (
    user_id TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    full_name TEXT NOT NULL,
    company_domain TEXT NOT NULL,
    job_titles TEXT NOT NULL,
    location TEXT NOT NULL,
    discovered_by_query TEXT NOT NULL,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    PRIMARY KEY (user_id, dedup_key)
);

CREATE TABLE IF NOT EXISTS people_search_jobs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    job_titles TEXT NOT NULL,
    location TEXT NOT NULL,
    icp TEXT,
    max_results INTEGER NOT NULL,
    max_paid_lookups INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    result_path TEXT,
    error TEXT,
    error_type TEXT,
    stage TEXT,
    people_found INTEGER,
    paid_lookups_used INTEGER
);

CREATE TABLE IF NOT EXISTS search_frontier (
    user_id TEXT NOT NULL,
    icp_hash TEXT NOT NULL,
    token TEXT NOT NULL,
    field TEXT NOT NULL,
    accept_count INTEGER NOT NULL DEFAULT 0,
    reject_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, icp_hash, token)
);

CREATE TABLE IF NOT EXISTS lead_states (
    user_id TEXT NOT NULL,
    icp_hash TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, icp_hash, dedup_key)
);
"""

# (table, column, sqlite type + default) additive migrations for existing
# installs -- CREATE TABLE IF NOT EXISTS above only helps fresh databases; a
# leadorbyt.db from before this change needs these columns bolted on.
_ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    ("search_jobs", "error_type", "TEXT"),
    ("search_jobs", "stage", "TEXT"),
    ("search_jobs", "items_discovered", "INTEGER"),
    ("search_jobs", "items_enriched", "INTEGER"),
    ("search_jobs", "items_total", "INTEGER"),
    ("users", "email_verified_at", "REAL"),
]


def _migrate_additive_columns(conn: sqlite3.Connection) -> None:
    for table, column, coltype in _ADDITIVE_COLUMNS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    conn.commit()

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
    _migrate_additive_columns(conn)
    _connection = conn
    return conn


@contextmanager
def _cursor():
    """Every caller reaches this through `asyncio.to_thread`, so concurrent
    callers (e.g. `_qualify_all`'s gathered tasks) land here from different
    OS threads at once. The single shared `sqlite3.Connection` is opened
    with `check_same_thread=False` but is not otherwise safe for concurrent
    use from multiple threads -- observed in practice as a raw
    `SystemError: error return without exception set` out of `commit()`
    under concurrent writes -- so all access is serialized through this lock.
    """
    with _write_lock:
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


# --- Extras cache (third-party source fan-out, keyed by domain) ------------

def get_extras(domain: str) -> dict | None:
    if not domain:
        return None
    ttl_seconds = config.CACHE_TTL_DAYS * 86400
    cutoff = time.time() - ttl_seconds
    with _cursor() as cur:
        row = cur.execute(
            "SELECT data_json, fetched_at FROM extras_cache WHERE domain = ?",
            (domain,),
        ).fetchone()
    if row is None:
        return None
    data_json, fetched_at = row
    if fetched_at < cutoff:
        return None
    return json.loads(data_json)


def put_extras(domain: str, data: dict) -> None:
    if not domain:
        return
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO extras_cache (domain, data_json, fetched_at) VALUES (?, ?, ?) "
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


def get_discovery_search(user_id: str, niche: str, location: str, max_results: int) -> str | None:
    """Return a cached discovery-only CSV without confusing it with legacy enriched results."""
    ttl_seconds = config.CACHE_TTL_DAYS * 86400
    cutoff = time.time() - ttl_seconds
    with _cursor() as cur:
        row = cur.execute(
            "SELECT result_path FROM discovery_search_cache "
            "WHERE user_id = ? AND niche = ? AND location = ? AND max_results = ? AND finished_at >= ?",
            (user_id, niche, location, max_results, cutoff),
        ).fetchone()
    if row is None:
        return None
    result_path = row[0]
    if not Path(result_path).exists():
        return None
    return result_path


def put_discovery_search(
    user_id: str, niche: str, location: str, max_results: int, result_path: str
) -> None:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO discovery_search_cache "
            "(user_id, niche, location, max_results, result_path, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, niche, location, max_results) DO UPDATE SET "
            "result_path = excluded.result_path, finished_at = excluded.finished_at",
            (user_id, niche, location, max_results, result_path, time.time()),
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


def fail_job(job_id: str, error: str, error_type: str | None = None) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE search_jobs SET status = 'error', finished_at = ?, error = ?, error_type = ? WHERE id = ?",
            (time.time(), error, error_type, job_id),
        )


def update_job_progress(
    job_id: str,
    stage: str | None = None,
    items_discovered: int | None = None,
    items_enriched: int | None = None,
    items_total: int | None = None,
) -> None:
    """Patch whichever progress fields are given, leaving the rest untouched."""
    fields, values = [], []
    for column, value in (
        ("stage", stage),
        ("items_discovered", items_discovered),
        ("items_enriched", items_enriched),
        ("items_total", items_total),
    ):
        if value is not None:
            fields.append(f"{column} = ?")
            values.append(value)
    if not fields:
        return
    values.append(job_id)
    with _cursor() as cur:
        cur.execute(f"UPDATE search_jobs SET {', '.join(fields)} WHERE id = ?", values)


def get_job(job_id: str, user_id: str) -> dict | None:
    """Scoped by user_id so a guessed/leaked job id from another tenant can't be polled."""
    with _cursor() as cur:
        row = cur.execute(
            "SELECT id, niche, location, max_results, status, result_path, error, error_type, "
            "stage, items_discovered, items_enriched, items_total "
            "FROM search_jobs WHERE id = ? AND user_id = ?",
            (job_id, user_id),
        ).fetchone()
    if row is None:
        return None
    keys = (
        "id", "niche", "location", "max_results", "status", "result_path", "error", "error_type",
        "stage", "items_discovered", "items_enriched", "items_total",
    )
    return dict(zip(keys, row))


# --- Leads (cross-run dedup/provenance; per-user) ----------------------------

def upsert_lead(
    user_id: str,
    dedup_key: str,
    business_name: str,
    domain: str,
    niche: str,
    location: str,
    discovered_by_query: str,
) -> bool:
    """Record a lead as seen; returns True if this is the first time we've seen it."""
    now = time.time()
    with _cursor() as cur:
        row = cur.execute(
            "SELECT 1 FROM leads WHERE user_id = ? AND dedup_key = ?",
            (user_id, dedup_key),
        ).fetchone()
        is_new = row is None
        cur.execute(
            "INSERT INTO leads (user_id, dedup_key, business_name, domain, niche, location, "
            "discovered_by_query, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, dedup_key) DO UPDATE SET last_seen_at = excluded.last_seen_at",
            (user_id, dedup_key, business_name, domain, niche, location, discovered_by_query, now, now),
        )
    return is_new


# --- ML qualification (per-user, per-ICP) ------------------------------------

def icp_hash(icp: str) -> str:
    import hashlib

    return hashlib.sha256(icp.encode("utf-8")).hexdigest()


def get_qualification_labels(user_id: str, icp_hash_: str) -> list[tuple[bytes, float]]:
    with _cursor() as cur:
        rows = cur.execute(
            "SELECT embedding_blob, label FROM qualification_labels WHERE user_id = ? AND icp_hash = ?",
            (user_id, icp_hash_),
        ).fetchall()
    return [(row[0], row[1]) for row in rows]


def add_qualification_label(user_id: str, icp_hash_: str, domain: str, embedding: bytes, label: float) -> None:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO qualification_labels (user_id, icp_hash, domain, embedding_blob, label, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, icp_hash_, domain, embedding, label, time.time()),
        )


# --- Person contact cache (email reveal results, keyed by provider person id) ---

def get_person_contact(person_id: str) -> dict | None:
    if not person_id:
        return None
    ttl_seconds = config.CACHE_TTL_DAYS * 86400
    cutoff = time.time() - ttl_seconds
    with _cursor() as cur:
        row = cur.execute(
            "SELECT data_json, fetched_at FROM person_contact_cache WHERE person_id = ?",
            (person_id,),
        ).fetchone()
    if row is None:
        return None
    data_json, fetched_at = row
    if fetched_at < cutoff:
        return None
    return json.loads(data_json)


def put_person_contact(person_id: str, data: dict) -> None:
    if not person_id:
        return
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO person_contact_cache (person_id, data_json, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(person_id) DO UPDATE SET data_json = excluded.data_json, "
            "fetched_at = excluded.fetched_at",
            (person_id, json.dumps(data), time.time()),
        )


# --- People leads (cross-run dedup/provenance; per-user) --------------------

def upsert_person_lead(
    user_id: str,
    dedup_key: str,
    full_name: str,
    company_domain: str,
    job_titles: str,
    location: str,
    discovered_by_query: str,
) -> bool:
    """Record a person lead as seen; returns True if this is the first time we've seen it."""
    now = time.time()
    with _cursor() as cur:
        row = cur.execute(
            "SELECT 1 FROM people_leads WHERE user_id = ? AND dedup_key = ?",
            (user_id, dedup_key),
        ).fetchone()
        is_new = row is None
        cur.execute(
            "INSERT INTO people_leads (user_id, dedup_key, full_name, company_domain, job_titles, "
            "location, discovered_by_query, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, dedup_key) DO UPDATE SET last_seen_at = excluded.last_seen_at",
            (user_id, dedup_key, full_name, company_domain, job_titles, location, discovered_by_query, now, now),
        )
    return is_new


# --- People search jobs (observability for the people-lead queue; per-user) ---

def create_people_job(
    job_id: str, user_id: str, job_titles: str, location: str, icp: str, max_results: int, max_paid_lookups: int
) -> None:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO people_search_jobs (id, user_id, job_titles, location, icp, max_results, "
            "max_paid_lookups, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
            (job_id, user_id, job_titles, location, icp, max_results, max_paid_lookups, time.time()),
        )


def start_people_job(job_id: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE people_search_jobs SET status = 'running', started_at = ? WHERE id = ?",
            (time.time(), job_id),
        )


def finish_people_job(job_id: str, result_path: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE people_search_jobs SET status = 'done', finished_at = ?, result_path = ? WHERE id = ?",
            (time.time(), result_path, job_id),
        )


def fail_people_job(job_id: str, error: str, error_type: str | None = None) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE people_search_jobs SET status = 'error', finished_at = ?, error = ?, error_type = ? WHERE id = ?",
            (time.time(), error, error_type, job_id),
        )


def update_people_job_progress(
    job_id: str,
    stage: str | None = None,
    people_found: int | None = None,
    paid_lookups_used: int | None = None,
) -> None:
    fields, values = [], []
    for column, value in (
        ("stage", stage),
        ("people_found", people_found),
        ("paid_lookups_used", paid_lookups_used),
    ):
        if value is not None:
            fields.append(f"{column} = ?")
            values.append(value)
    if not fields:
        return
    values.append(job_id)
    with _cursor() as cur:
        cur.execute(f"UPDATE people_search_jobs SET {', '.join(fields)} WHERE id = ?", values)


def get_people_job(job_id: str, user_id: str) -> dict | None:
    """Scoped by user_id so a guessed/leaked job id from another tenant can't be polled."""
    with _cursor() as cur:
        row = cur.execute(
            "SELECT id, job_titles, location, icp, max_results, max_paid_lookups, status, result_path, "
            "error, error_type, stage, people_found, paid_lookups_used "
            "FROM people_search_jobs WHERE id = ? AND user_id = ?",
            (job_id, user_id),
        ).fetchone()
    if row is None:
        return None
    keys = (
        "id", "job_titles", "location", "icp", "max_results", "max_paid_lookups", "status", "result_path",
        "error", "error_type", "stage", "people_found", "paid_lookups_used",
    )
    return dict(zip(keys, row))


# --- Search frontier (adaptive query expansion; per-user, per-ICP) ----------

def bump_frontier_token(user_id: str, icp_hash: str, token: str, field: str, accepted: bool) -> None:
    accept_inc, reject_inc = (1, 0) if accepted else (0, 1)
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO search_frontier (user_id, icp_hash, token, field, accept_count, reject_count) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, icp_hash, token) DO UPDATE SET "
            "accept_count = accept_count + excluded.accept_count, "
            "reject_count = reject_count + excluded.reject_count",
            (user_id, icp_hash, token, field, accept_inc, reject_inc),
        )


def get_frontier_tokens(user_id: str, icp_hash: str) -> list[tuple[str, str, int, int]]:
    """Returns (token, field, accept_count, reject_count) rows."""
    with _cursor() as cur:
        rows = cur.execute(
            "SELECT token, field, accept_count, reject_count FROM search_frontier "
            "WHERE user_id = ? AND icp_hash = ?",
            (user_id, icp_hash),
        ).fetchall()
    return [tuple(row) for row in rows]


# --- Lead lifecycle state (per-user, per-ICP) --------------------------------

def get_lead_state(user_id: str, icp_hash: str, dedup_key: str) -> str | None:
    with _cursor() as cur:
        row = cur.execute(
            "SELECT state FROM lead_states WHERE user_id = ? AND icp_hash = ? AND dedup_key = ?",
            (user_id, icp_hash, dedup_key),
        ).fetchone()
    return row[0] if row else None


def set_lead_state(user_id: str, icp_hash: str, dedup_key: str, state: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO lead_states (user_id, icp_hash, dedup_key, state, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, icp_hash, dedup_key) DO UPDATE SET state = excluded.state, "
            "updated_at = excluded.updated_at",
            (user_id, icp_hash, dedup_key, state, time.time()),
        )


# --- Users / API keys ---------------------------------------------------------

def get_user_by_email(email: str) -> dict | None:
    """Look up a tenant whose label is this email (case-insensitive)."""
    normalized = email.strip().lower()
    if not normalized:
        return None
    with _cursor() as cur:
        row = cur.execute(
            "SELECT id, label, created_at, disabled_at, email_verified_at "
            "FROM users WHERE lower(label) = ?",
            (normalized,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "label": row[1],
        "created_at": row[2],
        "disabled_at": row[3],
        "email_verified_at": row[4],
    }


def get_or_create_user_by_email(email: str) -> tuple[str, bool]:
    """Return (user_id, created). Reuses the tenant if this email already signed up."""
    existing = get_user_by_email(email)
    if existing is not None:
        if existing["disabled_at"]:
            raise ValueError("this account is disabled")
        return existing["id"], False
    return create_user(email.strip().lower()), True


def mark_email_verified(user_id: str) -> None:
    """Stamp first verification time; later confirms leave the original in place."""
    now = time.time()
    with _cursor() as cur:
        cur.execute(
            "UPDATE users SET email_verified_at = COALESCE(email_verified_at, ?) WHERE id = ?",
            (now, user_id),
        )


def create_email_verification(email: str, token_hash: str, expires_at: float) -> None:
    """Store a hashed token and revoke any still-pending token for this email."""
    now = time.time()
    with _cursor() as cur:
        cur.execute(
            "UPDATE email_verification_tokens SET consumed_at = ? "
            "WHERE email = ? AND consumed_at IS NULL",
            (now, email),
        )
        cur.execute(
            "INSERT INTO email_verification_tokens "
            "(token_hash, email, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token_hash, email, now, expires_at),
        )


def delete_email_verification(token_hash: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "DELETE FROM email_verification_tokens WHERE token_hash = ?",
            (token_hash,),
        )


def get_email_verification(token_hash: str) -> dict | None:
    with _cursor() as cur:
        row = cur.execute(
            "SELECT token_hash, email, created_at, expires_at, consumed_at "
            "FROM email_verification_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
    if row is None:
        return None
    keys = ("token_hash", "email", "created_at", "expires_at", "consumed_at")
    return dict(zip(keys, row))


def consume_email_verification(token_hash: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE email_verification_tokens SET consumed_at = ? WHERE token_hash = ?",
            (time.time(), token_hash),
        )


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
