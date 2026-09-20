import csv

import pytest

from leadorbyt import config, web_signals
from leadorbyt.errors import ErrorType, LeadOrbytError
from leadorbyt.web_jobs import WebSearchJob, _run_web_search, submit


async def test_run_web_search_exports_csv(tmp_path, isolated_db, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(config, "WEB_SIGNALS_ENABLED", True)

    async def fake_web(query, location, max_results, user_id=None, sites=None):
        return [
            {
                "source": "linkedin",
                "author": "jane",
                "community": "linkedin",
                "post_title": "Looking for a keynote speaker in Austin",
                "post_body": "Need a speaker for our offsite",
                "url": "https://www.linkedin.com/posts/jane_keynote",
                "email": "",
                "discovered_by_query": "speaking engagement Austin, TX site:linkedin.com",
            }
        ]

    monkeypatch.setattr(web_signals, "discover", fake_web)
    job = WebSearchJob(
        id="web1",
        user_id="user-1",
        query="speaking engagement",
        location="Austin, TX",
        max_results=5,
    )
    await _run_web_search(job)
    assert job.signals_found == 1
    with open(job.result_path, newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["source"] == "linkedin"
    assert row["post_title"] == "Looking for a keynote speaker in Austin"


async def test_web_search_passes_site_subset(tmp_path, isolated_db, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    captured = {}

    async def fake_web(query, location, max_results, user_id=None, sites=None):
        captured["sites"] = sites
        return []

    monkeypatch.setattr(web_signals, "discover", fake_web)
    job = WebSearchJob(
        id="web2",
        user_id="user-1",
        query="looking for a CRM",
        location="",
        max_results=5,
        sites=["linkedin"],
    )
    await _run_web_search(job)
    assert captured["sites"] == ["linkedin"]


async def test_submit_refuses_when_web_signals_disabled(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "WEB_SIGNALS_ENABLED", False)
    with pytest.raises(LeadOrbytError) as exc:
        await submit("user-1", "speaking engagement", "Austin, TX", 5)
    assert exc.value.type == ErrorType.INVALID_INPUT
