import pytest

from leadorbyt import store
from leadorbyt.people_jobs import PersonSearchJob, _qualify_batch, _reveal_via_apollo
from leadorbyt.sources import apollo_people


def test_set_and_get_lead_state(isolated_db):
    assert store.get_lead_state("user1", "icp1", "key1") is None
    store.set_lead_state("user1", "icp1", "key1", "QUALIFIED")
    assert store.get_lead_state("user1", "icp1", "key1") == "QUALIFIED"


def test_lead_state_scoped_per_icp(isolated_db):
    store.set_lead_state("user1", "icp1", "key1", "REJECTED")
    assert store.get_lead_state("user1", "icp2", "key1") is None


def test_lead_state_overwritten_on_update(isolated_db):
    store.set_lead_state("user1", "icp1", "key1", "QUALIFIED")
    store.set_lead_state("user1", "icp1", "key1", "EMAIL_FOUND")
    assert store.get_lead_state("user1", "icp1", "key1") == "EMAIL_FOUND"


def _person(person_id: str = "p1") -> dict:
    return {
        "apollo_person_id": person_id,
        "full_name": "Jane Smith",
        "business_name": "",
        "has_email": True,
        "email": "",
        "source_provider": "apollo",
    }


def _job() -> PersonSearchJob:
    return PersonSearchJob(
        id="job1", user_id="user1", job_titles=["CISO"], location="Austin, TX", max_results=10, max_paid_lookups=5
    )


@pytest.mark.asyncio
async def test_qualify_batch_reuses_rejected_state_without_llm_call(isolated_db):
    from leadorbyt.people_merge import dedup_key

    job = _job()
    job.icp = "some icp"
    icp_hash = store.icp_hash(job.icp)
    person = _person()
    store.set_lead_state(job.user_id, icp_hash, dedup_key(person), "REJECTED")

    await _qualify_batch([person], job, icp_hash)

    assert person["qualified"] is False
    assert "Previously rejected" in person["qualification_reason"]


@pytest.mark.asyncio
async def test_qualify_batch_reuses_qualified_state_without_llm_call(isolated_db):
    from leadorbyt.people_merge import dedup_key

    job = _job()
    job.icp = "some icp"
    icp_hash = store.icp_hash(job.icp)
    person = _person()
    store.set_lead_state(job.user_id, icp_hash, dedup_key(person), "QUALIFIED")

    await _qualify_batch([person], job, icp_hash)

    assert person["qualified"] is True
    assert "reveal not yet attempted" in person["qualification_reason"]


@pytest.mark.asyncio
async def test_qualify_batch_defers_unknown_state_to_agent_pending(isolated_db):
    """No prior lead_state and no agent-supplied verdicts yet for this ICP --
    falls through to qualify_pool's cold-start fail-open, not an LLM call.
    """
    job = _job()
    job.icp = "some icp"
    icp_hash = store.icp_hash(job.icp)
    person = _person()

    await _qualify_batch([person], job, icp_hash)

    assert person["qualified"] is True
    assert person["qualification_reason"] == (
        "Awaiting agent-supplied verdict -- see list_unlabeled_leads/submit_lead_verdicts"
    )
    # A pending verdict isn't a real decision yet -- nothing should be
    # persisted to lead_states for it.
    from leadorbyt.people_merge import dedup_key

    assert store.get_lead_state(job.user_id, icp_hash, dedup_key(person)) is None


@pytest.mark.asyncio
async def test_reveal_skips_previously_missed_person(isolated_db, monkeypatch):
    from leadorbyt.people_merge import dedup_key

    job = _job()
    icp_hash = store.icp_hash("")
    person = _person()
    store.set_lead_state(job.user_id, icp_hash, dedup_key(person), "NO_EMAIL_FOUND")

    async def fake_reveal(person_id):
        raise AssertionError("should not retry a person already marked NO_EMAIL_FOUND")

    monkeypatch.setattr(apollo_people, "reveal_email", fake_reveal)
    await _reveal_via_apollo([person], job, icp_hash)

    assert job.paid_lookups_used == 0
    assert person["email"] == ""


@pytest.mark.asyncio
async def test_reveal_sets_state_after_attempt(isolated_db, monkeypatch):
    from leadorbyt.people_merge import dedup_key

    job = _job()
    icp_hash = store.icp_hash("")
    person = _person()

    async def fake_reveal(person_id):
        return {"email": "jane@acme.com"}

    monkeypatch.setattr(apollo_people, "reveal_email", fake_reveal)
    await _reveal_via_apollo([person], job, icp_hash)

    assert store.get_lead_state(job.user_id, icp_hash, dedup_key(person)) == "EMAIL_FOUND"


@pytest.mark.asyncio
async def test_reveal_sets_no_email_found_state_on_miss(isolated_db, monkeypatch):
    from leadorbyt.people_merge import dedup_key

    job = _job()
    icp_hash = store.icp_hash("")
    person = _person()

    async def fake_reveal(person_id):
        return None

    monkeypatch.setattr(apollo_people, "reveal_email", fake_reveal)
    await _reveal_via_apollo([person], job, icp_hash)

    assert store.get_lead_state(job.user_id, icp_hash, dedup_key(person)) == "NO_EMAIL_FOUND"
