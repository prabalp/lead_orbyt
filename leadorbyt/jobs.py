"""In-process job queue + worker pool for discovery searches.

A `find_leads_maps`/`submit_search` call used to run discovery + enrichment
inline, so every concurrent call raced to launch its own browser work with
no shared cap. This module decouples "ask for a search" from "run a
search": callers enqueue a job and either await its completion (`find_leads_maps`)
or poll for it (`submit_search`/`get_search_status`), while a small, fixed
pool of worker coroutines pulls from the queue and does the actual
discovery -> research-list merge -> export pipeline. Website and third-party
enrichment is a separate, explicitly approved `enrich_lead_list()` operation.
Discovery concurrency is bounded by
config.SEARCH_WORKERS (which matches config.DISCOVERY_POOL_SIZE, since each
in-flight search holds one pooled browser session).

Jobs are mirrored into store.py's `search_jobs` table purely for
observability/polling; the live queue itself is an in-memory asyncio.Queue
(jobs don't need to survive a process restart -- the persistent *caches* do).
"""

import asyncio
import csv
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config, discovery, qualify, qualify_ml, store
from .enrich import enrich_website
from .errors import ErrorType, LeadOrbytError
from .merge import dedup_key, export_csv, merge_records, _normalize
from .sources import enrich_extras

logger = logging.getLogger("leadorbyt.jobs")


@dataclass
class SearchJob:
    id: str
    user_id: str
    niche: str
    location: str
    max_results: int
    icp: str = ""
    done: asyncio.Event = field(default_factory=asyncio.Event)
    status: str = "queued"
    result_path: str | None = None
    error: str | None = None
    error_type: str | None = None
    stage: str = "queued"
    items_discovered: int = 0
    items_enriched: int = 0
    items_total: int | None = None


_queue: asyncio.Queue[SearchJob] = asyncio.Queue()
_jobs: dict[str, SearchJob] = {}
_workers_started = False


async def _enrich_all(websites: list[str], job: SearchJob | None = None) -> dict[str, dict]:
    """Enrich each unique website concurrently (bounded), using the persistent cache."""
    sem = asyncio.Semaphore(config.MAX_CONCURRENCY)
    results: dict[str, dict] = {}

    async def _one(url: str):
        key = _normalize(url)
        cached = await asyncio.to_thread(store.get_enrichment, key)
        if cached is not None:
            logger.info(f"Cache hit for enrichment of {url}")
            results[key] = cached
        else:
            async with sem:
                logger.info(f"Enriching {url}")
                data = await enrich_website(url)
                await asyncio.to_thread(store.put_enrichment, key, data)
                results[key] = data
        if job is not None:
            job.items_enriched += 1
            await asyncio.to_thread(store.update_job_progress, job.id, items_enriched=job.items_enriched)

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


async def _qualify_all(merged, icp: str, user_id: str) -> None:
    """Attach ML qualification columns to each merged row, bounded by MAX_CONCURRENCY."""
    sem = asyncio.Semaphore(config.MAX_CONCURRENCY)

    async def _one(row: dict):
        async with sem:
            result = await qualify_ml.qualify_lead(row, icp, user_id)
        row["qualified"] = result.qualified
        row["qualification_score"] = result.score
        row["qualification_reason"] = result.reason

    await asyncio.gather(*(_one(row) for row in merged))


def _read_discovery_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        for field_name in ("lat", "lon"):
            value = row.get(field_name)
            if value not in (None, ""):
                try:
                    row[field_name] = float(value)
                except ValueError:
                    row[field_name] = None
    return rows


async def enrich_lead_list(source_path: str, user_id: str, icp: str = "") -> str:
    """Enrich an existing discovery CSV without repeating Google Maps discovery."""
    discovery_items = await asyncio.to_thread(_read_discovery_csv, source_path)
    websites = [item.get("website", "") for item in discovery_items]
    enrichment_by_url = await _enrich_all(websites)

    location = ""
    if discovery_items:
        query = discovery_items[0].get("discovered_by_query", "")
        if " | " in query:
            location = query.rsplit(" | ", 1)[-1]
    extras_by_url = await _enrich_extras_all(discovery_items, location)
    merged = merge_records(discovery_items, enrichment_by_url, extras_by_url)

    prior_newness = {dedup_key(row): row.get("is_new_lead", "") for row in discovery_items}
    for row in merged:
        row["is_new_lead"] = prior_newness.get(dedup_key(row), "")

    if icp:
        await _qualify_all(merged, icp, user_id)

    source = Path(source_path)
    out_path = source.with_name(f"{source.stem}_enriched_{int(time.time())}.csv")
    return str(export_csv(merged, out_path).resolve())


