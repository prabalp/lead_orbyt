import pytest

from leadorbyt import config, store
from leadorbyt.people_jobs import PersonSearchJob, _reveal_emails
from leadorbyt.sources import apollo_people, bettercontact

NO_ICP = store.icp_hash("")


def _person(person_id: str, has_email: bool = True) -> dict:
    return {
        "apollo_person_id": person_id,
        "first_name": "A",
        "last_name_obfuscated": "B.",
        "has_email": has_email,
        "email": "",  # _run_people_search initializes this before calling _reveal_emails
        "source_provider": "apollo",
    }


def _bc_person(linkedin_url: str) -> dict:
    return {
        "linkedin_url": linkedin_url,
        "full_name": "A B",
        "email": "",
        "source_provider": "bettercontact",
    }


def _job(max_paid_lookups: int) -> PersonSearchJob:
    return PersonSearchJob(
        id="job1",
        user_id="user1",
        job_titles=["CISO"],
        location="Austin, TX",
        max_results=10,
        max_paid_lookups=max_paid_lookups,
    )


@pytest.mark.asyncio
async def test_reveal_emails_respects_hard_cap(isolated_db, monkeypatch):
    calls = {"n": 0}

    async def fake_reveal(person_id):
        calls["n"] += 1
        return {"email": f"{person_id}@example.com"}

    monkeypatch.setattr(apollo_people, "reveal_email", fake_reveal)

    people = [_person(f"p{i}") for i in range(5)]
    job = _job(max_paid_lookups=2)
    await _reveal_emails(people, job, NO_ICP)

    assert calls["n"] == 2
    assert job.paid_lookups_used == 2
    assert people[0]["email"] == "p0@example.com"
    assert people[1]["email"] == "p1@example.com"
    assert people[2]["email"] == ""  # never attempted -- cap already hit


@pytest.mark.asyncio
async def test_reveal_emails_never_calls_apollo_for_people_without_email_flag(isolated_db, monkeypatch):
    async def fake_reveal(person_id):
        raise AssertionError("should not be called for has_email=False")

    monkeypatch.setattr(apollo_people, "reveal_email", fake_reveal)

    people = [_person("p1", has_email=False)]
    job = _job(max_paid_lookups=5)
    await _reveal_emails(people, job, NO_ICP)

    assert job.paid_lookups_used == 0
    assert people[0]["email"] == ""


@pytest.mark.asyncio
async def test_reveal_emails_cache_hit_does_not_consume_budget(isolated_db, monkeypatch):
    from leadorbyt import store

    store.put_person_contact("p1", {"email": "cached@example.com"})

    async def fake_reveal(person_id):
        raise AssertionError("should not call Apollo for a cached person")

    monkeypatch.setattr(apollo_people, "reveal_email", fake_reveal)

    people = [_person("p1")]
    job = _job(max_paid_lookups=1)
    await _reveal_emails(people, job, NO_ICP)

    assert job.paid_lookups_used == 0
    assert people[0]["email"] == "cached@example.com"


@pytest.mark.asyncio
async def test_reveal_emails_misses_still_count_as_attempts(isolated_db, monkeypatch):
    """A miss is free to bill, but still consumes an attempt against the cap."""

    async def fake_reveal(person_id):
        return None  # miss

    monkeypatch.setattr(apollo_people, "reveal_email", fake_reveal)

    people = [_person(f"p{i}") for i in range(3)]
    job = _job(max_paid_lookups=2)
    await _reveal_emails(people, job, NO_ICP)

    assert job.paid_lookups_used == 2
    assert people[2]["email"] == ""


@pytest.mark.asyncio
async def test_reveal_emails_dispatches_to_bettercontact_batch(isolated_db, monkeypatch):
    captured_urls = {}

    async def fake_reveal_emails(linkedin_urls):
        captured_urls["urls"] = linkedin_urls
        return {url: {"email": f"{url}@x.com"} for url in linkedin_urls}

    monkeypatch.setattr(bettercontact, "reveal_emails", fake_reveal_emails)

    people = [_bc_person(f"https://linkedin.com/in/p{i}") for i in range(5)]
    job = _job(max_paid_lookups=2)
    await _reveal_emails(people, job, NO_ICP)

    assert len(captured_urls["urls"]) == 2  # only 2 went into the one batch request
    assert job.paid_lookups_used == 2
    assert people[0]["email"] == "https://linkedin.com/in/p0@x.com"
    assert people[1]["email"] == "https://linkedin.com/in/p1@x.com"
    assert people[2]["email"] == ""  # never included in the batch


@pytest.mark.asyncio
async def test_bettercontact_reveal_cache_hit_reduces_batch_size(isolated_db, monkeypatch):
    from leadorbyt import store

    store.put_person_contact("https://linkedin.com/in/p0", {"email": "cached@x.com"})

    async def fake_reveal_emails(linkedin_urls):
        assert "https://linkedin.com/in/p0" not in linkedin_urls
        return {url: {"email": f"{url}@x.com"} for url in linkedin_urls}

    monkeypatch.setattr(bettercontact, "reveal_emails", fake_reveal_emails)

    people = [_bc_person("https://linkedin.com/in/p0"), _bc_person("https://linkedin.com/in/p1")]
    job = _job(max_paid_lookups=1)
    await _reveal_emails(people, job, NO_ICP)

    assert job.paid_lookups_used == 1  # only the uncached one counted
    assert people[0]["email"] == "cached@x.com"
    assert people[1]["email"] == "https://linkedin.com/in/p1@x.com"


@pytest.mark.asyncio
async def test_find_people_leads_clamps_max_paid_lookups(isolated_db, monkeypatch):
    from leadorbyt import auth, people_jobs, server

    monkeypatch.setattr(config, "MAX_PAID_LOOKUPS_CEILING", 10)
    captured = {}

    async def fake_submit(user_id, job_titles, location, max_results, max_paid_lookups, *args, **kwargs):
        captured["max_paid_lookups"] = max_paid_lookups
        return "job1"

    async def fake_wait_for(job_id):
        return {
            "status": "done",
            "result_path": str(config.OUTPUT_DIR / "user1" / "out.csv"),
            "error": None,
            "error_type": None,
        }

    monkeypatch.setattr(people_jobs, "submit", fake_submit)
    monkeypatch.setattr(people_jobs, "wait_for", fake_wait_for)

    token = auth.current_user_id.set("user1")
    try:
        await server.find_people_leads(["CISO"], "Austin, TX", max_paid_lookups=999)
    finally:
        auth.current_user_id.reset(token)

    assert captured["max_paid_lookups"] == 10
