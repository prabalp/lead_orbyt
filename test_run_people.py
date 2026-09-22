"""Manual smoke test for BetterContact person search, run directly (no MCP
transport, and does NOT go through job queue/CSV export -- calls
bettercontact.search_people directly so you see exactly what got sent and
what came back for each case). Requires BETTERCONTACT_API_KEY set.

    conda activate orbyt
    BETTERCONTACT_API_KEY=... python test_run_people.py

Cases below mix known-good and known-bad-before-the-fix inputs so you can
confirm the location/error-visibility fix against the live account:
- "United States" and "Austin, TX" were always fine (TX already an example
  in the docstrings) -- included as a control.
- "San Francisco, CA" / "Miami, FL" / "Florida" / "California" all returned
  ZERO results before the fix (state abbreviation, or a bare state name
  with no second component); should now return real results.
- headcount_max=1000 is included to show it was never actually broken --
  compare the returned companies' headcount range against the cap.
- industries=["SaaS"] is included to show it still returns 0 (not a bug we
  can fix -- "SaaS" isn't a BetterContact taxonomy value at all) alongside
  industries=["Marketing & Advertising"] (a real taxonomy value straight
  from BetterContact's own docs) to show even a documented-correct value
  commonly matches 0 real leads -- the taxonomy/data mismatch described in
  bettercontact.py's module docstring, not something client-side code can
  fix.
"""

import asyncio

from leadorbyt.errors import LeadOrbytError
from leadorbyt.sources import bettercontact


async def run_case(label: str, **kwargs) -> None:
    print(f"\n=== {label} ===")
    print("CALLING search_people with:", kwargs)
    try:
        people = await bettercontact.search_people(**kwargs)
    except LeadOrbytError as exc:
        print(f"RAISED (this is now visible instead of a silent []): {exc}")
        return
    print(f"RESULT: {len(people)} people")
    for p in people[:3]:
        print("   ", p.get("full_name"), "|", p.get("company_name"), "|", p.get("title"))
    # Observed live: BetterContact's account intermittently returns a
    # legitimately-shaped but empty `leads: []` response (not an error --
    # our fix can't distinguish this from a real zero-match search) when
    # several searches fire back-to-back. Spacing calls out clears it up in
    # testing; if a case below still comes back 0, try it again alone.
    await asyncio.sleep(3)


async def main():
    if not bettercontact.enabled():
        print("BETTERCONTACT_API_KEY is not set -- nothing to test.")
        return

    await run_case("control: United States (always worked)", job_titles=["VP Marketing"], location="United States", max_results=5)
    await run_case("control: Austin, TX (existing docstring example)", job_titles=["VP Marketing"], location="Austin, TX", max_results=5)

    await run_case("was broken: San Francisco, CA (state abbreviation)", job_titles=["VP Marketing"], location="San Francisco, CA", max_results=5)
    await run_case("was broken: Miami, FL (state abbreviation)", job_titles=["VP Marketing"], location="Miami, FL", max_results=5)
    await run_case("was broken: Florida (bare state name)", job_titles=["VP Marketing"], location="Florida", max_results=5)
    await run_case("was broken: California (bare state name)", job_titles=["VP Marketing"], location="California", max_results=5)

    await run_case(
        "headcount_max=1000 (was never actually broken -- check the printed companies' real size)",
        job_titles=["VP Marketing"], location="", max_results=5, headcount_max=1000,
    )

    await run_case(
        "industries=['SaaS'] (still 0 -- 'SaaS' is not a BetterContact taxonomy value; not fixable client-side)",
        job_titles=["VP Marketing"], location="", max_results=5, industries=["SaaS"],
    )
    await run_case(
        "industries=['Marketing & Advertising'] (a REAL taxonomy value from BetterContact's own docs -- "
        "still likely 0, because live data doesn't use these taxonomy strings; see bettercontact.py docstring)",
        job_titles=["VP Marketing"], location="", max_results=5, industries=["Marketing & Advertising"],
    )

    await run_case(
        "technologies=['HubSpot'] (capitalized -- now lowercased automatically before sending)",
        job_titles=["Marketing Manager"], location="", max_results=5, technologies=["HubSpot"],
    )


if __name__ == "__main__":
    asyncio.run(main())
