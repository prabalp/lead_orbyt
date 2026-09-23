import pytest

from leadorbyt import auth, config, store


async def _call_as(user_id, coro_factory):
    token = auth.current_user_id.set(user_id)
    try:
        return await coro_factory()
    finally:
        auth.current_user_id.reset(token)


@pytest.mark.asyncio
async def test_gate_status_cold_start(isolated_db):
    from leadorbyt import server

    result = await _call_as("user1", lambda: server.get_icp_gate_status("some ICP"))

    assert result == {
        "labels_recorded": 0,
        "positive_labels": 0,
        "negative_labels": 0,
        "min_labels_required": config.QUALIFY_MIN_LABELS,
        "gate_ready": False,
        "balanced": False,
    }


@pytest.mark.asyncio
async def test_gate_status_ready_but_unbalanced(isolated_db, monkeypatch):
    from leadorbyt import server

    monkeypatch.setattr(config, "QUALIFY_MIN_LABELS", 2)
    icp_hash = store.icp_hash("some ICP")
    for i in range(3):
        store.add_qualification_label("user1", icp_hash, f"domain{i}.com", b"\x00" * 8, 1.0)

    result = await _call_as("user1", lambda: server.get_icp_gate_status("some ICP"))

    assert result["labels_recorded"] == 3
    assert result["positive_labels"] == 3
    assert result["negative_labels"] == 0
    assert result["gate_ready"] is True
    assert result["balanced"] is False


@pytest.mark.asyncio
async def test_gate_status_ready_and_balanced(isolated_db, monkeypatch):
    from leadorbyt import server

    monkeypatch.setattr(config, "QUALIFY_MIN_LABELS", 2)
    icp_hash = store.icp_hash("some ICP")
    store.add_qualification_label("user1", icp_hash, "good.com", b"\x00" * 8, 1.0)
    store.add_qualification_label("user1", icp_hash, "bad.com", b"\x00" * 8, 0.0)

    result = await _call_as("user1", lambda: server.get_icp_gate_status("some ICP"))

    assert result["gate_ready"] is True
    assert result["balanced"] is True


@pytest.mark.asyncio
async def test_gate_status_scoped_by_exact_icp_text(isolated_db):
    from leadorbyt import server

    icp_hash = store.icp_hash("ICP A")
    store.add_qualification_label("user1", icp_hash, "good.com", b"\x00" * 8, 1.0)

    result = await _call_as("user1", lambda: server.get_icp_gate_status("ICP B"))

    assert result["labels_recorded"] == 0  # different ICP text -- separate, unlabeled gate


@pytest.mark.asyncio
async def test_gate_status_scoped_by_user(isolated_db):
    from leadorbyt import server

    icp_hash = store.icp_hash("shared ICP text")
    store.add_qualification_label("user1", icp_hash, "good.com", b"\x00" * 8, 1.0)

    result = await _call_as("user2", lambda: server.get_icp_gate_status("shared ICP text"))

    assert result["labels_recorded"] == 0