async def _run_search(job: SearchJob) -> None:
    job.status = "running"
    job.stage = "discovering"
    await asyncio.to_thread(store.start_job, job.id)
    await asyncio.to_thread(store.update_job_progress, job.id, stage=job.stage)
    logger.info(f"Discovering '{job.niche}' in '{job.location}' (max {job.max_results})")
    discovery_items = await discovery.discover(job.niche, job.location, job.max_results)
    logger.info(f"Discovered {len(discovery_items)} businesses for job {job.id}")

    job.items_discovered = len(discovery_items)
    job.items_total = len(discovery_items)
    job.stage = "researching"
    await asyncio.to_thread(
        store.update_job_progress,
        job.id,
        stage=job.stage,
        items_discovered=job.items_discovered,
        items_total=job.items_total,
    )

    job.stage = "merging"
    await asyncio.to_thread(store.update_job_progress, job.id, stage=job.stage)
    merged = merge_records(discovery_items, {}, {})

    for row in merged:
        is_new = await asyncio.to_thread(
            store.upsert_lead,
            job.user_id,
            dedup_key(row),
            row.get("business_name", ""),
            _normalize(row.get("website", "")),
            job.niche,
            job.location,
            row.get("discovered_by_query", ""),
        )
        row["is_new_lead"] = is_new

    if job.icp:
        job.stage = "qualifying"
        await asyncio.to_thread(store.update_job_progress, job.id, stage=job.stage)
        await _qualify_all(merged, job.icp, job.user_id)

    job.stage = "exporting"
    await asyncio.to_thread(store.update_job_progress, job.id, stage=job.stage)

    user_output_dir = config.OUTPUT_DIR / job.user_id
    user_output_dir.mkdir(parents=True, exist_ok=True)
    filename = (
        f"discovered_{job.niche.strip().replace(' ', '_')}_"
        f"{job.location.strip().replace(' ', '_').replace(',', '')}_{int(time.time())}.csv"
    )
    out_path = export_csv(merged, user_output_dir / filename)
    logger.info(f"Exported {len(merged)} leads to {out_path} for job {job.id}")

    result_path = str(out_path.resolve())
    niche_key = job.niche.strip().lower()
    location_key = job.location.strip().lower()
    await asyncio.to_thread(
        store.put_discovery_search,
        job.user_id,
        niche_key,
        location_key,
        job.max_results,
        result_path,
    )
    job.result_path = result_path


async def _worker(worker_id: int) -> None:
    logger.info(f"Search worker {worker_id} started")
    while True:
        job = await _queue.get()
        try:
            await _run_search(job)
            await asyncio.to_thread(store.finish_job, job.id, job.result_path)
            job.status = "done"
        except LeadOrbytError as exc:
            logger.exception(f"Job {job.id} failed")
            job.error = exc.message
            job.error_type = exc.type.value
            job.status = "error"
            await asyncio.to_thread(store.fail_job, job.id, exc.message, exc.type.value)
        except Exception as exc:
            logger.exception(f"Job {job.id} failed")
            job.error = str(exc)
            job.error_type = ErrorType.INTERNAL.value
            job.status = "error"
            await asyncio.to_thread(store.fail_job, job.id, str(exc), ErrorType.INTERNAL.value)
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


async def submit(user_id: str, niche: str, location: str, max_results: int, icp: str = "") -> str:
    """Enqueue a search job and return its id immediately (does not wait)."""
    start_workers()
    job_id = uuid.uuid4().hex
    job = SearchJob(id=job_id, user_id=user_id, niche=niche, location=location, max_results=max_results, icp=icp)
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
        return {
            "status": job.status,
            "result_path": job.result_path,
            "error": job.error,
            "error_type": job.error_type,
            "stage": job.stage,
            "items_discovered": job.items_discovered,
            "items_enriched": job.items_enriched,
            "items_total": job.items_total,
        }
    return store.get_job(job_id, user_id)


async def wait_for(job_id: str) -> dict:
    """Block until `job_id` finishes, then return its final status dict."""
    job = _jobs[job_id]
    await job.done.wait()
    if job.error:
        return {"status": "error", "result_path": None, "error": job.error, "error_type": job.error_type}
    return {"status": "done", "result_path": job.result_path, "error": None, "error_type": None}
