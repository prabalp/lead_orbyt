import pytest

from leadorbyt import config, people_jobs, qualify_ml, store
from leadorbyt.people_jobs import PersonSearchJob, _run_people_search
from leadorbyt.qualify_ml import QualificationResult
from leadorbyt.sources import person_search


def _fake_person(name: str) -> dict:
    return {
        "full_name": name,
        "first_name": name,
        "last_name_obfuscated": "",
        "title": "CISO",
        "company_name": f"{name} Co",
        "company_domain": f"{name.lower()}.com",
        "has_email": False,  # skip reveal entirely -- not under test here
        "linkedin_url": "",
    }


@pytest.fixture(autouse=True)
def output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)


def _job(goal_new_leads=None, icp="") -> PersonSearchJob:
    return PersonSearchJob(
        id="job1",
        user_id="user1",
        job_titles=["CISO"],
        location="Austin, TX",
        max_results=5,
        max_paid_lookups=0,
        icp=icp,
        goal_new_leads=goal_new_leads,
    )


@pytest.mark.asyncio
async def test_no_goal_runs_exactly_one_round(isolated_db, monkeypatch):
    calls = {"n": 0}

    async def fake_search(job_titles, location, max_results, **filters):
        calls["n"] += 1
        return [_fake_person("Alice")]

    monkeypatch.setattr(person_search, "search_people", fake_search)

    job = _job(goal_new_leads=None)
    await _run_people_search(job)

    assert calls["n"] == 1
    assert job.people_found == 1


@pytest.mark.asyncio
async def test_goal_stops_once_reached_without_expanding(isolated_db, monkeypatch):
    """Enough new leads on round 1 already meets the goal -- no expansion needed."""
    calls = {"n": 0}

    async def fake_search(job_titles, location, max_results, **filters):
        calls["n"] += 1
        return [_fake_person("Alice"), _fake_person("Bob")]

    monkeypatch.setattr(person_search, "search_people", fake_search)

    job = _job(goal_new_leads=2)
    await _run_people_search(job)

    assert calls["n"] == 1
    assert job.people_found == 2


@pytest.mark.asyncio
async def test_goal_expands_when_first_round_falls_short(isolated_db, monkeypatch):
    """icp is set so qualification (and frontier learning) runs; round 1 is
    short of the goal, round 2 uses an expanded title list learned from round 1.
    """
    store.bump_frontier_token("user1", store.icp_hash("some icp"), "manager", "title", accepted=True)
    seen_titles = []

    async def fake_search(job_titles, location, max_results, **filters):
        seen_titles.append(list(job_titles))
        if len(seen_titles) == 1:
            return [_fake_person("Alice")]
        return [_fake_person("Bob"), _fake_person("Carol")]

    async def fake_qualify_pool(items, icp, user_id, skip_llm=False):
        return [QualificationResult(qualified=True, score=1.0, reason="fits", source="llm") for _ in items]

    monkeypatch.setattr(person_search, "search_people", fake_search)
    monkeypatch.setattr(qualify_ml, "qualify_pool", fake_qualify_pool)

    job = _job(goal_new_leads=3, icp="some icp")
    await _run_people_search(job)

    assert len(seen_titles) == 2
    assert seen_titles[1] != seen_titles[0]  # round 2 used an expanded title list
    assert "manager" in [t.lower() for t in seen_titles[1]]
    assert job.people_found == 3


@pytest.mark.asyncio
async def test_goal_stops_at_max_rounds_when_never_met(isolated_db, monkeypatch):
    # Seed far more frontier tokens than QUERY_EXPANSION_MAX_ROUNDS *
    # QUERY_EXPANSION_TOKENS_PER_ROUND could ever consume, so the loop is
    # bounded by the round cap itself, not by running out of tokens to try.
    for i in range(20):
        store.bump_frontier_token("user1", store.icp_hash("icp"), f"token{i}", "title", accepted=True)
    calls = {"n": 0}

    async def fake_search(job_titles, location, max_results, **filters):
        calls["n"] += 1
        return [_fake_person(f"Person{calls['n']}")]  # only 1 new lead per round, goal never met

    async def fake_qualify_pool(items, icp, user_id, skip_llm=False):
        return [QualificationResult(qualified=True, score=1.0, reason="fits", source="llm") for _ in items]

    monkeypatch.setattr(person_search, "search_people", fake_search)
    monkeypatch.setattr(qualify_ml, "qualify_pool", fake_qualify_pool)

    job = _job(goal_new_leads=100, icp="icp")
    await _run_people_search(job)

    assert calls["n"] == config.QUERY_EXPANSION_MAX_ROUNDS + 1  # first round + all expansion rounds, then stop


@pytest.mark.asyncio
async def test_goal_with_no_icp_stops_after_one_round_when_no_frontier(isolated_db, monkeypatch):
    """Without an ICP, qualification (and frontier learning) never runs, so
    there's nothing to expand into -- the loop degrades gracefully to one round.
    """
    calls = {"n": 0}

    async def fake_search(job_titles, location, max_results, **filters):
        calls["n"] += 1
        return [_fake_person("Alice")]

    monkeypatch.setattr(person_search, "search_people", fake_search)

    job = _job(goal_new_leads=10, icp="")
    await _run_people_search(job)

    assert calls["n"] == 1
