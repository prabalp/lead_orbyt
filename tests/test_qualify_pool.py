import numpy as np
import pytest

from leadorbyt import config
from leadorbyt import qualify_ml
from leadorbyt.qualify_ml import embed, qualify_pool


def _items(n: int) -> list[dict]:
    return [{"business_name": f"Business {i}", "category": "widgets"} for i in range(n)]


@pytest.mark.asyncio
async def test_cold_start_pool_is_all_agent_pending(isolated_db):
    """No API key, ever: before any agent-supplied verdicts exist for this
    (user, ICP), every lead in the pool is fail-open and awaiting a verdict.
    """
    results = await qualify_pool(_items(5), "some icp", "user1")
    assert len(results) == 5
    assert all(r.qualified is True and r.source == "agent_pending" for r in results)


@pytest.mark.asyncio
async def test_confident_gate_decisions_share_one_fit(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "QUALIFY_MIN_LABELS", 1)
    fit_calls = {"n": 0}

    class AlwaysConfidentGate:
        def predict(self, embedding):
            return 0.99, 0.01  # far past QUALIFY_GATE_CONFIDENCE, far under QUALIFY_GATE_MAX_STD

    def fake_fit_gate(labels, icp):
        fit_calls["n"] += 1
        return AlwaysConfidentGate()

    monkeypatch.setattr(qualify_ml, "_fit_gate", fake_fit_gate)
    monkeypatch.setattr(
        qualify_ml.store, "get_qualification_labels", lambda user_id, icp_hash: [(embed("seed").tobytes(), 1.0)]
    )

    results = await qualify_pool(_items(4), "icp", "user1")

    assert fit_calls["n"] == 1  # one shared fit across the whole pool, not one per lead
    assert all(r.source == "gate_accept" and r.qualified for r in results)


@pytest.mark.asyncio
async def test_mixed_pool_resolves_confident_and_defers_uncertain(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "QUALIFY_MIN_LABELS", 1)
    # Distinct multi-character words -- HashingVectorizer's default tokenizer
    # drops single-character tokens, so "Business 0"/"1"/"2" would otherwise
    # all collapse to the same {"business", "widgets"} bag-of-words.
    items = [
        {"business_name": "Alpha Traders", "category": "widgets"},
        {"business_name": "Beta Logistics", "category": "gadgets"},
        {"business_name": "Gamma Ventures", "category": "sprockets"},
    ]
    embeddings = [embed(qualify_ml.profile_text(item)) for item in items]
    means_stds = {0: (0.9, 0.01), 1: (0.1, 0.01), 2: (0.5, 0.5)}  # accept, reject, uncertain

    class ControlledGate:
        def predict(self, embedding):
            for i, emb in enumerate(embeddings):
                if np.array_equal(emb, embedding):
                    return means_stds[i]
            raise AssertionError("unexpected embedding")

    monkeypatch.setattr(qualify_ml, "_fit_gate", lambda labels, icp: ControlledGate())
    monkeypatch.setattr(
        qualify_ml.store, "get_qualification_labels", lambda user_id, icp_hash: [(embed("seed").tobytes(), 1.0)]
    )

    results = await qualify_pool(items, "icp", "user1")

    assert results[0].source == "gate_accept" and results[0].qualified is True
    assert results[1].source == "gate_reject" and results[1].qualified is False
    assert results[2].source == "agent_pending" and results[2].qualified is True  # fail-open


@pytest.mark.asyncio
async def test_qualify_lead_is_pool_of_one(isolated_db):
    result = await qualify_ml.qualify_lead(_items(1)[0], "icp", "user1")
    assert result.qualified is True
    assert result.source == "agent_pending"


@pytest.mark.asyncio
async def test_qualify_pool_empty_input(isolated_db):
    assert await qualify_pool([], "icp", "user1") == []
