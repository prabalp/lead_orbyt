"""In-process job queue for Reddit signal-lead discovery searches (see
people_jobs.py for the person-lead equivalent, jobs.py for the business one).

Simpler than people_jobs.py: there is no paid-reveal stage at all. A
signal's "contact" is just the Reddit username/permalink, already free from
the search step itself -- unlike a business (needs a website visit) or a
named person (needs a paid email reveal), there's no further resolution
step to gate or cap.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from scrapling.spiders.result import ItemList

from . import config, qualify_ml, social_connect, store
from .errors import ErrorType, LeadOrbytError
from .signal_merge import dedup_key, export_signal_csv
from .sources import reddit

logger = logging.getLogger("leadorbyt.signal_jobs")


@dataclass
class SignalSearchJob:
    id: str
    user_id: str
    query: str
    subreddits: list[str]
    max_results: int
    icp: str = ""
    sort: str = "new"
    time_filter: str = "week"
    done: asyncio.Event = field(default_factory=asyncio.Event)
    status: str = "queued"
    result_path: str | None = None
    error: str | None = None
    error_type: str | None = None
    stage: str = "queued"
    signals_found: int = 0


_queue: asyncio.Queue[SignalSearchJob] = asyncio.Queue()
_jobs: dict[str, SignalSearchJob] = {}
_workers_started = False


async def _qualify_batch(signals: list[dict], job: SignalSearchJob, icp_hash: str) -> None:
    """Qualify `signals` against job.icp, reusing any lead_states verdict
    already on record for this exact (user, ICP) pair -- same pattern as
    people_jobs.py's _qualify_batch, minus the EMAIL_FOUND/NO_EMAIL_FOUND
    states (no reveal step to have a verdict about) and minus adaptive
    query expansion (that's job-title-driven; Reddit signal search has no
    equivalent frontier to expand into).
    """
    to_qualify: list[dict] = []
    for signal in signals:
        state = await asyncio.to_thread(store.get_lead_state, job.user_id, icp_hash, dedup_key(signal))
        if state == "REJECTED":
            signal["qualified"] = False
            signal["qualification_score"] = 0.0
            signal["qualification_reason"] = "Previously rejected for this ICP"
        elif state == "QUALIFIED":
            signal["qualified"] = True
            signal["qualification_score"] = 1.0
            signal["qualification_reason"] = "Previously qualified for this ICP"
        else:
            to_qualify.append(signal)

    if not to_qualify:
        return

    results = await qualify_ml.qualify_pool(to_qualify, job.icp, job.user_id)
    for signal, result in zip(to_qualify, results):
        signal["qualified"] = result.qualified
        signal["qualification_score"] = result.score
        signal["qualification_reason"] = result.reason
        if result.source != "agent_pending":  # not a real verdict yet -- nothing to remember
            await asyncio.to_thread(
                store.set_lead_state,
                job.user_id,
                icp_hash,
                dedup_key(signal),
                "QUALIFIED" if result.qualified else "REJECTED",
            )


async def _run_signal_search(job: SignalSearchJob) -> None:
    job.status = "running"
    job.stage = "searching"
    await asyncio.to_thread(store.start_signal_job, job.id)
    await asyncio.to_thread(store.update_signal_job_progress, job.id, stage=job.stage)

    logger.info(f"Searching Reddit for {job.query!r} in {job.subreddits or 'all subreddits'} (max {job.max_results})")
    user_token = await asyncio.to_thread(social_connect.valid_access_token, job.user_id, "reddit")
    signals = await reddit.search_signals(
        job.query, job.subreddits, job.sort, job.time_filter, job.max_results, access_token=user_token
    )
    discovered_by_query = f"{job.query} | r/{','.join(job.subreddits) if job.subreddits else 'all'}"
    for signal in signals:
        signal["discovered_by_query"] = discovered_by_query
        signal["qualified"] = ""
        signal["qualification_score"] = ""
        signal["qualification_reason"] = ""

    job.signals_found = len(signals)
    await asyncio.to_thread(store.update_signal_job_progress, job.id, signals_found=job.signals_found)
    logger.info(f"Found {len(signals)} Reddit signals for job {job.id}")

    for signal in signals:
        is_new = await asyncio.to_thread(
            store.upsert_signal_lead,
            job.user_id,
            dedup_key(signal),
            signal.get("reddit_username", ""),
            signal.get("subreddit", ""),
            discovered_by_query,
        )
        signal["is_new_lead"] = is_new

    icp_hash = store.icp_hash(job.icp)
    if job.icp:
        job.stage = "qualifying"
        await asyncio.to_thread(store.update_signal_job_progress, job.id, stage=job.stage)
        await _qualify_batch(signals, job, icp_hash)
        signals.sort(key=lambda s: (s["qualified"] is not True, -(s["qualification_score"] or 0)))

    job.stage = "exporting"
    await asyncio.to_thread(store.update_signal_job_progress, job.id, stage=job.stage)

    user_output_dir = config.OUTPUT_DIR / job.user_id
    user_output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"reddit_{job.query.strip().replace(' ', '_')}_{int(time.time())}.csv"
    out_path = export_signal_csv(ItemList(signals), user_output_dir / filename)
    logger.info(f"Exported {len(signals)} signals to {out_path} for job {job.id}")

    job.result_path = str(out_path.resolve())


async def _worker(worker_id: int) -> None:
    logger.info(f"Signal search worker {worker_id} started")
    while True:
        job = await _queue.get()
        try:
            await _run_signal_search(job)
            await asyncio.to_thread(store.finish_signal_job, job.id, job.result_path)
            job.status = "done"
        except LeadOrbytError as exc:
            logger.exception(f"Signal job {job.id} failed")
            job.error = exc.message
            job.error_type = exc.type.value
            job.status = "error"
            await asyncio.to_thread(store.fail_signal_job, job.id, exc.message, exc.type.value)
        except Exception as exc:
            logger.exception(f"Signal job {job.id} failed")
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
    logger.info(f"Started {n} signal search workers")


async def submit(
    user_id: str,
    query: str,
    subreddits: list[str] | None,
    max_results: int,
    icp: str = "",
    sort: str = "new",
    time_filter: str = "week",
) -> str:
    """Enqueue a Reddit signal search job and return its id immediately (does not wait)."""
    start_workers()
    job_id = uuid.uuid4().hex
    job = SignalSearchJob(
        id=job_id,
        user_id=user_id,
        query=query,
        subreddits=subreddits or [],
        max_results=max_results,
        icp=icp,
        sort=sort,
        time_filter=time_filter,
    )
    _jobs[job_id] = job
    await asyncio.to_thread(
        store.create_signal_job, job_id, user_id, query, str(subreddits or []), icp, max_results
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
            "signals_found": job.signals_found,
        }
    return store.get_signal_job(job_id, user_id)


async def wait_for(job_id: str) -> dict:
    """Block until `job_id` finishes, then return its final status dict."""
    job = _jobs[job_id]
    await job.done.wait()
    if job.error:
        return {"status": "error", "result_path": None, "error": job.error, "error_type": job.error_type}
    return {"status": "done", "result_path": job.result_path, "error": None, "error_type": None}
