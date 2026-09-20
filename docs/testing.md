# Testing and what is actually verified

## Automated suite

From `leadorbyt/` with the `orbyt` conda env:

```bash
conda run -n orbyt python -m pytest tests -q
```

This is **unit / mocked** coverage. It does not hit Google Maps, BetterContact, or Apollo live.

Covered behavior includes:

- Error type contract
- Merge dedup keys (website vs name+address)
- Store job progress and isolated SQLite (`tests/conftest.py`)
- Backoff retries and typed failures
- Qualification embeddings, confidence gate, fail-open `agent_pending`
- Agent verdict ingest
- Person search provider dispatch and filters
- People job reveal caps, lead_states reuse, `goal_new_leads` expansion
- People CSV merge keys
- Discovery-only CSV fields and explicit enrichment of an existing CSV
- Zero paid-lookups default for person tools
- Signup verification: hashed token, GET does not consume, POST mints the key, Resend payload
- Web/social SERP parsing (DuckDuckGo HTML) and Maps jobs that do not call web search
- Official Reddit/X OAuth connect tickets; LinkedIn/Facebook login refused

Last local run while writing these docs: **174 passed**.

## What the suite does not prove

| Area | Why it can still fail in production |
|---|---|
| Google Maps discovery | DOM selectors are empirical; Google can change them |
| Website enrichment quality | Real sites vary; blocked/empty pages escalate to stealth |
| Paid APIs | Keys, quotas, payload shape drift |
| MCP HTTP + auth E2E | No transport test in `tests/` |
| Job survival | Queues are in-memory; restart loses in-flight work |
| Docker image | Compose does not inject `.env` by default |

`test_run.py` at the package root is a **live discovery** helper: it calls `find_leads_maps` without MCP. It needs browsers installed (`scrapling install`, `patchright install chromium`) and will take minutes. It does not enrich.

## Manual smoke (server)

1. `conda activate orbyt && pip install -e . --no-deps` (from `leadorbyt/`)
2. `leadorbyt-admin create-user "dev"`
3. `python -m leadorbyt.server`
4. Point an MCP client at `http://127.0.0.1:8000/mcp` with the Bearer key
5. `find_leads_maps("coffee shops", "Austin, TX", 5)`: expect a researched Maps CSV and `next_action` that does not imply web search already ran
5b. `find_web_signals("speaking engagement", "Austin, TX", 5)`: expect a separate intent CSV; optional after Maps only if asked
6. Repeat the same call: expect cache hit (seconds, not minutes) if `icp` is empty
7. After approval, call `enrich_lead_list(result_path)`: expect a second `_enriched_*.csv`
8. `find_people_leads` with no people-provider keys: expect an empty people CSV, not a crash
