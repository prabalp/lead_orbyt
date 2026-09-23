"""In-process job queue for person-lead discovery searches (see jobs.py for the
business-lead equivalent).

Deliberately a separate queue rather than folding into jobs.py's SearchJob/
_run_search: this pipeline is pure REST (provider search + reveal, see
sources/person_search.py), with no browser/backoff step at all, and keeping
it separate avoids touching the already-shipped, tested business-lead job path.

The reveal step is the one part of this pipeline that spends real money (a
provider's email reveal -- 1 credit per verified hit, a miss is free, for
both Apollo and BetterContact). The two providers' paid step has a different
shape though: Apollo reveals one person per call, so its budget check is a
sequential loop that stops the instant `max_paid_lookups` is hit; BetterContact
reveals a whole batch in one call, so its budget is enforced by capping how
many candidates go *into* that one batch before it's ever sent. Both paths
land on the same guarantee: at most `max_paid_lookups` reveal attempts.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from scrapling.spiders.result import ItemList

from . import config, query_expansion, qualify_ml, store
from .errors import ErrorType, LeadOrbytError
from .people_merge import company_domain_key, dedup_key, export_people_csv
from .sources import apollo_people, bettercontact, person_search

logger = logging.getLogger("leadorbyt.people_jobs")


@dataclass
class PersonSearchJob:
    id: str
    user_id: str
    job_titles: list[str]
    location: str
    max_results: int
    max_paid_lookups: int
    icp: str = ""
    goal_new_leads: int | None = None
    filters: dict = field(default_factory=dict)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    status: str = "queued"
    result_path: str | None = None
    error: str | None = None
    error_type: str | None = None
    stage: str = "queued"
    people_found: int = 0
    paid_lookups_used: int = 0


_queue: asyncio.Queue[PersonSearchJob] = asyncio.Queue()
_jobs: dict[str, PersonSearchJob] = {}
_workers_started = False


def _contact_cache_key(person: dict) -> str:
    """Same identity used to cache a reveal result, whichever provider found this person."""
    return person.get("apollo_person_id") or person.get("linkedin_url", "")


async def _qualify_batch(people: list[dict], job: PersonSearchJob, icp_hash: str) -> None:
    """Qualify `people` against job.icp, reusing any lead_states verdict already
    on record for this exact (user, ICP) pair instead of re-asking the gate/LLM
    for someone already known QUALIFIED/REJECTED/EMAIL_FOUND from a prior run.
    """
    to_qualify: list[dict] = []
    for person in people:
        state = await asyncio.to_thread(store.get_lead_state, job.user_id, icp_hash, dedup_key(person))
        if state == "REJECTED":
            person["qualified"] = False
            person["qualification_score"] = 0.0
            person["qualification_reason"] = "Previously rejected for this ICP"
        elif state == "EMAIL_FOUND":
            person["qualified"] = True
            person["qualification_score"] = 1.0
            person["qualification_reason"] = "Previously qualified and resolved for this ICP"
            cached = await asyncio.to_thread(store.get_person_contact, _contact_cache_key(person))
            if cached:
                person["email"] = cached.get("email", "")
        elif state == "QUALIFIED":
            person["qualified"] = True
            person["qualification_score"] = 1.0
            person["qualification_reason"] = "Previously qualified for this ICP; reveal not yet attempted"
        else:
            to_qualify.append(person)

    if not to_qualify:
        return

    results = await qualify_ml.qualify_pool(to_qualify, job.icp, job.user_id)
    for person, result in zip(to_qualify, results):
        person["qualified"] = result.qualified
        person["qualification_score"] = result.score
        person["qualification_reason"] = result.reason
        if result.source != "agent_pending":  # not a real verdict yet -- nothing to remember
            await asyncio.to_thread(query_expansion.record_outcome, job.user_id, icp_hash, person, result.qualified)
            await asyncio.to_thread(
                store.set_lead_state,
                job.user_id,
                icp_hash,
                dedup_key(person),
                "QUALIFIED" if result.qualified else "REJECTED",
            )


async def _reveal_via_apollo(people: list[dict], job: PersonSearchJob, icp_hash: str) -> None:
    """Sequentially reveal emails up to job.max_paid_lookups, cache-first.

    Sequential on purpose: paid_lookups_used must be checked and bumped one
    attempt at a time so the cap is respected exactly, not raced past by
    concurrent tasks that all read the counter before any of them update it.
    """
    for person in people:
        if job.paid_lookups_used >= job.max_paid_lookups:
            break
        if not person.get("has_email"):
            continue

        key = dedup_key(person)
        if await asyncio.to_thread(store.get_lead_state, job.user_id, icp_hash, key) == "NO_EMAIL_FOUND":
            continue  # already missed for this exact ICP context -- don't retry

        person_id = person.get("apollo_person_id", "")
        cached = await asyncio.to_thread(store.get_person_contact, person_id)
        if cached is not None:
            person["email"] = cached.get("email", "")
            if cached.get("last_name"):
                person["last_name_obfuscated"] = cached["last_name"]
            continue

        result = await apollo_people.reveal_email(person_id)
        job.paid_lookups_used += 1
        await asyncio.to_thread(store.update_people_job_progress, job.id, paid_lookups_used=job.paid_lookups_used)

        data = result or {"email": ""}
        await asyncio.to_thread(store.put_person_contact, person_id, data)
        person["email"] = data.get("email", "")
        if data.get("last_name"):
            person["last_name_obfuscated"] = data["last_name"]
        await asyncio.to_thread(
            store.set_lead_state, job.user_id, icp_hash, key, "EMAIL_FOUND" if data.get("email") else "NO_EMAIL_FOUND"
        )


async def _reveal_via_bettercontact(people: list[dict], job: PersonSearchJob, icp_hash: str) -> None:
    """Batch-reveal up to job.max_paid_lookups people in ONE paid request.

    Candidates are first split into cache hits (free, resolved immediately)
    and cache misses; only cache misses count against the cap, and exactly
    that many (never more) are included in the single batch request sent --
    the budget is enforced by what goes *into* the request, since there's no
    way to stop a batch call partway through once it's sent.
    """
    to_reveal: list[dict] = []
    for person in people:
        if job.paid_lookups_used + len(to_reveal) >= job.max_paid_lookups:
            break
        linkedin_url = person.get("linkedin_url", "")
        if not linkedin_url:
            continue

        key = dedup_key(person)
        if await asyncio.to_thread(store.get_lead_state, job.user_id, icp_hash, key) == "NO_EMAIL_FOUND":
            continue

        cached = await asyncio.to_thread(store.get_person_contact, linkedin_url)
        if cached is not None:
            person["email"] = cached.get("email", "")
            continue
        to_reveal.append(person)

    if not to_reveal:
        return

    job.paid_lookups_used += len(to_reveal)
    await asyncio.to_thread(store.update_people_job_progress, job.id, paid_lookups_used=job.paid_lookups_used)

    results = await bettercontact.reveal_emails([p["linkedin_url"] for p in to_reveal])
    for person in to_reveal:
        data = results.get(person["linkedin_url"]) or {"email": ""}
        await asyncio.to_thread(store.put_person_contact, person["linkedin_url"], data)
        person["email"] = data.get("email", "")
        if data.get("full_name"):
            person["full_name"] = data["full_name"]
        await asyncio.to_thread(
            store.set_lead_state,
            job.user_id,
            icp_hash,
            dedup_key(person),
            "EMAIL_FOUND" if data.get("email") else "NO_EMAIL_FOUND",
        )


async def _reveal_emails(people: list[dict], job: PersonSearchJob, icp_hash: str) -> None:
    """Dispatch to the provider each person was searched through (person_search.py
    tags every result with `source_provider`, and a search only ever uses one
    active provider at a time, so this is never a mixed list).
    """
    if not people:
        return
    provider = people[0].get("source_provider")
    if provider == "bettercontact":
        await _reveal_via_bettercontact(people, job, icp_hash)
    elif provider == "apollo":
        await _reveal_via_apollo(people, job, icp_hash)


async def _search_and_process_round(
    round_titles: list[str],
    job: PersonSearchJob,
    icp_hash: str,
    seen_keys: set[str],
    extra_filters: dict | None = None,
) -> list[dict]:
    """One round: search, dedupe against everything seen so far (this job and
    prior jobs, via `people_leads` -- `seen_keys` is pre-seeded from it at
    job start, see `_run_people_search`), tag, and qualify. Returns only the
    newly-seen-in-this-job people from this round (already qualified if
    job.icp is set).

    `extra_filters` overrides/adds to `job.filters` for this round only
    (job.filters itself is untouched) -- used by the auto-volume-expansion
    phase in `_run_people_search` to vary seniority/headcount per round.
    """
    filters = {**job.filters, **extra_filters} if extra_filters else job.filters
    logger.info(f"Searching people {round_titles!r} in '{job.location}' (max {job.max_results}) filters={filters!r}")
    batch = await person_search.search_people(round_titles, job.location, job.max_results, **filters)
    discovered_by_query = f"{round_titles} | {job.location}" + (f" | {extra_filters}" if extra_filters else "")

    new_people = []
    for person in batch:
        key = dedup_key(person)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        # BetterContact already gives a real full_name; Apollo doesn't (its
        # free tier only gives first_name + an obfuscated last name), so only
        # derive one when the provider didn't already supply an authoritative one.
        if not person.get("full_name"):
            person["full_name"] = f"{person.get('first_name', '')} {person.get('last_name_obfuscated', '')}".strip()
        person["discovered_by_query"] = discovered_by_query
        person["email"] = ""
        person["qualified"] = ""
        person["qualification_score"] = ""
        person["qualification_reason"] = ""
        new_people.append(person)

    for person in new_people:
        is_new = await asyncio.to_thread(
            store.upsert_person_lead,
            job.user_id,
            dedup_key(person),
            person.get("full_name", ""),
            company_domain_key(person),
            str(job.job_titles),
            job.location,
            discovered_by_query,
        )
        person["is_new_lead"] = is_new

    if job.icp:
        job.stage = "qualifying"
        await asyncio.to_thread(store.update_people_job_progress, job.id, stage=job.stage)
        await _qualify_batch(new_people, job, icp_hash)

    return new_people


# BetterContact's lead_finder caps at ~100 leads/request AND is fully
# deterministic per filter set -- the identical request returns the
# identical ~100 people every time (confirmed live). So reaching a large
# max_results needs genuinely different filters per round, not more of the
# same. Seniority is tried first: a garbage value reliably returns 0 (a
# real, applied filter) and "vp" vs "director" returned zero overlapping
# people in testing, unlike headcount bands which can straddle real
# organizations less cleanly. Values are BetterContact's documented
# lead_seniority taxonomy (confirmed real, unlike company_industry's
# taxonomy -- see bettercontact.py's module docstring).
_AUTO_SENIORITY_BANDS = [
    "vp", "director", "c_suite", "head", "manager",
    "senior", "mid-level", "founder", "owner", "partner", "entry", "intern",
]
_AUTO_HEADCOUNT_BANDS = [
    (1, 10), (11, 50), (51, 200), (201, 500),
    (501, 1000), (1001, 5000), (5001, 10000), (10001, None),
]


def _auto_variation_rounds(job_filters: dict) -> list[dict]:
    """Extra per-round filter overlays to reach max_results once the initial
    (+ any goal_new_leads title-expansion) rounds aren't enough on their
    own. Only varies a dimension the caller didn't already explicitly
    constrain -- an explicit ask (e.g. a specific seniority) is never
    silently overridden, and if the caller already constrained BOTH
    dimensions this can vary, it returns [] (nothing left to try
    automatically; see find_people_leads's docstring for what to do next)."""
    if not job_filters.get("seniorities"):
        return [{"seniorities": [s]} for s in _AUTO_SENIORITY_BANDS]
    if job_filters.get("headcount_min") is None and job_filters.get("headcount_max") is None:
        return [
            {"headcount_min": lo, **({"headcount_max": hi} if hi is not None else {})}
            for lo, hi in _AUTO_HEADCOUNT_BANDS
        ]
    return []


async def _run_people_search(job: PersonSearchJob) -> None:
    job.status = "running"
    job.stage = "searching"
    await asyncio.to_thread(store.start_people_job, job.id)
    await asyncio.to_thread(store.update_people_job_progress, job.id, stage=job.stage)

    icp_hash = store.icp_hash(job.icp)  # scopes lead_states even with icp="" (empty-ICP context)
    # Pre-seeded from every prior job's results for this user (not just this
    # job's own rounds) -- so a person already surfaced to this tenant is
    # excluded from a fresh export outright, matching what find_people_leads's
    # docstring already promises ("seen leads are remembered"), rather than
    # only being tagged is_new_lead=False while still duplicating the row.
    seen_keys: set[str] = await asyncio.to_thread(store.get_seen_person_dedup_keys, job.user_id)
    people: list[dict] = []
    total_new_qualified = 0
    round_titles = list(job.job_titles)
    rounds_budget = config.PEOPLE_SEARCH_MAX_ROUNDS
    # Expansion only ever runs when a goal was explicitly requested -- with
    # goal_new_leads=None this is exactly one round, identical to pre-expansion behavior.
    max_rounds = min(config.QUERY_EXPANSION_MAX_ROUNDS, rounds_budget - 1) if job.goal_new_leads else 0

    round_num = 0
    for round_num in range(max_rounds + 1):
        new_people = await _search_and_process_round(round_titles, job, icp_hash, seen_keys)
        people.extend(new_people)

        job.people_found = len(people)
        await asyncio.to_thread(store.update_people_job_progress, job.id, people_found=job.people_found)

        new_qualified = sum(1 for p in new_people if p["is_new_lead"] and (not job.icp or p["qualified"] is True))
        total_new_qualified += new_qualified

        if not job.goal_new_leads or total_new_qualified >= job.goal_new_leads or round_num == max_rounds:
            break
        expansion_titles = query_expansion.sample_next_titles(
            job.user_id, icp_hash, round_titles, config.QUERY_EXPANSION_TOKENS_PER_ROUND
        )
        if not expansion_titles:
            logger.info(f"Job {job.id}: no more frontier tokens to expand into, stopping early")
            break
        round_titles = round_titles + expansion_titles
        logger.info(f"Job {job.id}: expanding search with {expansion_titles!r} (round {round_num + 1})")

    # Auto-volume expansion: if max_results still isn't reached, keep going
    # with rounds that vary seniority (then headcount) instead of job
    # titles -- see _auto_variation_rounds. This is what lets a caller just
    # ask for max_results=1000 without crafting separate calls themselves.
    rounds_used = round_num + 1
    for extra_filters in _auto_variation_rounds(job.filters):
        if len(people) >= job.max_results or rounds_used >= rounds_budget:
            break
        rounds_used += 1
        new_people = await _search_and_process_round(job.job_titles, job, icp_hash, seen_keys, extra_filters)
        people.extend(new_people)
        job.people_found = len(people)
        await asyncio.to_thread(store.update_people_job_progress, job.id, people_found=job.people_found)

    logger.info(f"Found {len(people)} people for job {job.id} in {rounds_used} round(s)")

    ranked = people
    if job.icp:
        ranked = sorted(people, key=lambda p: (p["qualified"] is not True, -(p["qualification_score"] or 0)))

    job.stage = "revealing"
    await asyncio.to_thread(store.update_people_job_progress, job.id, stage=job.stage)
    await _reveal_emails(ranked, job, icp_hash)

    # Apollo's reveal can upgrade last_name_obfuscated to the real last name
    # (see _reveal_via_apollo) -- recompute full_name from it. BetterContact
    # already supplies (and, on reveal, re-confirms) an authoritative
    # full_name directly, so leave those rows alone.
    for person in people:
        if person.get("source_provider") == "apollo":
            person["full_name"] = f"{person.get('first_name', '')} {person.get('last_name_obfuscated', '')}".strip()

    job.stage = "exporting"
    await asyncio.to_thread(store.update_people_job_progress, job.id, stage=job.stage)

    user_output_dir = config.OUTPUT_DIR / job.user_id
    user_output_dir.mkdir(parents=True, exist_ok=True)
    filename = (
        f"people_{'-'.join(job.job_titles).replace(' ', '_')}_"
        f"{job.location.strip().replace(' ', '_').replace(',', '')}_{int(time.time())}.csv"
    )
    out_path = export_people_csv(ItemList(people), user_output_dir / filename)
    logger.info(f"Exported {len(people)} people to {out_path} for job {job.id}")

    job.result_path = str(out_path.resolve())


async def _worker(worker_id: int) -> None:
    logger.info(f"People search worker {worker_id} started")
    while True:
        job = await _queue.get()
        try:
            await _run_people_search(job)
            await asyncio.to_thread(store.finish_people_job, job.id, job.result_path)
            job.status = "done"
        except LeadOrbytError as exc:
            logger.exception(f"People job {job.id} failed")
            job.error = exc.message
            job.error_type = exc.type.value
            job.status = "error"
            await asyncio.to_thread(store.fail_people_job, job.id, exc.message, exc.type.value)
        except Exception as exc:
            logger.exception(f"People job {job.id} failed")
            job.error = str(exc)
            job.error_type = ErrorType.INTERNAL.value
            job.status = "error"
            await asyncio.to_thread(store.fail_people_job, job.id, str(exc), ErrorType.INTERNAL.value)
        finally:
            job.done.set()
            _queue.task_done()


def start_workers(n: int | None = None) -> None:
    global _workers_started
    if _workers_started:
        return
    n = n or config.SEARCH_WORKERS
    for i in range(n):
        asyncio.create_task(_worker(i))
    _workers_started = True
    logger.info(f"Started {n} people search workers")


async def submit(
    user_id: str,
    job_titles: list[str],
    location: str,
    max_results: int,
    max_paid_lookups: int,
    icp: str = "",
    goal_new_leads: int | None = None,
    filters: dict | None = None,
) -> str:
    """Enqueue a person-lead search job and return its id immediately (does not wait)."""
    start_workers()
    job_id = uuid.uuid4().hex
    job = PersonSearchJob(
        id=job_id,
        user_id=user_id,
        job_titles=job_titles,
        location=location,
        max_results=max_results,
        max_paid_lookups=max_paid_lookups,
        icp=icp,
        goal_new_leads=goal_new_leads,
        filters=filters or {},
    )
    _jobs[job_id] = job
    await asyncio.to_thread(
        store.create_people_job, job_id, user_id, str(job_titles), location, icp, max_results, max_paid_lookups
    )
    await _queue.put(job)
    return job_id


def status(job_id: str, user_id: str) -> dict | None:
    """In-process status if the job object is still held; falls back to the DB.

    Scoped by user_id so a guessed/leaked job id from another tenant can't be polled.
    """
    job = _jobs.get(job_id)
    if job is not None:
        if job.user_id != user_id:
            return None
        return {
            "status": job.status,
            "result_path": job.result_path,
            "error": job.error,
            "error_type": job.error_type,
            "stage": job.stage,
            "people_found": job.people_found,
            "paid_lookups_used": job.paid_lookups_used,
        }
    return store.get_people_job(job_id, user_id)


async def wait_for(job_id: str) -> dict:
    """Block until `job_id` finishes, then return its final status dict."""
    job = _jobs[job_id]
    await job.done.wait()
    if job.error:
        return {"status": "error", "result_path": None, "error": job.error, "error_type": job.error_type}
    return {"status": "done", "result_path": job.result_path, "error": None, "error_type": None}
