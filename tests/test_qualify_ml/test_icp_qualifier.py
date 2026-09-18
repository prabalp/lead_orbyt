import pytest

from leadorbyt import config, store
from leadorbyt.qualify_ml import qualify_lead

GOOD_ITEM = {
    "business_name": "Great Independent Coffee Shop",
    "category": "Coffee shop",
    "website": "https://greatcoffee.example.com",
    "address": "1 Main St",
}


@pytest.fixture(autouse=True)
def tight_gate(monkeypatch):
    monkeypatch.setattr(config, "QUALIFY_MIN_LABELS", 3)
    monkeypatch.setattr(config, "QUALIFY_GATE_CONFIDENCE", 0.7)
    monkeypatch.setattr(config, "QUALIFY_GATE_MAX_STD", 0.3)


@pytest.mark.asyncio
async def test_cold_start_is_agent_pending(isolated_db):
    """No API key, ever: before any agent-supplied verdicts exist, a lead is
    fail-open (qualified=True) and clearly marked as awaiting a verdict.
    """
    result = await qualify_lead(GOOD_ITEM, "independent coffee shops", "user1")
    assert result.qualified is True
    assert result.source == "agent_pending"


@pytest.mark.asyncio
async def test_gate_decides_once_enough_verdicts_exist(isolated_db):
    icp = "independent coffee shops"
    icp_hash = store.icp_hash(icp)

    # Simulate the agent-qualify flow: enough consistent verdicts recorded
    # for this exact lead via submit_lead_verdicts' underlying store call.
    from leadorbyt.qualify_ml import embed, embedding_to_bytes, profile_text

    embedding = embedding_to_bytes(embed(profile_text(GOOD_ITEM)))
    for _ in range(config.QUALIFY_MIN_LABELS):
        store.add_qualification_label("user1", icp_hash, "greatcoffee.example.com", embedding, 1.0)

    result = await qualify_lead(GOOD_ITEM, icp, "user1")
    assert result.source == "gate_accept"
    assert result.qualified is True


@pytest.mark.asyncio
async def test_labels_are_scoped_per_user_and_icp(isolated_db):
    icp = "independent coffee shops"
    icp_hash = store.icp_hash(icp)
    from leadorbyt.qualify_ml import embed, embedding_to_bytes, profile_text

    embedding = embedding_to_bytes(embed(profile_text(GOOD_ITEM)))
    for _ in range(config.QUALIFY_MIN_LABELS):
        store.add_qualification_label("user1", icp_hash, "greatcoffee.example.com", embedding, 1.0)

    # A different user, or a different ICP for the same user, has no
    # accumulated labels yet -- still cold-start, still agent_pending.
    other_user_result = await qualify_lead(GOOD_ITEM, icp, "user2")
    assert other_user_result.source == "agent_pending"

    other_icp_result = await qualify_lead(GOOD_ITEM, "a completely different ICP", "user1")
    assert other_icp_result.source == "agent_pending"
