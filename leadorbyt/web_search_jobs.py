"""In-process job queue for the general-purpose web_search tool.

Separate from web_jobs.py (site-restricted social intent search) and
org_jobs.py (organization discovery, which runs several web_search queries
internally and does its own extraction) -- this is the plain, stateless
primitive: one query in, one CSV of {title, url, snippet} out. No ICP
qualification, no cross-call dedup/is_new_lead tracking -- a search-engine
call, not a lead-discovery pipeline, so there's no "lead" identity to track
across calls the way there is for people/businesses/signals.

Reuses the `signal_search_jobs` table (via store.create_signal_job et al.,
the same functions web_jobs.py/signal_jobs.py already share) rather than
adding a new table -- `subreddits`/`icp` are left blank for this job type.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from scrapling.spiders.result import ItemList

from . import config, store, web_search
from .errors import ErrorType, LeadOrbytError
from .merge import export_csv

logger = logging.getLogger("leadorbyt.web_search_jobs")


@dataclass
class WebSearchQueryJob:
    id: str
    user_id: str
    query: str
    max_results: int
    done: asyncio.Event = field(default_factory=asyncio.Event)
    status: str = "queued"
    result_path: str | None = None
    error: str | None = None
    error_type: str | None = None
    stage: str = "queued"
    results_found: int = 0


_queue: asyncio.Queue[WebSearchQueryJob] = asyncio.Queue()
_jobs: dict[str, WebSearchQueryJob] = {}
_workers_started = False


async def _run_web_search(job: WebSearchQueryJob) -> None:
    job.status = "running"
    job.stage = "searching"
    await asyncio.to_thread(store.start_signal_job, job.id)
    await asyncio.to_thread(store.update_signal_job_progress, job.id, stage=job.stage)

    logger.info("Web search %r (max %s)", job.query, job.max_results)
    rows = await web_search.search_web(job.query, job.max_results)

    job.results_found = len(rows)
    await asyncio.to_thread(store.update_signal_job_progress, job.id, signals_found=job.results_found)

    job.stage = "exporting"
    await asyncio.to_thread(store.update_signal_job_progress, job.id, stage=job.stage)
    user_output_dir = config.OUTPUT_DIR / job.user_id
    user_output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"web_search_{job.query.strip().replace(' ', '_')[:60]}_{int(time.time())}.csv"
    out_path = export_csv(ItemList(rows), user_output_dir / filename, fields=web_search.WEB_SEARCH_CSV_FIELDS)
    job.result_path = str(out_path.resolve())
    logger.info("Exported %s web search results to %s for job %s", len(rows), out_path, job.id)


async def _worker(worker_id: int) -> None:
    logger.info("Web search worker %s started", worker_id)
    while True:
        job = await _queue.get()
        try:
            await _run_web_search(job)
            await asyncio.to_thread(store.finish_signal_job, job.id, job.result_path)
            job.status = "done"
        except LeadOrbytError as exc:
            logger.exception("Web search job %s failed", job.id)
            job.error = exc.message
            job.error_type = exc.type.value
            job.status = "error"
            await asyncio.to_thread(store.fail_signal_job, job.id, exc.message, exc.type.value)
        except Exception as exc:
            logger.exception("Web search job %s failed", job.id)
            job.error = str(exc)
            job.error_type = ErrorType.INTERNAL.value
            job.status = "error"
            await asyncio.to_thread(store.fail_signal_job, job.id, str(exc), ErrorType.INTERNAL.value)
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
    logger.info("Started %s web search query workers", n)


async def submit(user_id: str, query: str, max_results: int) -> str:
    start_workers()
    job_id = uuid.uuid4().hex
    job = WebSearchQueryJob(id=job_id, user_id=user_id, query=query, max_results=max_results)
    _jobs[job_id] = job
    await asyncio.to_thread(store.create_signal_job, job_id, user_id, query, "", "", max_results)
    await _queue.put(job)
    return job_id


def status(job_id: str, user_id: str) -> dict | None:
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
            "results_found": job.results_found,
        }
    stored = store.get_signal_job(job_id, user_id)
    if stored is None:
        return None
    results_found = stored.pop("signals_found", 0)
    return {**stored, "results_found": results_found}


async def wait_for(job_id: str) -> dict:
    job = _jobs[job_id]
    await job.done.wait()
    if job.error:
        return {"status": "error", "result_path": None, "error": job.error, "error_type": job.error_type}
    return {"status": "done", "result_path": job.result_path, "error": None, "error_type": None}
