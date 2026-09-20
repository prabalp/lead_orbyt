import pytest

from leadorbyt import config, qualify_ml, store
from leadorbyt.qualify_ml import QualificationResult
from leadorbyt.signal_jobs import SignalSearchJob, _run_signal_search
from leadorbyt.sources import reddit


def _fake_signal(post_id: str, username: str = "some_user") -> dict:
    return {
        "reddit_post_id": post_id,
        "reddit_username": username,
        "subreddit": "smallbusiness",
        "post_title": "Looking for a CRM",
        "post_body": "Any recommendations for a small team?",
        "permalink": f"https://reddit.com/r/smallbusiness/comments/{post_id}/",
        "created_utc": 1700000000,
        "matched_query": "looking for a crm",
    }


@pytest.fixture(autouse=True)
def output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)


def _job(icp: str = "") -> SignalSearchJob:
    return SignalSearchJob(
        id="job1", user_id="user1", query="looking for a crm", subreddits=["smallbusiness"], max_results=10, icp=icp
    )


@pytest.mark.asyncio
async def test_run_signal_search_exports_csv(isolated_db, monkeypatch):
    async def fake_search_signals(query, subreddits, sort, time_filter, max_results, access_token=None):
        return [_fake_signal("p1"), _fake_signal("p2")]

    monkeypatch.setattr(reddit, "search_signals", fake_search_signals)

    job = _job()
    await _run_signal_search(job)

    assert job.signals_found == 2
    assert job.result_path is not None
    content = open(job.result_path).read()
    assert "reddit_post_id" not in content  # post id itself isn't a CSV column, only used for dedup
    assert "some_user" in content
    assert "smallbusiness" in content


@pytest.mark.asyncio
async def test_run_signal_search_marks_dedup_across_runs(isolated_db, monkeypatch):
    async def fake_search_signals(query, subreddits, sort, time_filter, max_results, access_token=None):
        return [_fake_signal("p1")]

    monkeypatch.setattr(reddit, "search_signals", fake_search_signals)

    job1 = _job()
    await _run_signal_search(job1)
    content1 = open(job1.result_path).read()
    assert "True" in content1  # is_new_lead True on first sight

    job2 = _job()
    await _run_signal_search(job2)
    content2 = open(job2.result_path).read()
    assert "False" in content2  # same post -- not new the second time


@pytest.mark.asyncio
async def test_run_signal_search_qualifies_when_icp_set(isolated_db, monkeypatch):
    async def fake_search_signals(query, subreddits, sort, time_filter, max_results, access_token=None):
        return [_fake_signal("p1"), _fake_signal("p2", "other_user")]

    async def fake_qualify_pool(items, icp, user_id):
        return [
            QualificationResult(qualified=True, score=1.0, reason="fits", source="llm"),
            QualificationResult(qualified=False, score=0.0, reason="not relevant", source="llm"),
        ]

    monkeypatch.setattr(reddit, "search_signals", fake_search_signals)
    monkeypatch.setattr(qualify_ml, "qualify_pool", fake_qualify_pool)

    job = _job(icp="small business owners frustrated with spreadsheets")
    await _run_signal_search(job)

    content = open(job.result_path).read()
    lines = content.splitlines()
    assert lines[1].split(",")[0] == "some_user"  # qualified lead sorted first
    assert "True" in lines[1]


@pytest.mark.asyncio
async def test_run_signal_search_reuses_lead_state_without_reasking(isolated_db, monkeypatch):
    icp = "small business owners"
    icp_hash = store.icp_hash(icp)
    from leadorbyt.signal_merge import dedup_key

    store.set_lead_state("user1", icp_hash, dedup_key(_fake_signal("p1")), "REJECTED")

    async def fake_search_signals(query, subreddits, sort, time_filter, max_results, access_token=None):
        return [_fake_signal("p1")]

    def boom(*args, **kwargs):
        raise AssertionError("should not need to re-qualify an already-known REJECTED signal")

    monkeypatch.setattr(reddit, "search_signals", fake_search_signals)
    monkeypatch.setattr(qualify_ml, "qualify_pool", boom)

    job = _job(icp=icp)
    await _run_signal_search(job)

    content = open(job.result_path).read()
    assert "Previously rejected" in content


@pytest.mark.asyncio
async def test_run_signal_search_no_icp_skips_qualification(isolated_db, monkeypatch):
    async def fake_search_signals(query, subreddits, sort, time_filter, max_results, access_token=None):
        return [_fake_signal("p1")]

    def boom(*args, **kwargs):
        raise AssertionError("should not qualify when no icp given")

    monkeypatch.setattr(reddit, "search_signals", fake_search_signals)
    monkeypatch.setattr(qualify_ml, "qualify_pool", boom)

    job = _job(icp="")
    await _run_signal_search(job)

    assert job.result_path is not None
