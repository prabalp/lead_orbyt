import pytest

from leadorbyt import config, store
from leadorbyt.people_jobs import (
    PersonSearchJob,
    _AUTO_HEADCOUNT_BANDS,
    _AUTO_SENIORITY_BANDS,
    _auto_variation_rounds,
    _run_people_search,
)
from leadorbyt.sources import person_search


def _fake_person(name: str, company: str | None = None) -> dict:
    return {
        "full_name": name,
        "first_name": name,
        "last_name_obfuscated": "",
        "title": "VP Marketing",
        "company_name": company or f"{name} Co",
        "company_domain": f"{(company or name).lower().replace(' ', '')}.com",
        "has_email": False,
        "linkedin_url": "",
    }


@pytest.fixture(autouse=True)
def output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)


def _job(max_results=1000, filters=None) -> PersonSearchJob:
    return PersonSearchJob(
        id="job1",
        user_id="user1",
        job_titles=["VP Marketing"],
        location="",
        max_results=max_results,
        max_paid_lookups=0,
        filters=filters or {},
    )


# --- _auto_variation_rounds (pure, no I/O) ----------------------------------


def test_auto_variation_uses_seniority_bands_when_unconstrained():
    rounds = _auto_variation_rounds({"seniorities": None, "headcount_min": None, "headcount_max": None})
    assert rounds == [{"seniorities": [s]} for s in _AUTO_SENIORITY_BANDS]


def test_auto_variation_respects_explicit_seniorities_and_falls_back_to_headcount():
    rounds = _auto_variation_rounds({"seniorities": ["vp"], "headcount_min": None, "headcount_max": None})
    assert rounds[0] == {"headcount_min": _AUTO_HEADCOUNT_BANDS[0][0], "headcount_max": _AUTO_HEADCOUNT_BANDS[0][1]}
    assert len(rounds) == len(_AUTO_HEADCOUNT_BANDS)


def test_auto_variation_returns_nothing_when_both_dimensions_already_constrained():
    rounds = _auto_variation_rounds({"seniorities": ["vp"], "headcount_min": 1, "headcount_max": 500})
    assert rounds == []


# --- _run_people_search integration -----------------------------------------


@pytest.mark.asyncio
async def test_auto_volume_expansion_varies_seniority_to_reach_max_results(isolated_db, monkeypatch):
    """Each seniority band should surface genuinely different people (mirrors
    BetterContact's real behavior, confirmed live: 'vp' and 'director' had
    zero overlap) -- confirm the job keeps going until max_results is hit."""
    calls = []

    async def fake_search(job_titles, location, max_results, **filters):
        seniority = (filters.get("seniorities") or ["none"])[0]
        calls.append(seniority)
        return [_fake_person(f"{seniority}-{i}") for i in range(3)]

    monkeypatch.setattr(person_search, "search_people", fake_search)

    job = _job(max_results=10)
    await _run_people_search(job)

    assert job.people_found >= 10
    assert calls[0] == "none"  # first round: caller's own filters, unvaried
    assert calls[1:4] == _AUTO_SENIORITY_BANDS[:3]  # then bands in order until target met


@pytest.mark.asyncio
async def test_auto_volume_expansion_bounded_by_round_budget(isolated_db, monkeypatch):
    """An unreachable max_results must not loop forever -- bounded by
    config.PEOPLE_SEARCH_MAX_ROUNDS total rounds."""
    monkeypatch.setattr(config, "PEOPLE_SEARCH_MAX_ROUNDS", 4)
    calls = {"n": 0}

    async def fake_search(job_titles, location, max_results, **filters):
        calls["n"] += 1
        return [_fake_person(f"person{calls['n']}")]  # 1 new person/round, target never met

    monkeypatch.setattr(person_search, "search_people", fake_search)

    job = _job(max_results=1000)
    await _run_people_search(job)

    assert calls["n"] == 4
    assert job.people_found == 4


@pytest.mark.asyncio
async def test_auto_volume_expansion_noop_when_max_results_already_met(isolated_db, monkeypatch):
    calls = {"n": 0}

    async def fake_search(job_titles, location, max_results, **filters):
        calls["n"] += 1
        return [_fake_person(f"person{i}") for i in range(5)]

    monkeypatch.setattr(person_search, "search_people", fake_search)

    job = _job(max_results=5)
    await _run_people_search(job)

    assert calls["n"] == 1
    assert job.people_found == 5


@pytest.mark.asyncio
async def test_cross_call_dedup_excludes_previously_seen_people(isolated_db, monkeypatch):
    """A person already surfaced to this user in an earlier job must be
    excluded from a completely separate later job's export outright, not
    just tagged is_new_lead=False -- see get_seen_person_dedup_keys."""
    from leadorbyt.people_merge import dedup_key

    alice = _fake_person("Alice")
    store.upsert_person_lead(
        "user1", dedup_key(alice), "Alice", "alice.com", "['VP Marketing']", "", "prior job",
    )

    async def fake_search(job_titles, location, max_results, **filters):
        return [alice, _fake_person("Bob")]

    monkeypatch.setattr(person_search, "search_people", fake_search)

    job = _job(max_results=1, filters={"seniorities": ["vp"], "headcount_min": 1, "headcount_max": 999_999_999})
    await _run_people_search(job)

    assert job.people_found == 1  # Alice excluded, only Bob is genuinely new
