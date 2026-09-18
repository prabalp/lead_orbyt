# Data model

SQLite file: `LEADORBYT_DB_PATH` (default `./leadorbyt.db` in the process cwd). WAL mode. All public `store` functions are sync; callers use `asyncio.to_thread`.

Caches are disposable (TTL). Recreating the file loses labels, lead identity, and API key hashes — tenants must be re-created with `leadorbyt-admin`.

## Tenancy

| Table | Key | Notes |
|---|---|---|
| `users` | `id` | `label`, `created_at`, `disabled_at`, `email_verified_at` |
| `api_keys` | `key_hash` (SHA-256 of raw key) | Raw key is never stored. `revoked_at` disables the key. |
| `email_verification_tokens` | `token_hash` (SHA-256 of raw token) | 24h TTL. Pending tokens for the same email are revoked when a new link is sent. Consumed only on POST `/verify-email`. |

## Business pipeline

| Table | Purpose |
|---|---|
| `discovery_search_cache` | Current discovery-only `(user_id, niche, location, max_results)` -> researched CSV path. |
| `search_cache` | Legacy enriched-search cache retained for schema compatibility; the split workflow does not read it. |
| `search_jobs` | Observability for pollers. Status/progress. Does **not** resume work after restart. |
| `enrichment_cache` | domain -> website scrape JSON |
| `extras_cache` | domain -> third-party extras JSON |
| `leads` | Per-user business identity (`dedup_key`). Sets `is_new_lead` on upsert. |

Business `dedup_key`: normalized domain, else `name:{name}|{address}`.

## Person pipeline

| Table | Purpose |
|---|---|
| `people_search_jobs` | Same idea as `search_jobs` |
| `people_leads` | Per-user person identity |
| `person_contact_cache` | Paid reveal result keyed by Apollo person id **or** LinkedIn URL |
| `lead_states` | `(user_id, icp_hash, dedup_key)` -> `QUALIFIED` / `REJECTED` / `EMAIL_FOUND` / `NO_EMAIL_FOUND` |
| `search_frontier` | Title-token accept/reject counts for query expansion |

Person `dedup_key` (first match): `apollo:{id}`, `bettercontact:{id}`, `linkedin:{url}`, else `name:{name}|{company}`.

`icp_hash` is a hash of the ICP string (including empty ICP), so states for different briefs do not collide.

## Shared scraping / ML

| Table | Purpose |
|---|---|
| `domain_backoff` | `next_allowed_at`, `consecutive_blocks` for Maps/website fetches |
| `qualification_labels` | Agent verdicts: embedding blob + 0/1 label, scoped by `user_id` + `icp_hash` |

## Migrations

- Fresh DB: `CREATE TABLE IF NOT EXISTS` in `_SCHEMA`.
- Old single-tenant `search_cache` / jobs: dropped and recreated (`_migrate_to_multitenant`) — cache only.
- New columns on existing `search_jobs`: `_ADDITIVE_COLUMNS`.
- New column on existing `users`: `email_verified_at`.
