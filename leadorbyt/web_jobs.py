"""In-process job queue for DuckDuckGo web/social intent searches.

Separate from Google Maps (`jobs.py`) and from Reddit API search
(`signal_jobs.py`) so the agent can call one source, or both in sequence.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from scrapling.spiders.result import ItemList

from . import config, qualify_ml, store, web_signals
from .errors import ErrorType, LeadOrbytError
from .merge import export_csv

logger = logging.getLogger("leadorbyt.web_jobs")


@dataclass
class WebSearchJob:
    id: str
    user_id: str
    query: str
    location: str
    max_results: int
    icp: str = ""
    sites: list[str] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    status: str = "queued"
    result_path: str | None = None
    error: str | None = None
    error_type: str | None = None
    stage: str = "queued"
    signals_found: int = 0


_queue: asyncio.Queue[WebSearchJob] = asyncio.Queue()
_jobs: dict[str, WebSearchJob] = {}
_workers_started = False


async def _qualify_batch(signals: list[dict], job: WebSearchJob, icp_hash: str) -> None:
    to_qualify: list[dict] = []
    for signal in signals:
        state = await asyncio.to_thread(
            store.get_lead_state, job.user_id, icp_hash, web_signals.dedup_key(signal)
        )
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
        if result.source != "agent_pending":
            await asyncio.to_thread(
                store.set_lead_state,
                job.user_id,
                icp_hash,
                web_signals.dedup_key(signal),
                "QUALIFIED" if result.qualified else "REJECTED",
            )


async def _run_web_search(job: WebSearchJob) -> None:
    job.status = "running"
    job.stage = "searching"
    await asyncio.to_thread(store.start_signal_job, job.id)
    await asyncio.to_thread(store.update_signal_job_progress, job.id, stage=job.stage)

    logger.info("Web/social search %r in %r (max %s)", job.query, job.location, job.max_results)
    rows = await web_signals.discover(
        job.query, job.location, job.max_results, user_id=job.user_id, sites=job.sites or None
    )
    for row in rows:
        row.setdefault("qualified", "")
        row.setdefault("qualification_score", "")
        row.setdefault("qualification_reason", "")
        is_new = await asyncio.to_thread(
            store.upsert_signal_lead,
            job.user_id,
            web_signals.dedup_key(row),
            row.get("author", ""),
            row.get("community", ""),
            row.get("discovered_by_query", ""),
        )
        row["is_new_lead"] = is_new

    job.signals_found = len(rows)
    await asyncio.to_thread(store.update_signal_job_progress, job.id, signals_found=job.signals_found)

    if job.icp and rows:
        job.stage = "qualifying"
        await asyncio.to_thread(store.update_signal_job_progress, job.id, stage=job.stage)
        await _qualify_batch(rows, job, store.icp_hash(job.icp))

    job.stage = "exporting"
    await asyncio.to_thread(store.update_signal_job_progress, job.id, stage=job.stage)
    user_output_dir = config.OUTPUT_DIR / job.user_id
    user_output_dir.mkdir(parents=True, exist_ok=True)
    loc = job.location.strip().replace(" ", "_").replace(",", "") or "any"
    filename = f"web_signals_{job.query.strip().replace(' ', '_')}_{loc}_{int(time.time())}.csv"
    out_path = export_csv(ItemList(rows), user_output_dir / filename, fields=web_signals.WEB_SIGNAL_CSV_FIELDS)
    job.result_path = str(out_path.resolve())
    logger.info("Exported %s web signals to %s for job %s", len(rows), out_path, job.id)


async def _worker(worker_id: int) -> None:
    logger.info("Web search worker %s started", worker_id)
    while True:
        job = await _queue.get()
        try:
            await _run_web_search(job)
            await asyncio.to_thread(store.finish_signal_job, job.id, job.result_path)
            job.status = "done"
        except LeadOrbytError as exc:
            logger.exception("Web job %s failed", job.id)
            job.error = exc.message
            job.error_type = exc.type.value
            job.status = "error"
            await asyncio.to_thread(store.fail_signal_job, job.id, exc.message, exc.type.value)
        except Exception as exc:
            logger.exception("Web job %s failed", job.id)
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
    logger.info("Started %s web search workers", n)


async def submit(
    user_id: str,
    query: str,
    location: str,
    max_results: int,
    icp: str = "",
    sites: list[str] | None = None,
) -> str:
    if not config.WEB_SIGNALS_ENABLED:
        raise LeadOrbytError(ErrorType.INVALID_INPUT, "web/social search is disabled on this host")
    start_workers()
    job_id = uuid.uuid4().hex
    job = WebSearchJob(
        id=job_id,
        user_id=user_id,
        query=query,
        location=location,
        max_results=max_results,
        icp=icp,
        sites=sites or [],
    )
    _jobs[job_id] = job
    await asyncio.to_thread(
        store.create_signal_job, job_id, user_id, f"{query} | {location}", str(sites or []), icp, max_results
    )
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
            "signals_found": job.signals_found,
        }
    return store.get_signal_job(job_id, user_id)


async def wait_for(job_id: str) -> dict:
    job = _jobs[job_id]
    await job.done.wait()
    if job.error:
        return {"status": "error", "result_path": None, "error": job.error, "error_type": job.error_type}
    return {"status": "done", "result_path": job.result_path, "error": None, "error_type": None}
