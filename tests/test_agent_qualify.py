import pytest

from leadorbyt import auth, config, qualify_ml, server, store
from leadorbyt.qualify_ml import pending_profiles, qualify_pool
from leadorbyt.sources import person_search


def _items(n: int) -> list[dict]:
    return [{"business_name": f"Business {i}", "category": "widgets"} for i in range(n)]


@pytest.mark.asyncio
async def test_pending_profiles_returns_only_agent_pending_items(isolated_db):
    items = _items(3)
    pending = await pending_profiles(items, "icp", "user1")

    assert len(pending) == 3  # cold start -- all of them are pending
    assert all("profile_text" in p for p in pending)
    assert pending[0]["profile_text"] == qualify_ml.profile_text(items[0])


@pytest.mark.asyncio
async def test_pending_profiles_empty_once_gate_confident(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "QUALIFY_MIN_LABELS", 1)

    class AlwaysConfidentGate:
        def predict(self, embedding):
            return 0.99, 0.01

    monkeypatch.setattr(qualify_ml, "_fit_gate", lambda labels, icp: AlwaysConfidentGate())
    monkeypatch.setattr(
        qualify_ml.store,
        "get_qualification_labels",
        lambda user_id, icp_hash: [(qualify_ml.embed("seed").tobytes(), 1.0)],
    )

    pending = await pending_profiles(_items(3), "icp", "user1")
    assert pending == []  # the gate is confident about all of them -- nothing pending


@pytest.mark.asyncio
async def test_list_unlabeled_leads_returns_pending_profiles(isolated_db, monkeypatch):
    async def fake_search(job_titles, location, max_results, **filters):
        return [{"full_name": "Jane Smith", "title": "CISO", "apollo_person_id": "p1"}]

    monkeypatch.setattr(person_search, "search_people", fake_search)

    token = auth.current_user_id.set("user1")
    try:
        pending = await server.list_unlabeled_leads(["CISO"], "Austin, TX", "some icp")
    finally:
        auth.current_user_id.reset(token)

    assert len(pending) == 1
    assert pending[0]["dedup_key"] == "apollo:p1"
    assert "profile_text" in pending[0]


@pytest.mark.asyncio
async def test_submit_lead_verdicts_persists_labels(isolated_db):
    token = auth.current_user_id.set("user1")
    try:
        result = await server.submit_lead_verdicts(
            "some icp",
            [
                {"profile_text": "Jane Smith | CISO", "qualified": True, "reason": "fits", "dedup_key": "apollo:p1"},
                {"profile_text": "Bob Jones | Intern", "qualified": False, "reason": "too junior", "dedup_key": "apollo:p2"},
            ],
        )
    finally:
        auth.current_user_id.reset(token)

    assert result == {"labels_added": 2}
    labels = store.get_qualification_labels("user1", store.icp_hash("some icp"))
    assert len(labels) == 2
    assert {label for _, label in labels} == {0.0, 1.0}


@pytest.mark.asyncio
async def test_submitted_verdicts_let_a_later_search_gate_decide(isolated_db):
    icp = "independent coffee shops"
    token = auth.current_user_id.set("user1")
    try:
        for i in range(config.QUALIFY_MIN_LABELS):
            await server.submit_lead_verdicts(
                icp, [{"profile_text": f"Jane Smith {i} | CISO", "qualified": True, "reason": "fits", "dedup_key": f"p{i}"}]
            )

        # A near-duplicate of the submitted profiles should now gate-decide
        # confidently -- no API key involved anywhere in this flow.
        results = await qualify_pool([{"business_name": "Jane Smith 0", "category": "CISO"}], icp, "user1")
    finally:
        auth.current_user_id.reset(token)

    assert results[0].source in ("gate_accept", "gate_reject")
