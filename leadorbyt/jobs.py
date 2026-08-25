"""In-process job queue + worker pool for discovery searches.

A `find_leads`/`submit_search` call used to run discovery + enrichment
inline, so every concurrent call raced to launch its own browser work with
no shared cap. This module decouples "ask for a search" from "run a
search": callers enqueue a job and either await its completion (`find_leads`)
or poll for it (`submit_search`/`get_search_status`), while a small, fixed
pool of worker coroutines pulls from the queue and does the actual
discovery -> enrichment -> merge -> export pipeline, bounded by
config.SEARCH_WORKERS (which matches config.DISCOVERY_POOL_SIZE, since each
in-flight search holds one pooled browser session).

Jobs are mirrored into store.py's `search_jobs` table purely for
observability/polling; the live queue itself is an in-memory asyncio.Queue
(jobs don't need to survive a process restart -- the persistent *caches* do).
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from . import config, discovery, qualify, store
from .enrich import enrich_website
from .merge import export_csv, merge_records, _normalize
from .sources import enrich_extras

logger = logging.getLogger("leadorbyt.jobs")


@dataclass
class SearchJob:
    id: str
    user_id: str
    niche: str
    location: str
    max_results: int
    done: asyncio.Event = field(default_factory=asyncio.Event)
    status: str = "queued"
    result_path: str | None = None
    error: str | None = None


_queue: asyncio.Queue[SearchJob] = asyncio.Queue()
_jobs: dict[str, SearchJob] = {}
_workers_started = False


async def _enrich_all(websites: list[str]) -> dict[str, dict]:
    """Enrich each unique website concurrently (bounded), using the persistent cache."""
    sem = asyncio.Semaphore(config.MAX_CONCURRENCY)
    results: dict[str, dict] = {}

    async def _one(url: str):
        key = _normalize(url)
        cached = await asyncio.to_thread(store.get_enrichment, key)
        if cached is not None:
            logger.info(f"Cache hit for enrichment of {url}")
            results[key] = cached
            return
        async with sem:
            logger.info(f"Enriching {url}")
            data = await enrich_website(url)
            await asyncio.to_thread(store.put_enrichment, key, data)
            results[key] = data

    unique = [u for u in dict.fromkeys(w for w in websites if w)]
    await asyncio.gather(*(_one(url) for url in unique))
    return results


async def _enrich_extras_all(discovery_items: list[dict], location: str) -> dict[str, dict]:
    """Query the third-party sources (sources/registry.py) for each discovered business.

    Gated by `qualify.should_enrich_extras` (skips businesses that were never
    going to be worth ~20 paid lookups -- no website, an excluded category)
    and, for the ones that pass, cached by domain in store.py's
    `extras_cache` table so the same business surfacing across two searches
    doesn't re-bill every provider.
    """
    sem = asyncio.Semaphore(config.MAX_CONCURRENCY)
    results: dict[str, dict] = {}

    # Dedup by domain before spawning tasks (not inside them) so two
    # concurrent tasks for the same domain can't race past an "already have
    # this key" check that neither has written yet.
    by_key = {}
    skipped = 0
    for item in discovery_items:
        if not qualify.should_enrich_extras(item):
            skipped += 1
            continue
        key = _normalize(item.get("website", ""))
        if key and key not in by_key:
            by_key[key] = item
    if skipped:
        logger.info(f"Qualify gate skipped {skipped} of {len(discovery_items)} discovered businesses")

    async def _one(key: str, item: dict):
        cached = await asyncio.to_thread(store.get_extras, key)
        if cached is not None:
            logger.info(f"Cache hit for extras of {item.get('business_name', key)!r}")
            results[key] = cached
            return
        async with sem:
            logger.info(f"Fetching extra data sources for {item.get('business_name', key)!r}")
            data = await enrich_extras(
                business_name=item.get("business_name", ""),
                website=item.get("website", ""),
                domain=key,
                location=location,
                lat=item.get("lat"),
                lon=item.get("lon"),
            )
            await asyncio.to_thread(store.put_extras, key, data)
            results[key] = data

    await asyncio.gather(*(_one(key, item) for key, item in by_key.items()))
    return results


async def _run_search(job: SearchJob) -> None:
    job.status = "running"
    await asyncio.to_thread(store.start_job, job.id)
    logger.info(f"Discovering '{job.niche}' in '{job.location}' (max {job.max_results})")
    discovery_items = await discovery.discover(job.niche, job.location, job.max_results)
    logger.info(f"Discovered {len(discovery_items)} businesses for job {job.id}")

    websites = [item.get("website", "") for item in discovery_items]
    enrichment_by_url = await _enrich_all(websites)
    extras_by_url = await _enrich_extras_all(discovery_items, job.location)

    merged = merge_records(discovery_items, enrichment_by_url, extras_by_url)

    user_output_dir = config.OUTPUT_DIR / job.user_id
    user_output_dir.mkdir(parents=True, exist_ok=True)
    filename = (
        f"{job.niche.strip().replace(' ', '_')}_"
        f"{job.location.strip().replace(' ', '_').replace(',', '')}_{int(time.time())}.csv"
    )
    out_path = export_csv(merged, user_output_dir / filename)
    logger.info(f"Exported {len(merged)} leads to {out_path} for job {job.id}")

    result_path = str(out_path.resolve())
    niche_key = job.niche.strip().lower()
    location_key = job.location.strip().lower()
    await asyncio.to_thread(store.put_search, job.user_id, niche_key, location_key, job.max_results, result_path)
    job.result_path = result_path


async def _worker(worker_id: int) -> None:
    logger.info(f"Search worker {worker_id} started")
    while True:
        job = await _queue.get()
        try:
            await _run_search(job)
            await asyncio.to_thread(store.finish_job, job.id, job.result_path)
            job.status = "done"
        except Exception as exc:
            logger.exception(f"Job {job.id} failed")
            job.error = str(exc)
            job.status = "error"
            await asyncio.to_thread(store.fail_job, job.id, str(exc))
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
    logger.info(f"Started {n} search workers")


async def submit(user_id: str, niche: str, location: str, max_results: int) -> str:
    """Enqueue a search job and return its id immediately (does not wait)."""
    start_workers()
    job_id = uuid.uuid4().hex
    job = SearchJob(id=job_id, user_id=user_id, niche=niche, location=location, max_results=max_results)
    _jobs[job_id] = job
    await asyncio.to_thread(store.create_job, job_id, user_id, niche, location, max_results)
    await _queue.put(job)
    return job_id


async def submit_cached(user_id: str, niche: str, location: str, max_results: int, result_path: str) -> str:
    """Register an already-complete result as a job so callers can poll it uniformly."""
    job_id = uuid.uuid4().hex
    job = SearchJob(
        id=job_id,
        user_id=user_id,
        niche=niche,
        location=location,
        max_results=max_results,
        status="done",
        result_path=result_path,
    )
    job.done.set()
    _jobs[job_id] = job
    await asyncio.to_thread(store.create_job, job_id, user_id, niche, location, max_results)
    await asyncio.to_thread(store.finish_job, job_id, result_path)
    return job_id


def status(job_id: str, user_id: str) -> dict | None:
    """In-process status if the job object is still held; falls back to the DB.

    Scoped by user_id so a guessed/leaked job id from another tenant can't be polled.
    """
    job = _jobs.get(job_id)
    if job is not None:
        if job.user_id != user_id:
            return None
        return {"status": job.status, "result_path": job.result_path, "error": job.error}
    return store.get_job(job_id, user_id)


async def wait_for(job_id: str) -> dict:
    """Block until `job_id` finishes, then return its final status dict."""
    job = _jobs[job_id]
    await job.done.wait()
    if job.error:
        return {"status": "error", "result_path": None, "error": job.error}
    return {"status": "done", "result_path": job.result_path, "error": None}
