# Lead Orbyt architecture

Lead Orbyt is a **multi-tenant MCP server** that finds leads and writes CSVs. It does not send outreach. Clients (Claude Desktop, other MCP agents, automation) call tools over **streamable HTTP** with a Bearer API key. The hosted signup page sends a **transactional** verification email via Resend so a visitor can prove they own the address before an API key is minted.

There are independent discovery surfaces. The agent picks which to call first from what the user asked for; if they want more than one, it calls them sequentially.

| Surface | Ideal lead | Discovery | Follow-up |
|---|---|---|---|
| **Business leads** | A local company (shop, plumber, clinic) | `find_leads_maps`: Google Maps only | `enrich_lead_list`: explicit opt-in homepage + REST fan-out |
| **Web/social intent** | Someone already posting a need | `find_web_signals`: DuckDuckGo HTML `site:` (LinkedIn, Reddit, X, Facebook) | None bundled; optional later Maps or people search |
| **Person leads** | A named decision-maker (CISO, IT director) | BetterContact or Apollo REST search | Explicit opt-in email reveal, cache-first, hard-capped |

They share auth, SQLite, the offline ICP gate, and CSV export. Maps, web, people, and Reddit API each have their own in-memory job queue. Person search is pure HTTP (no browser).

## System map

```
MCP client (mcp-remote / HTTP)
        |
        v
uvicorn + MCPServer  (leadorbyt/server.py)
        |
        +-- ApiKeyAuthMiddleware  (auth.py)  --> users / api_keys
        |
        +-- Maps tools --------> jobs.py queue --------> discovery.py -> Maps CSV
        |                            |
        |                            +-- user approves enrichment
        |                                                 + enrich.py (Fetcher -> stealth)
        |                                                 + sources/registry.py
        |                                                 + qualify.py (paid-fan-out gate)
        |                                                 + merge.py -> enriched CSV
        |
        +-- web/social tools --> web_jobs.py queue -----> web_signals.py -> intent CSV
        |
        +-- people tools ------> people_jobs.py queue --> workers
        |                            |
        |                            + sources/person_search.py
        |                            + query_expansion.py (opt-in)
        |                            + qualify_ml.py + lead_states
        |                            + paid reveal (Apollo sequential / BetterContact batch)
        |                            + people_merge.py -> CSV
        |
        +-- SQLite  (store.py, LEADORBYT_DB_PATH)
```

Jobs live in **in-memory asyncio queues**. SQLite is the durable cache, tenant store, and progress log. A process restart drops queued/running jobs; caches survive.

## Runtime processes

| Process | Entry | Role |
|---|---|---|
| Server | `leadorbyt` / `python -m leadorbyt.server` | Long-lived HTTP MCP on `LEADORBYT_HOST`:`LEADORBYT_PORT` (default `0.0.0.0:8000`) |
| Admin CLI | `leadorbyt-admin` | Create tenants, mint/revoke keys. Never exposed as an MCP tool. |
| Docker | `docker-compose.yml` | Publishes `127.0.0.1:8010` -> container `:8000`, persists `/data` |

Workers start lazily on the first `submit` / `find_*` call (`jobs.start_workers`, `web_jobs.start_workers`, `people_jobs.start_workers`). Browser sessions start on first `browser_pool.checkout()`.

## Layering

### Transport and tenancy

- `MCPServer` from `mcp.server.mcpserver` (SDK v2; not FastMCP).
- Auth is **not** the SDK OAuth path. `auth.ApiKeyAuthMiddleware` checks `Authorization: Bearer <key>`, SHA-256 hashes it, looks up `api_keys` -> `users`.
- Tools call `auth.require_user_id()`. Job workers **do not** read the request context; they carry `user_id` on the job object.
- CSVs land under `{OUTPUT_DIR}/{user_id}/`. Search cache and lead identity are scoped by `user_id`.
- Hosted signup (`signup.py`, public `/`) is a landing page plus email verification (Resend, Mail Orbyt-style: GET shows a confirm button, POST consumes the hashed token). After confirm it mints a tenant and shows `{PUBLIC_URL}/mcp` plus a one-time API key. `/mcp` stays behind Bearer auth.

### Persistence (`store.py`)

One SQLite file, WAL, a process-wide write lock (sqlite3 is used from `asyncio.to_thread`). Schema and additive migrations live in this module. See [data-model.md](data-model.md).

### Scraping politeness

| Mechanism | Where | Why |
|---|---|---|
| robots.txt | `robots.py` | Discovery left CrawlSpider; still honors Google Maps allow for `/maps/search/` and `/maps/place/` |
| Shared per-domain backoff | `backoff.py` + `domain_backoff` table | Scrapling AutoThrottle resets every spider run; this does not |
| Browser pool | `browser_pool.py` | Reuse Chromium instead of launching per search |
| Concurrency caps | `MAX_CONCURRENCY`, `SEARCH_WORKERS`, `DISCOVERY_POOL_SIZE` | Bound in-flight scrapes and paid extras |

Third-party REST sources (`sources/base.py`) do **not** use the browser pool or domain backoff. They are documented APIs with their own rate limits; failures are swallowed per source.

### Qualification (two different gates)

1. **`qualify.py`:** free rules before **paid business extras**. Skip if no website (default) or category matches `LEADORBYT_EXCLUDE_CATEGORIES`.
2. **`qualify_ml.py`:** optional ICP scoring. No LLM, no API key. A Gaussian Process over HashingVectorizer embeddings learns from **agent-supplied verdicts** (`submit_lead_verdicts`). Until `LEADORBYT_QUALIFY_MIN_LABELS` (default 8) exist for `(user, icp)`, every lead is `qualified=True` / `source=agent_pending` (fail-open).

Person leads also keep a per-`(user, icp, dedup_key)` **state machine** in `lead_states` so a later search does not re-qualify or re-reveal someone already decided.

## What this is not

- Not OpenOutFind: no licensed data feed, no address-resolve product, Google Maps scrape instead of browserless APIs for businesses.
- Not OpenOutreach: no campaigns, no outbound mail.
- Indeed and LinkedIn modules exist as **explicit non-integrations** (no public self-serve search API that this project will scrape).
- `sources/twitter.py` exists and `X_BEARER_TOKEN` is read in config, but Twitter/X is **not** registered in `sources/registry.py`, so it is never called.

## Related docs

- [pipelines.md](pipelines.md): step-by-step for Maps, web/social, and people search
- [mcp-tools.md](mcp-tools.md): tool contracts
- [data-model.md](data-model.md): SQLite tables
- [configuration.md](configuration.md): env vars
- [testing.md](testing.md): what the test suite actually covers
