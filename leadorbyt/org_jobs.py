"""In-process job queue for organization-category discovery (find_organizations).

Mirrors web_jobs.py's shape (search, optional ICP qualification, CSV
export) but for organizations rather than social/web signals -- see
org_search.py for the actual discovery logic. Reuses the same
`signal_search_jobs` table (via store.create_signal_job et al.) web_jobs.py/
signal_jobs.py/web_search_jobs.py already share.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from scrapling.spiders.result import ItemList

from . import config, org_search, qualify_ml, store
from .errors import ErrorType, LeadOrbytError
from .merge import export_csv

logger = logging.getLogger("leadorbyt.org_jobs")


@dataclass
class OrgSearchJob:
    id: str
    user_id: str
    category: str
    location: str
    max_results: int
    icp: str = ""
    done: asyncio.Event = field(default_factory=asyncio.Event)
    status: str = "queued"
    result_path: str | None = None
    error: str | None = None
    error_type: str | None = None
    stage: str = "queued"
    organizations_found: int = 0


_queue: asyncio.Queue[OrgSearchJob] = asyncio.Queue()
_jobs: dict[str, OrgSearchJob] = {}
_workers_started = False


async def _qualify_batch(orgs: list[dict], job: OrgSearchJob, icp_hash: str) -> None:
    to_qualify: list[dict] = []
    for org in orgs:
        state = await asyncio.to_thread(store.get_lead_state, job.user_id, icp_hash, org_search.dedup_key(org))
        if state == "REJECTED":
            org["qualified"] = False
            org["qualification_score"] = 0.0
            org["qualification_reason"] = "Previously rejected for this ICP"
        elif state == "QUALIFIED":
            org["qualified"] = True
            org["qualification_score"] = 1.0
            org["qualification_reason"] = "Previously qualified for this ICP"
        else:
            to_qualify.append(org)
    if not to_qualify:
        return
    results = await qualify_ml.qualify_pool(to_qualify, job.icp, job.user_id)
    for org, result in zip(to_qualify, results):
        org["qualified"] = result.qualified
        org["qualification_score"] = result.score
        org["qualification_reason"] = result.reason
        if result.source != "agent_pending":
            await asyncio.to_thread(
                store.set_lead_state,
                job.user_id,
                icp_hash,
                org_search.dedup_key(org),
                "QUALIFIED" if result.qualified else "REJECTED",
            )


async def _run_org_search(job: OrgSearchJob) -> None:
    job.status = "running"
    job.stage = "searching"
    await asyncio.to_thread(store.start_signal_job, job.id)
    await asyncio.to_thread(store.update_signal_job_progress, job.id, stage=job.stage)

    logger.info("Organization search %r in %r (max %s)", job.category, job.location, job.max_results)
    orgs = await org_search.discover_organizations(job.category, job.location, job.max_results)
    for org in orgs:
        org.setdefault("qualified", "")
        org.setdefault("qualification_score", "")
        org.setdefault("qualification_reason", "")

    job.organizations_found = len(orgs)
    await asyncio.to_thread(store.update_signal_job_progress, job.id, signals_found=job.organizations_found)

    if job.icp and orgs:
        job.stage = "qualifying"
        await asyncio.to_thread(store.update_signal_job_progress, job.id, stage=job.stage)
        await _qualify_batch(orgs, job, store.icp_hash(job.icp))

    job.stage = "exporting"
    await asyncio.to_thread(store.update_signal_job_progress, job.id, stage=job.stage)
    user_output_dir = config.OUTPUT_DIR / job.user_id
    user_output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"organizations_{job.category.strip().replace(' ', '_')[:60]}_{int(time.time())}.csv"
    out_path = export_csv(ItemList(orgs), user_output_dir / filename, fields=org_search.ORG_CSV_FIELDS)
    job.result_path = str(out_path.resolve())
    logger.info("Exported %s organizations to %s for job %s", len(orgs), out_path, job.id)


async def _worker(worker_id: int) -> None:
    logger.info("Organization search worker %s started", worker_id)
    while True:
        job = await _queue.get()
        try:
            await _run_org_search(job)
            await asyncio.to_thread(store.finish_signal_job, job.id, job.result_path)
            job.status = "done"
        except LeadOrbytError as exc:
            logger.exception("Organization job %s failed", job.id)
            job.error = exc.message
            job.error_type = exc.type.value
            job.status = "error"
            await asyncio.to_thread(store.fail_signal_job, job.id, exc.message, exc.type.value)
        except Exception as exc:
            logger.exception("Organization job %s failed", job.id)
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
    logger.info("Started %s organization search workers", n)


async def submit(user_id: str, category: str, location: str, max_results: int, icp: str = "") -> str:
    start_workers()
    job_id = uuid.uuid4().hex
    job = OrgSearchJob(
        id=job_id, user_id=user_id, category=category, location=location, max_results=max_results, icp=icp
    )
    _jobs[job_id] = job
    await asyncio.to_thread(
        store.create_signal_job, job_id, user_id, f"{category} | {location}", "", icp, max_results
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
            "organizations_found": job.organizations_found,
        }
    stored = store.get_signal_job(job_id, user_id)
    if stored is None:
        return None
    organizations_found = stored.pop("signals_found", 0)
    return {**stored, "organizations_found": organizations_found}


async def wait_for(job_id: str) -> dict:
    job = _jobs[job_id]
    await job.done.wait()
    if job.error:
        return {"status": "error", "result_path": None, "error": job.error, "error_type": job.error_type}
    return {"status": "done", "result_path": job.result_path, "error": None, "error_type": None}
