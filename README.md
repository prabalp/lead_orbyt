# leadorbyt

An MCP server that finds businesses of a given niche in a given location and pulls
their contact info (email, phone, website, socials) into a CSV.

## How it works

1. **Discovery** (`discovery.py`) -- `GoogleMapsDiscoverySpider`, a Scrapling
   `CrawlSpider`, renders a Google Maps search with a stealth browser session,
   scrolls the results feed to load listings, then follows each result's
   place-detail link (`LinkExtractor(allow=r"/maps/place/")`) to pull
   `business_name`, `category`, `website`, `phone`, `address` from the
   detail page's `data-item-id` attributes.
2. **Enrichment** (`enrich.py`) -- for each discovered website, fetches the
   homepage with the fast `Fetcher` (plain HTTP), escalating to
   `StealthyFetcher` (real browser) if the response looks blocked or empty.
   Also follows any on-page `/contact` or `/about` links. Extracts emails,
   phones, and social links via `extractors.py`.
3. **Merge + export** (`merge.py`) -- joins discovery and enrichment records
   by normalized website domain and exports the merged `ItemList` via
   Scrapling's built-in `.to_csv()`.
4. **MCP server** (`server.py`) -- exposes `find_leads` and `enrich_url` as
   MCP tools over stdio, with an in-memory cache so repeat searches/lookups
   in the same session don't re-scrape.

## Why Google Maps, not Yelp

The task brief asked to pick whichever of Google Maps / Yelp is more
scrapeable. A live check during development showed:

- **Yelp**: the very first plain HTTP request to a search URL returns
  `403` with a DataDome CAPTCHA challenge page (`geo.captcha-delivery.com`).
  Not scrapeable without heavy, ongoing anti-bot evasion.
- **Google Maps**: returns a real `200` with actual result data, and its
  `robots.txt` explicitly `Allow`s `/maps/search/` and `/maps/place/` for
  generic user agents.

Google Maps was used, with `robots_txt_obey = True`.

## Things I had to guess / confirm empirically (not from Scrapling's source)

Google Maps' own DOM is undocumented and obfuscated -- Scrapling has nothing
to say about it, since it's not part of Scrapling's API. During development
I rendered live search and place pages to confirm the actual markup rather
than guessing from memory:

- Search feed: `div[role="feed"]` (scroll container), `a.hfpxzc` (result
  links, each with an `aria-label` = business name, `href` = place URL).
- Place detail page: `h1` (name), `button[jsaction*="category"]` (category),
  `a[data-item-id="authority"]` (website), `button[data-item-id^="phone:tel:"]`
  (phone, via its `aria-label`), `button[data-item-id="address"]` (address,
  via its `aria-label`).

These are stable-looking `data-item-id` attributes rather than the
obfuscated CSS classes (`Nv2PK`, `qBF1Pd`, etc. -- also seen during
development, deliberately avoided), but Google can still change this at any
time. If discovery stops finding businesses, `_parse_place_page()` and
`RESULT_LINK_SELECTOR`/`FEED_SELECTOR` in `discovery.py` are the place to fix.

## Scrapling API note

This project targets **Scrapling 0.4.14**, confirmed by cloning
`D4Vinci/Scrapling` into `reference/scrapling` and reading the source
directly rather than assuming API shape from memory:

- `Spider`, `CrawlSpider`, `CrawlRule`, and `LinkExtractor` all live under
  `scrapling.spiders` (`from scrapling.spiders import Spider, CrawlSpider, CrawlRule, LinkExtractor, Response`).
- `result.items` is a Scrapling `ItemList` (`scrapling.spiders.result.ItemList`)
  with built-in `.to_csv(path, fields=..., delimiter=...)`, `.to_json()`, `.to_xml()`.
- Fetchers (`Fetcher`, `StealthyFetcher`, `AsyncStealthySession`, etc.) are
  imported from `scrapling.fetchers`, not `scrapling` directly (though
  `scrapling.Fetcher` etc. work too via lazy re-export).

## MCP SDK note (important deviation from the brief)

The brief asked for `pip install mcp` + `FastMCP`. As of this writing,
**`mcp` v2.0.0** (pulled in automatically as a dependency of
`scrapling[all]`) is the latest version on PyPI, and it **renamed
`FastMCP` to `MCPServer`**, moved to `mcp.server.mcpserver`. Confirmed by
inspecting the installed package directly
(`from mcp.server.mcpserver import MCPServer`), not guessed. The
decorator-based `@server.tool()` API and `server.run(transport="stdio")`
are unchanged in spirit, so `server.py` uses `MCPServer` in place of
`FastMCP`.

## Step 0: Environment setup

