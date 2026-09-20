# Pipelines

## Business leads (`find_leads_maps` / `submit_search`)

Discovery and enrichment are deliberately separate. A request for leads runs
only `jobs._run_search`; enrichment requires a later, explicit
`enrich_lead_list` call after the user sees the list and approves it.

```
niche + location + max_results [+ icp]
        |
        v
discovery_search_cache hit? (only if icp is empty)
        | no
        v
discover()  Google Maps search URL
        |   pooled AsyncStealthySession
        |   scroll feed, follow /maps/place/ links
        |   parse every Maps-visible field
        v
merge_records() discovery fields only, dedupe
        |
        v
upsert_lead()  set is_new_lead
        |
        v
if icp: qualify_ml.qualify_lead() per row
        |
        v
export researched CSV  -> discovery_search_cache
        |
        v
Claude presents list and asks user whether to enrich
        | explicit yes
        v
enrich_lead_list(discovery CSV)
        |   enrich_website() per domain (cache, HTTP -> stealth)
        |   qualify.should_enrich_extras()
        |   enrich_extras() for eligible rows (cache, configured sources)
        |   optional post-enrichment ICP qualification
        v
export a new enriched CSV (Maps discovery is not repeated)
```

### Discovery details (`discovery.py`)

Google Maps is a JS SPA. Discovery does **not** use Scrapling `CrawlSpider` anymore: CrawlSpider tears down the browser at the end of `.start()`. The same selectors are used against a pooled session:

- Feed: `div[role="feed"]`
- Result links: `a.hfpxzc`
- Place fields: `data-item-id` (`authority`, `phone:tel:*`, `address`, `oloc`/`plus_code`) rather than obfuscated CSS classes
- Also harvested from the same rendered page: `mailto:`/`tel:` links, emails in visible text, and first social URL per platform

These selectors were confirmed empirically and **will break if Google changes markup**. Coordinates are parsed from `@lat,lon` in the place URL (enables free OSM extras). Discovery keeps whatever Maps already published; website enrichment later only fills blanks.

### Web/social intent posts (`find_web_signals` / `web_jobs.py`)

Separate from Maps. Call this when the user wants public posts that already express the need (a LinkedIn “looking for a speaker in Austin”, a Reddit thread, an X post, a Facebook listing). If they also want Maps businesses, call `find_leads_maps` as a second tool — order follows what they asked for first.

```
query + location + max_results [+ icp] [+ sites]
        |
        v
web_signals.discover()  DuckDuckGo HTML `site:` search
        |   LinkedIn / Reddit / X / Facebook
        |   scrapling Fetcher, stealth if blocked
        |   optional official X recent-search if the user connected X
        v
export web-signal CSV  (not mixed into the Maps enrichment schema)
```

It uses scrapling against DuckDuckGo’s HTML SERP with `site:` filters — not Google `/search`, which robots.txt disallows. Disable with `LEADORBYT_WEB_SIGNALS_ENABLED=false`. Restrict default sites with `LEADORBYT_WEB_SIGNAL_SITES`; a call can pass a `sites` subset.

### Enrichment details (`enrich_lead_list` / `enrich.py`)

Fast path is `Fetcher.get` (curl_cffi). Blocked-looking responses (4xx/5xx of interest, or body &lt; 200 bytes) escalate to the **same** stealth pool as discovery. Storefront detection runs on homepage HTML and is intentionally not part of the paid extras fan-out. Merge keeps Google Maps values and uses scrape/provider results only for empty fields.

The source CSV path is constrained to the authenticated user's output
directory. This prevents one tenant from enriching another tenant's file or
using the tool as an arbitrary file reader.

### Cache behavior

`find_leads_maps` / `submit_search` return a cached **discovery-only** CSV when `(user_id, niche, location, max_results)` is still within `CACHE_TTL_DAYS` **and `icp` is empty**. It uses `discovery_search_cache`, separate from legacy enriched search cache rows.

`enrich_company_extras` (one-off MCP tool) is **not** cached; the pipeline extras path is.

### Typical latency

A cold Google Maps discovery of ~5–20 results is on the order of **1–2 minutes** (stealth render + backoff delays). Repeat searches in the TTL window return the previous CSV path immediately. Enrichment is a second operation and may take additional time.

---

## Person leads (`find_people_leads` / `submit_people_search`)

Implemented in `people_jobs._run_people_search`. Separate queue on purpose: no browser, different spend model.

```
job_titles + location + caps [+ icp] [+ goal_new_leads] [+ filters]
        |
        v
round: person_search.search_people()
        |   BetterContact if BETTERCONTACT_API_KEY else Apollo
        |   else empty list (no error)
        v
dedupe vs seen_keys this job + upsert_person_lead (is_new_lead)
        |
        v
if icp: qualify using lead_states first, else qualify_ml
        |   REJECTED / EMAIL_FOUND / QUALIFIED reused
        |   agent_pending is fail-open, not stored as a verdict
        v
optional expansion rounds if goal_new_leads set
        |   Thompson-sample title tokens from search_frontier
        |   up to QUERY_EXPANSION_MAX_ROUNDS
        v
rank qualified first (if icp)
        |
        v
reveal emails only when max_paid_lookups > 0
        |   default 0; assistant asks before a paid rerun
        |   clamped to MAX_PAID_LOOKUPS_CEILING
        |   cache: person_contact_cache
        |   Apollo: sequential, 1 credit per verified hit
        |   BetterContact: one batch, budget = who is sent
        v
export people CSV
```

### Provider contract (`sources/person_search.py`)

| | BetterContact | Apollo |
|---|---|---|
| Used when | `BETTERCONTACT_API_KEY` set (wins if both set) | else `APOLLO_API_KEY` |
| Free search includes | full name, LinkedIn, company domain | first name + obfuscated last name; identity behind paid match |
| Reveal | batch by LinkedIn URL | one person id at a time |
| CSV `source_provider` | `bettercontact` | `apollo` |

Reveal is always through the same provider that searched. Misses are free; hits cost a credit. `max_paid_lookups` bounds **attempts**, not hits.

### Query expansion

Only when `goal_new_leads` is set. Tokens are words from job titles of previously qualified/rejected people for this `(user, icp)`. A plain search never touches `query_expansion.py`.

### Agent labeling loop

1. `list_unlabeled_leads` — free search, return only `agent_pending` people.
2. Caller judges each against the ICP.
3. `submit_lead_verdicts` — writes `qualification_labels`.
4. Later `find_people_leads(..., icp=...)` can `gate_accept` / `gate_reject`.

There is **no** equivalent preview tool for business leads. `find_leads_maps(..., icp=...)` still scores rows, but unlabeled businesses stay fail-open in the CSV until the same verdicts (keyed by user+ICP, not entity type) accumulate.
