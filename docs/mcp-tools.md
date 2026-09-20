# MCP tools

All tools require a valid API key on the HTTP request. Errors at the tool boundary are `RuntimeError` with a stable prefix:

```
error: <type>: <message>
```

Types: `blocked`, `transport_error`, `robots_disallowed`, `invalid_input`, `not_found`, `internal` (`leadorbyt/errors.py`).

Blocking tools (`find_leads_maps`, `find_web_signals`, `find_people_leads`) wait on the job. Async twins (`submit_*` + `get_*_status`) return immediately.

**Which tool first:** `find_leads_maps` for local businesses, `find_web_signals` for people posting a need on LinkedIn/Reddit/X/Facebook, `find_people_leads` for named titles at companies. If the user wants more than one, call them sequentially in the order they asked. Do not chain a second source unless they asked for it.

## Business (Google Maps)

### `find_leads_maps(niche, location, max_results=20, icp="") -> dict`

Maps discovery only. Returns `status`, absolute `result_path`, `lead_count`,
`enriched: false`, and `next_action`. Does **not** search LinkedIn/Reddit/X/Facebook
(that is `find_web_signals`). The assistant must present the Maps list and ask
before enrichment or a second source. This tool cannot visit business websites
or call enrichment providers.

### `submit_search(...) -> str`

Enqueue (or synthesize a completed job on cache hit). Returns `job_id`.

### `get_search_status(job_id) -> dict`

`status`: `queued` | `running` | `done` | `error` | `unknown`. Also `result_path`, `error`, `error_type`, `stage`, `items_discovered`, `items_enriched`, `items_total`.

Unknown / other-tenant job ids look the same (`unknown` + `not_found`) so ids are not enumerable across tenants.

`stage` values: `queued`, `discovering`, `researching`, `merging`, `qualifying`, `exporting`.

## Web/social intent

### `find_web_signals(query, location="", max_results=20, icp="", sites=None) -> dict`

DuckDuckGo HTML `site:` search (scrapling). Returns `status` (`web_signals_complete`),
`result_path`, `signal_count`, and `next_action`. Optional `sites` subset of
`linkedin`, `reddit`, `x`, `facebook`. Disabled when `LEADORBYT_WEB_SIGNALS_ENABLED=false`.

### `submit_web_signal_search(...) -> str` / `get_web_signal_search_status(job_id) -> dict`

Same pattern as Maps jobs. Progress field: `signals_found`. Stages: `queued`, `searching`, `qualifying`, `exporting`.

### `enrich_lead_list(lead_list_path, icp="") -> dict`

Explicit second phase. Call only after the user approves enrichment. Reads a
discovery CSV created for the authenticated tenant, enriches website contacts
and configured third-party sources, and writes a new CSV without repeating
Maps discovery.

### `enrich_url(url) -> dict`

One website. Cached by normalized domain. Returns contact/social fields.

### `enrich_company_extras(business_name, website="", location="") -> dict`

Live fan-out to every configured extras source. **Not cached.** Includes `sources_used`.

## People

### `find_people_leads(job_titles, location, icp="", max_results=20, max_paid_lookups=0, goal_new_leads=None, seniorities=None, headcount_min=None, headcount_max=None, industries=None, technologies=None) -> str`

Person CSV path. Default zero means discovery cannot spend. After showing the
list, ask before rerunning with a positive lookup cap.
`max_paid_lookups` is `min(requested, LEADORBYT_MAX_PAID_LOOKUPS_CEILING)`.

If neither BetterContact nor Apollo is configured, search returns no people and you still get an (empty) CSV rather than an auth error.

### `submit_people_search(...) -> str` / `get_people_search_status(job_id) -> dict`

Same pattern as business jobs. Progress fields: `people_found`, `paid_lookups_used`. Stages: `queued`, `searching`, `qualifying`, `revealing`, `exporting`.

### `list_unlabeled_leads(job_titles, location, icp, max_results=20, ...filters) -> list[dict]`

Free preview. Does not reveal emails. Adds `profile_text` and `dedup_key`.

### `submit_lead_verdicts(icp, verdicts) -> {"labels_added": n}`

Each verdict: `profile_text`, `qualified` (bool), optional `reason`, `dedup_key`. Embeddings are stored for the GP gate. Labels are scoped by `(user_id, icp_hash)`.

## Social login

Official OAuth only. Claude must never collect a LinkedIn/X/Facebook password.

### `list_social_connections() -> dict`

Which of Reddit/X are configured and already connected; LinkedIn/Facebook listed as not connectable.

### `connect_social_account(provider) -> dict`

`reddit` or `x`: `{status: login_required, login_url}`. User opens the URL in a browser. `linkedin` / `facebook`: `{status: unsupported, reason}`.

### `disconnect_social_account(provider) -> dict`

Drops the stored token for this tenant.

If Reddit has no app credentials and the user has not connected Reddit, `find_reddit_signals` returns the same `login_required` payload instead of an empty CSV.

## CSV schemas

Business (`merge.CSV_FIELDS`): `business_name`, `category`, `website`, `email`, `phone`, `address`, `plus_code`, `maps_url`, `lat`, `lon`, socials, `storefront_platforms`, `extra_sources_used`, `extra_data_json`, `discovered_by_query`, `is_new_lead`, `qualified`, `qualification_score`, `qualification_reason`.

Web/social intent (`web_signals.WEB_SIGNAL_CSV_FIELDS`): `source`, `author`, `community`, `post_title`, `post_body`, `url`, `email`, `discovered_by_query`, `is_new_lead`, qualification columns.

People (`people_merge.PEOPLE_CSV_FIELDS`): `full_name`, `title`, `company_name`, `company_domain`, `linkedin_url`, `email`, qualification columns, `source_provider`, `discovered_by_query`, `is_new_lead`.

## Claude Desktop

The server is HTTP, not stdio. Bridge with `mcp-remote`:

```json
{
  "mcpServers": {
    "leadorbyt": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://127.0.0.1:8000/mcp",
        "--header",
        "Authorization: Bearer <RAW_KEY>"
      ]
    }
  }
}
```

Create the key with `leadorbyt-admin create-user "<label>"` (printed once).
