import pytest

from leadorbyt import auth, config, people_jobs, server


@pytest.fixture(autouse=True)
def output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(config, "PUBLIC_URL", "https://lead.orbyt.in")


@pytest.mark.asyncio
async def test_find_people_leads_threads_company_domains_into_filters(isolated_db, monkeypatch):
    captured = {}

    async def fake_submit(user_id, job_titles, location, max_results, max_paid_lookups, icp, goal_new_leads, filters):
        captured["filters"] = filters
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
        await server.find_people_leads(
            ["VP Marketing"], "", company_domains=["fedex.com", "acme.org"]
        )
    finally:
        auth.current_user_id.reset(token)

    assert captured["filters"]["company_domains"] == ["fedex.com", "acme.org"]


@pytest.mark.asyncio
async def test_find_people_leads_omits_company_domains_by_default(isolated_db, monkeypatch):
    """Existing callers who never pass company_domains must be unaffected."""
    captured = {}

    async def fake_submit(user_id, job_titles, location, max_results, max_paid_lookups, icp, goal_new_leads, filters):
        captured["filters"] = filters
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
        await server.find_people_leads(["VP Marketing"], "Austin, TX")
    finally:
        auth.current_user_id.reset(token)

    assert captured["filters"]["company_domains"] is None
