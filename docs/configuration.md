# Configuration

Copy `.env.example` to `.env` in the **working directory** of `leadorbyt` (same place as `leadorbyt.db`). `config.py` loads it via `python-dotenv` without overriding variables already in the real environment.

A blank API key means that source is **never called**. The Maps + website pipeline runs with zero extras keys.

## Server and scale

| Variable | Default | Meaning |
|---|---|---|
| `LEADORBYT_HOST` | `0.0.0.0` | Bind address |
| `LEADORBYT_PORT` | `8000` | Bind port |
| `LEADORBYT_DB_PATH` | `./leadorbyt.db` | SQLite |
| `LEADORBYT_OUTPUT_DIR` | `./leads_output` | CSV root (per-user subdirs) |
| `LEADORBYT_CACHE_TTL_DAYS` | `7` | Search / enrichment / extras TTL |
| `LEADORBYT_DISCOVERY_POOL_SIZE` | `4` | Warm Chromium sessions |
| `LEADORBYT_SEARCH_WORKERS` | same as pool | Business job workers |
| `LEADORBYT_MAX_CONCURRENCY` | `8` | Enrichment / extras / qualify fan-out |
| `LEADORBYT_PUBLIC_URL` | `http://127.0.0.1:8000` | Origin shown on the signup page, in Claude config, and in verification links |
| `LEADORBYT_SIGNUP_ENABLED` | `true` | Public email form that sends a verification link, then mints an API key |
| `LEADORBYT_SIGNUP_PER_HOUR` | `8` | Signup attempts allowed per IP per hour |
| `RESEND_API_KEY` | empty | Resend key for verification email (same name as Mail Orbyt). Blank logs the message instead of sending |
| `EMAIL_FROM_ADDRESS` | `Lead Orbyt <onboarding@resend.dev>` | From address; must be on a Resend-verified domain in production |

Docker sets `LEADORBYT_DB_PATH=/data/leadorbyt.db` and `LEADORBYT_OUTPUT_DIR=/data/leads_output`. Compose publishes `127.0.0.1:8010:8000` and does **not** load `.env` unless you add `env_file`.

## Politeness / fetch

| Variable | Default |
|---|---|
| `LEADORBYT_USER_AGENT` | Chrome 124 UA |
| `LEADORBYT_DOWNLOAD_DELAY` | `1.0` |
| `LEADORBYT_ROBOTS_TXT_OBEY` | `true` |
| `LEADORBYT_REQUEST_TIMEOUT` | `20` |
| `LEADORBYT_AUTOTHROTTLE_*` | start `3`, max `30` |
| `LEADORBYT_SOURCE_HTTP_TIMEOUT` | `15` (REST extras) |

## Qualification

| Variable | Default | Meaning |
|---|---|---|
| `LEADORBYT_REQUIRE_WEBSITE_FOR_EXTRAS` | `true` | Skip paid extras with no website |
| `LEADORBYT_EXCLUDE_CATEGORIES` | empty | Comma-separated substrings vs Maps category |
| `LEADORBYT_QUALIFY_MIN_LABELS` | `8` | Verdicts before the GP may decide |
| `LEADORBYT_QUALIFY_GATE_CONFIDENCE` | `0.85` | Posterior mean threshold |
| `LEADORBYT_QUALIFY_GATE_MAX_STD` | `0.15` | Max posterior std to trust the mean |

## People search

| Variable | Default |
|---|---|
| `LEADORBYT_MAX_PAID_LOOKUPS_CEILING` | `50` |
| `LEADORBYT_QUERY_EXPANSION_MAX_ROUNDS` | `2` |
| `LEADORBYT_QUERY_EXPANSION_TOKENS_PER_ROUND` | `3` |
| `BETTERCONTACT_API_KEY` | empty (Apollo used if only Apollo is set) |
| `LEADORBYT_BETTERCONTACT_POLL_INTERVAL` | `3.0` |
| `LEADORBYT_BETTERCONTACT_MAX_POLL_ATTEMPTS` | `40` |
| `APOLLO_API_KEY` | empty |

## Extra sources

See `.env.example` for the full key list (Places, firmographics, tech, jobs, news, contact APIs). Always-on without keys when a website exists:

- OpenCorporates (low-volume keyless)
- Greenhouse / Lever / Ashby public boards
- The Muse (keyless low volume)
- Shopify/WooCommerce HTML check (in `enrich.py`, not the registry)

OpenStreetMap/Overpass runs only when discovery extracted lat/lon.

**Configured but unused:** `X_BEARER_TOKEN` / `sources/twitter.py` is not in `registry._SOURCES`.

**Stubbed:** `INDEED_PUBLISHER_ID`, `LINKEDIN_API_KEY` — modules always report not integrated.
