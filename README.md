# leadorbyt

An MCP server that finds **businesses** (niche + location), **web/social intent posts**, or **named people** (job title + location) and writes contact CSVs. It does not send email.

Architecture, pipelines, tool contracts, SQLite, and config live in **[docs/](docs/architecture.md)**. The rest of this README is how to run it.

## Independent discovery tools

Call the source the user asked for first. If they want more than one, call the matching tools **one after another**. They are not bundled.

1. **Google Maps businesses:** `find_leads_maps` researches a Maps list (name, category, website, phone, address, plus code, maps URL, coordinates, and any contact Maps already shows). After review and explicit approval, `enrich_lead_list` visits websites and calls configured extras.
2. **Web/social intent:** `find_web_signals` finds public posts already asking for that thing (LinkedIn, Reddit, X, Facebook) via DuckDuckGo HTML `site:` search. Optional `sites` subset. Does not run as part of `find_leads_maps`.
3. **Person leads:** `find_people_leads` (BetterContact or Apollo) → optional offline ICP gate → optional paid email reveal. Paid lookups default to zero.

Claude is instructed by the MCP tool contracts to show each list and ask before enrichment or another source. A bare “find leads” request cannot trigger business enrichment, web search, or paid person lookups.

Clients talk **streamable HTTP** with `Authorization: Bearer <api_key>`, not stdio. See [docs/mcp-tools.md](docs/mcp-tools.md).

## Environment

Conda env `orbyt`, Python 3.11:

```bash
conda env create -f environment.yml
conda activate orbyt
scrapling install
patchright install chromium   # StealthyFetcher uses patchright; scrapling install is not enough
cd leadorbyt   # this directory (package root)
pip install -e . --no-deps
```

Copy `.env.example` to `.env` next to wherever you run the server (same cwd as `leadorbyt.db`). Blank keys are skipped; Maps discovery still runs.

## Run

## Hosted signup

The server serves a public landing page at `/` that explains the Maps, web, and people tools, then a **Get MCP access** form. A visitor enters an email, receives a verification link (Resend when `RESEND_API_KEY` is set; otherwise the link is logged), and after confirming gets:

- the MCP URL (`{LEADORBYT_PUBLIC_URL}/mcp`)
- a one-time API key
- a Claude Desktop `mcpServers` snippet using `mcp-remote`

Opening the link does not mint the key. The page asks for an explicit confirm click, so email scanners cannot burn the token. Set `LEADORBYT_PUBLIC_URL` to the HTTPS origin you publish (no trailing slash). MCP tools at `/mcp` still require the Bearer key. Disable the form with `LEADORBYT_SIGNUP_ENABLED=false` if you only want `leadorbyt-admin`.

```bash
# from this directory so leadorbyt.db and leads_output land here
python -m leadorbyt.server                  # default 0.0.0.0:8000. Open http://127.0.0.1:8000
# optional operator path:
leadorbyt-admin create-user "<your name>"
```

Claude Desktop (stdio-only) needs `mcp-remote`:

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

Docker: `docker-compose.yml` maps `127.0.0.1:8010` → `:8000` and stores DB/CSVs in a volume. It does not load `.env` unless you add `env_file`.

## Tests

```bash
conda run -n orbyt python -m pytest tests -q
```

Unit/mocked only. Live Maps smoke: `python test_run.py` (minutes, needs browsers). Details: [docs/testing.md](docs/testing.md).

## Layout

```
leadorbyt/                 # installable package
  server.py                # MCP tools + uvicorn
  jobs.py / people_jobs.py # in-memory worker queues
  discovery.py / enrich.py # Maps + websites
  sources/                 # optional REST extras + person search
  store.py / auth.py / admin.py
  qualify.py / qualify_ml.py
docs/                      # architecture
```

Google Maps selectors are empirical (`div[role="feed"]`, `a.hfpxzc`, `data-item-id`). If discovery goes empty, start in `discovery.py`.