All Python work in this project uses a conda environment named `orbyt`
with Python 3.11:

```bash
conda create -n orbyt python=3.11 -y
conda activate orbyt
pip install "scrapling[all]"
scrapling install       # installs Playwright's browser binaries
patchright install chromium   # StealthyFetcher uses patchright, a separate
                               # Playwright fork with its own browser cache --
                               # `scrapling install` alone is NOT enough.
pip install mcp
```

To recreate the environment elsewhere:

```bash
conda env create -f environment.yml
conda activate orbyt
scrapling install
patchright install chromium
```

Then install this package (editable, for development):

```bash
cd leadorbyt
pip install -e . --no-deps
```

## Project structure

```
leadorbyt/
  leadorbyt/
    __init__.py
    discovery.py      # GoogleMapsDiscoverySpider (CrawlSpider)
    enrich.py          # enrich_website(): Fetcher -> StealthyFetcher escalation
    extractors.py       # email/phone/social/contact-page regex+heuristics
    merge.py           # joins discovery + enrichment, exports CSV
    server.py          # MCP server: find_leads, enrich_url tools
    config.py          # env-var-overridable settings
  environment.yml
  pyproject.toml
  README.md
  test_run.py
```

## Step 8: Test it

```bash
conda activate orbyt
python test_run.py
```

This calls `find_leads("coffee shops", "Austin, TX", 5)` directly (no MCP
transport) and prints the resulting CSV path and contents. Verified working
end-to-end during development -- a real 5-row CSV of Austin coffee shops
with emails, phones, addresses, and Instagram/Facebook links populated from
their live websites.

Note on speed: Google Maps discovery renders pages with a real (stealth)
browser and Scrapling's AutoThrottle backs off aggressively based on
observed latency, so a 5-result discovery run can take 1-2 minutes. This is
expected, not a bug -- it's the cost of politely scraping a JS-heavy,
anti-bot-protected search interface.

## Step 9: Wire-up into Claude Desktop / other MCP clients

`server.py` is a multi-tenant server (`store.py`/`jobs.py`/`browser_pool.py`
back a persistent SQLite cache + bounded job queue + pooled browser
sessions, see `server.py`'s module docstring), so it speaks **streamable
HTTP with per-request API-key auth** (`auth.py`), not plain stdio -- it is
started as a long-lived process, not spawned per-client.

**1. Create a tenant + API key** (shown once, save it):

```bash
cd leadorbyt   # project root, so the default `leadorbyt.db` / `leads_output` land here
conda run -n orbyt python -m leadorbyt.admin create-user "<your name>"
# user_id: ...
# api_key: <RAW_KEY>
```

**2. Start the server** (stays running; defaults to `127.0.0.1:8000`, override
with `LEADORBYT_HOST`/`LEADORBYT_PORT`):

```bash
conda run -n orbyt python -m leadorbyt.server
```

**3. Point Claude Desktop at it.** Since Claude Desktop's `mcpServers` config
only spawns stdio subprocesses, bridge to the HTTP server with `mcp-remote`
(same pattern as any other locally-running streamable-HTTP MCP server):

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

Restart Claude Desktop after editing the config. This same `mcp-remote`
bridge works for any other MCP client that reads this config format, and for
a remote (non-`127.0.0.1`) deployment of the server -- just swap the URL.

Revoke a leaked/rotated key with `leadorbyt-admin revoke-key <RAW_KEY>`, and
list tenants with `leadorbyt-admin list-users`.

## Configuration (`config.py` / env vars)

| Env var | Default | Purpose |
|---|---|---|
| `LEADORBYT_USER_AGENT` | Chrome-124 UA string | UA used by both fetchers |
| `LEADORBYT_DOWNLOAD_DELAY` | `1.0` | Base delay between requests (seconds) |
| `LEADORBYT_MAX_CONCURRENCY` | `8` | Global concurrency cap (discovery + enrichment) |
| `LEADORBYT_ROBOTS_TXT_OBEY` | `true` | Respect robots.txt (set `false` to disable) |
| `LEADORBYT_REQUEST_TIMEOUT` | `20` | Per-request timeout (seconds) |
| `LEADORBYT_AUTOTHROTTLE_ENABLED` | `true` | Adaptive delay for discovery |
| `LEADORBYT_AUTOTHROTTLE_START_DELAY` | `3.0` | Starting delay per domain |
| `LEADORBYT_AUTOTHROTTLE_MAX_DELAY` | `30.0` | Ceiling for adaptive delay |
| `LEADORBYT_OUTPUT_DIR` | `./leads_output` | Where CSVs are written |
| `LEADORBYT_LOG_LEVEL` | `INFO` | Logging verbosity |
